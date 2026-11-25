"""Temporary-index snapshots and settle-time promotion of isolated Git worktrees."""
from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
import tempfile
from contextlib import closing
from fnmatch import fnmatchcase
from functools import cache
from pathlib import Path

from sqrlly.runtime.lease import acquire_sqlite_lease
from sqrlly.runtime.promote import PromoteConflictError, plan_promotions


def _git(root, *args, data=None, index=None):
    env = os.environ.copy()
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    for role in ("AUTHOR", "COMMITTER"):
        env[f"GIT_{role}_NAME"] = "sqrlly"
        env[f"GIT_{role}_EMAIL"] = "sqrlly@localhost"
    proc = subprocess.run(["git", "-C", root, *args], env=env, input=data, capture_output=True)
    if proc.returncode:
        raise RuntimeError(f"git {args[0]} in {root}: {os.fsdecode(proc.stderr).strip()}")
    return proc.stdout


def _component(value):
    if re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}", value):
        return value
    return "_" + hashlib.sha256(value.encode()).hexdigest()


def _records(data):
    return data.rstrip(b"\0").split(b"\0") if data else []


def _tree(root, excluded):
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        paths = ["--", ".", *excluded]
        staged = _git(root, "ls-files", "--stage", "-z", *paths)
        _git(root, "update-index", "-z", "--index-info", data=staged, index=index)
        _git(root, "add", "-A", *paths, index=index)
        return _git(root, "write-tree", index=index).strip().decode()


def _entries(root, tree):
    result = {}
    for record in _records(_git(root, "ls-tree", "-rz", tree)):
        meta, name = record.split(b"\t", 1)
        mode, _, oid = meta.split()
        result[os.fsdecode(name)] = (mode, oid)
    return result


def _output_match(path, pattern):
    parts, glob = Path(path).parts, Path(pattern).parts
    if parts == glob:
        return True

    @cache
    def match(i, j):
        if j == len(glob):
            return i == len(parts)
        if glob[j] == "**":
            if j + 1 == len(glob):
                return i < len(parts)
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return i < len(parts) and fnmatchcase(parts[i], glob[j]) and match(i + 1, j + 1)

    return match(0, 0)


def _path_conflicts(base, current, updates):
    # update-index can silently discard a file when adding a child below it.
    final_paths = set(current)
    for path, entry in updates.items():
        if entry is None:
            final_paths.discard(path)
        else:
            final_paths.add(path)
    order = {path: i for i, path in enumerate(updates)}
    for path in final_paths:
        for parent in Path(path).parents:
            if str(parent) in final_paths:
                # An existing path wins. Between node outputs, preserve promotion order.
                blocked, blocker = sorted((path, str(parent)), key=lambda p: order.get(p, -1), reverse=True)
                yield blocked, blocker
    for path, entry in updates.items():
        dest = Path(base) / path
        blocked_parent = False
        for parent in reversed(Path(path).parents):
            candidate = Path(base) / parent
            try:
                dir_mode = candidate.lstat().st_mode
            except FileNotFoundError:
                break
            if not stat.S_ISDIR(dir_mode):
                if updates.get(str(parent), True) is not None:
                    yield path, str(parent)
                blocked_parent = True
                break
        if blocked_parent:
            continue
        try:
            existing = dest.lstat().st_mode
        except FileNotFoundError:
            continue
        if not any(kind(existing) for kind in (stat.S_ISREG, stat.S_ISDIR, stat.S_ISLNK)):
            yield path, "special file"
            continue
        if path not in current and not stat.S_ISDIR(existing):
            yield path, "ignored file"
            continue
        if entry is not None and stat.S_ISDIR(existing):
            # Ignored files do not appear in the Git tree.
            for directory, dirs, files in os.walk(dest):
                for name in [*files, *(d for d in dirs if (Path(directory) / d).is_symlink())]:
                    child = str((Path(directory) / name).relative_to(base))
                    if child not in updates or updates[child] is not None:
                        yield path, child


def _apply(base, current_tree, current, updates):
    with tempfile.TemporaryDirectory() as tmp:
        index = Path(tmp) / "index"
        _git(base, "read-tree", current_tree, index=index)
        zero = b"0" * len(current_tree)
        records = []
        for path, entry in sorted(updates.items(), key=lambda pair: pair[1] is not None):
            if (entry and entry[0] == b"160000") or current.get(path, (None,))[0] == b"160000":
                raise RuntimeError(f"Cannot promote Git submodule {path!r}")
            mode_bits, oid = entry or (b"0", zero)
            records.append(mode_bits + b" " + oid + b"\t" + os.fsencode(path) + b"\0")
        _git(base, "update-index", "-z", "--index-info", data=b"".join(records), index=index)
        patch = _git(base, "diff", "--cached", "--binary", "--no-renames", "--no-ext-diff",
                     "--no-textconv", "--src-prefix=a/", "--dst-prefix=b/", current_tree, index=index)
        if patch:
            _git(base, "apply", "--binary", "--whitespace=nowarn", "-", data=patch)


class SnapshotRun:
    def __init__(self, base: str, workflow: str, run: str, exclude_paths=()):
        self.base = str(Path(base).resolve())
        self.workflow = "refs/sqrlly/" + _component(workflow) + "/"
        self.run = _component(run)
        self.prefix = self.workflow + self.run + "/"
        self.excluded = [":(exclude,glob)**/.sqrlly", ":(exclude,glob)**/.sqrlly/**",
                         ":(exclude,glob)**/.sqrlly-checkpoint.db*",
                         *[f":(exclude,literal){p}" for p in exclude_paths]]
        common = _git(base, "rev-parse", "--path-format=absolute", "--git-common-dir")
        self.locks = Path(os.fsdecode(common.rstrip(b"\n"))) / "sqrlly-locks"
        self.locks.mkdir(exist_ok=True)
        with self._lock():
            self.earlier = {ref[len(self.workflow):].split("/")[0] for ref in self._refs(self.workflow)}
            self.earlier.discard(self.run)
            self.lease = acquire_sqlite_lease(self._lease_path(self.run), timeout=0)

    def _lease_path(self, run):
        key = hashlib.sha256((self.workflow + run).encode()).hexdigest()
        return self.locks / key

    def _lock(self):
        return closing(acquire_sqlite_lease(self.locks / "promotion.lock", timeout=60))

    def _refs(self, prefix):
        return _git(self.base, "for-each-ref", "--format=%(refname)", prefix).decode().splitlines()

    def existing_refs(self, refs: set[str]) -> set[str]:
        return refs.intersection(self._refs(self.prefix)) if refs else set()

    def ref(self, node_id, phase):
        if phase not in ("in", "out"):
            raise ValueError(f"Unknown snapshot phase {phase!r}")
        return self.prefix + _component(node_id) + "/" + phase

    def _capture(self, root, node_id, phase, exclude_paths):
        excluded = [*self.excluded, *(f":(exclude,literal){path}" for path in exclude_paths)]
        tree = _tree(root, excluded)
        head = _git(root, "rev-parse", "--revs-only", "HEAD").strip()
        ref = self.ref(node_id, phase)
        prior = _git(root, "rev-parse", "--revs-only", ref).strip()
        if prior and _git(root, "show", "-s", "--format=%T:%P", prior).strip() == tree.encode() + b":" + head:
            return prior.decode()
        args = ["-p", head] if head else []
        sha = _git(root, "commit-tree", tree, *args, "-m", f"sqrlly {phase}").strip().decode()
        _git(root, "update-ref", ref, sha)
        return sha

    def capture_in(self, node_id, *, exclude_paths=()):
        with self._lock():
            sha = self._capture(self.base, node_id, "in", exclude_paths)
            out = self.ref(node_id, "out")
            _git(self.base, "update-ref", "--stdin",
                 data=f"delete {out}-settled\ndelete {out}-pending\n".encode())
            return sha

    def capture_out(self, node_id, worktree, *, exclude_paths=()):
        self._capture(worktree, node_id, "out", exclude_paths)
        return self.ref(node_id, "out")

    def promote(self, specs, mode, excludes=None, prefix=""):
        with self._lock():
            return self._promote(specs, mode, excludes or (), prefix)

    def _promote(self, specs, mode, excludes, prefix):
        current_tree = _tree(self.base, self.excluded)
        current = _entries(self.base, current_tree)
        footprints, outputs, stale, revisions = {}, {}, {}, {}
        for node, globs in specs:
            before, after = self.ref(node, "in"), self.ref(node, "out")
            settled = _git(self.base, "rev-parse", "--revs-only", after + "-settled").strip()
            oid = _git(self.base, "rev-parse", after).strip()
            if settled == oid:
                continue
            pending = _git(self.base, "rev-parse", "--revs-only", after + "-pending").strip() == oid
            revisions[after] = oid.decode()
            paths = ["."]
            paths.extend(f":(exclude){e}" for e in excludes)
            changed = _records(_git(Path(self.base) / prefix, "diff", "--name-only",
                                    "--no-relative", "--no-renames", "-z", before, after,
                                    "--", *paths))
            old, new = _entries(self.base, before), _entries(self.base, after)
            outputs[node] = new
            changes = footprints[node] = {}
            for raw in changed:
                path = os.fsdecode(raw)
                if globs:
                    entry = new.get(path, old.get(path))
                    if entry[0] not in (b"100644", b"100755"):
                        continue
                    relative = Path(path).relative_to(prefix)
                    if not any(_output_match(relative, pattern) for pattern in globs):
                        continue
                changes[path] = "deleted" if path not in new else "modified" if path in old else "added"
                # An interrupted git apply may have installed part of this exact delta.
                applied = pending and current.get(path) == new.get(path)
                if current.get(path) != old.get(path) and not applied:
                    stale.setdefault(path, []).append(node)
        plan = plan_promotions(footprints, mode)
        for path, nodes in stale.items():
            plan.conflicts[path] = ["base", *dict.fromkeys([*plan.conflicts.get(path, []), *nodes])]
            if mode == "skip":
                for node in nodes:
                    plan.allowed[node].pop(path, None)
        if stale and mode == "fail":
            raise PromoteConflictError(plan.conflicts)
        updates = {path: outputs[node].get(path) for node, allowed in plan.allowed.items()
                   for path in allowed}
        while updates:
            obstructions = dict(_path_conflicts(self.base, current, updates))
            if not obstructions:
                self._mark(revisions, "pending")
                _apply(self.base, current_tree, current, updates)
                break
            for path, blocker in obstructions.items():
                owners = [node for node, allowed in plan.allowed.items() if path in allowed]
                plan.conflicts[path] = [blocker, *owners]
            if mode != "skip":
                raise PromoteConflictError(plan.conflicts)
            # A blocked directory replacement also keeps its original children.
            for path in list(updates):
                kept_child = updates[path] is None and any(
                    str(parent) in obstructions for parent in Path(path).parents)
                if path in obstructions or kept_child:
                    del updates[path]
                    for allowed in plan.allowed.values():
                        allowed.pop(path, None)
        self._mark(revisions, "settled")
        return plan

    def _mark(self, revisions, stage):
        commands = [f"update {ref}-{stage} {oid}\n" for ref, oid in revisions.items()]
        if stage == "settled":
            commands.extend(f"delete {ref}-pending\n" for ref in revisions)
        if commands:
            _git(self.base, "update-ref", "--stdin", data="".join(commands).encode())

    def close(self, clean: bool):
        if self.lease is None:
            return
        try:
            if clean:
                with self._lock():
                    for run in self.earlier:
                        path = self._lease_path(run)
                        try:
                            lease = acquire_sqlite_lease(path, timeout=0)
                        except BlockingIOError:
                            continue
                        with closing(lease):
                            prefix = self.workflow + run + "/"
                            refs = [r for r in self._refs(prefix)
                                    if re.fullmatch(re.escape(prefix) + r"[^/]+/(in|out|out-settled|out-pending)", r)]
                            if refs:
                                _git(self.base, "update-ref", "--stdin",
                                     data="".join(f"delete {ref}\n" for ref in refs).encode())
                        path.unlink(missing_ok=True)
        finally:
            with self._lock():
                self.lease.close()
                self.lease = None
                self._lease_path(self.run).unlink(missing_ok=True)
