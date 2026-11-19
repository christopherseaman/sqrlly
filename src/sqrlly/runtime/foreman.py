"""Isolate nodes in snapshot-backed Git worktrees and settle their deltas."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import asdict
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from sqrlly.runtime.gates import scaffold_output_directory
from sqrlly.runtime.memory_gate import POLL_INTERVAL_S, wait_for_memory
from sqrlly.runtime.result import ExecutionResult, NodeExecutor, PromptBackend
from sqrlly.runtime.snapshots import SnapshotRun
from sqrlly.runtime.worktree_share import ensure_setup, materialize_shares
from sqrlly.schema.models import Node, Settings

logger = logging.getLogger(__name__)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", path, *args], capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result.stdout.strip()


class ForemanExecutor:
    def __init__(
        self, inner: NodeExecutor, base_workdir: str, max_parallel_jobs: int = 4,
        settings: Settings | None = None,
        memory_poll_interval_s: float = POLL_INTERVAL_S, *,
        workflow: str = "workflow", run: str | None = None, exclude_paths: tuple[str, ...] = (),
        recover: bool = False,
    ):
        self._inner = inner
        self._base = Path(base_workdir).resolve()
        self._root = Path(_git(self._base, "rev-parse", "--show-toplevel")).resolve()
        self._prefix = self._base.relative_to(self._root)
        self._pool = self._base / ".sqrlly"
        self._common = Path(_git(self._base, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
        self._settings = settings or Settings()
        self._creation = asyncio.Semaphore(max_parallel_jobs)
        self._locks: dict[str, asyncio.Lock] = {}
        self._worktrees: dict[str, str] = {}
        self._names: tuple[str, ...] = ()
        self._recover = recover
        self.reexecute: set[str] = set()
        self._dispatched: set[str] = set()
        self._memory_poll_s = memory_poll_interval_s
        self.run_id = run or uuid.uuid4().hex
        excluded = [str(self._prefix / ".sqrlly")]
        for path in exclude_paths:
            absolute = (self._base / path).resolve()
            if absolute.is_relative_to(self._root):
                excluded.append(str(absolute.relative_to(self._root)))
        self.snapshots = SnapshotRun(str(self._root), workflow, self.run_id, tuple(excluded))

    def scoped(self, name: str) -> ForemanExecutor:
        """Share the run and its leases while qualifying nested node ids."""
        from copy import copy
        child = copy(self)
        child._names = (*self._names, name)
        return child

    def _key(self, node_id: str) -> str:
        if not self._names and not node_id.startswith("["):
            return node_id
        return json.dumps([*self._names, node_id], separators=(",", ":"))

    def _tree(self, key: str) -> Path:
        digest = hashlib.sha256(self.snapshots.ref(key, "in").encode()).hexdigest()
        return self._pool / f"wt-{digest}"

    def _owner(self, tree: Path) -> Path:
        if tree.resolve() != tree or tree.parent != self._pool:
            raise RuntimeError(f"Unexpected worktree path {tree}")
        admin = Path(_git(tree, "rev-parse", "--absolute-git-dir")).resolve()
        if (admin.parent != self._common / "worktrees"
            or Path((admin / "gitdir").read_text().strip()).resolve() != tree / ".git"):
            raise RuntimeError(f"Unregistered snapshot worktree {tree}")
        return admin / "sqrlly-owner"

    def _prepare_tree(self, key: str, tree: Path, ref: str, settings: Settings) -> None:
        if self._pool.resolve() != self._pool:
            raise RuntimeError(f"Worktree pool has a symlink ancestor: {self._pool}")
        if not tree.exists():
            if self._recover and key not in self.reexecute and key not in self._dispatched:
                out = self.snapshots.ref(key, "out")
                if _git(self._root, "rev-parse", "--revs-only", out):
                    raise RuntimeError(f"Previous dispatch already produced {out}; checkpoint completion is missing. Inspect the ref before a fresh run.")
            self._pool.mkdir(exist_ok=True)
            commit = self.snapshots.capture_in(key, exclude_paths=self._shares(settings))
            _git(self._root, "worktree", "add", "--detach", "--no-checkout", "-q", str(tree), commit)
            self._owner(tree).write_text(ref)
        owner = self._owner(tree)
        if owner.read_text() != ref:
            raise RuntimeError(f"Snapshot owner differs for {tree}")
        checked_out = owner.with_name("sqrlly-checkout")
        if not checked_out.exists():
            if any(entry.name != ".git" for entry in tree.iterdir()):
                raise RuntimeError(f"Incomplete checkout contains files; retained {tree}")
            # Populate an empty, owned tree before invoking the repository's checkout hook.
            if _git(tree, "ls-tree", "HEAD"):
                _git(tree, "-c", "core.hooksPath=/dev/null", "restore", "--source=HEAD", "--staged", "--worktree", "--", ".")
            checked_out.touch()
        ready = owner.with_name("sqrlly-hook-complete")
        if not ready.exists():
            commit = _git(tree, "rev-parse", "HEAD")
            _git(tree, "hook", "run", "--ignore-missing", "post-checkout", "--", "0" * len(commit), commit, "1")
            ready.touch()

    def _check_removable(self, key: str, tree: Path) -> None:
        if self._owner(tree).read_text() != self.snapshots.ref(key, "in"):
            raise RuntimeError(f"Snapshot owner differs for {tree}")
        registered = _git(self._root, "worktree", "list", "--porcelain", "-z")
        for field in registered.split("\0"):
            if field.startswith("worktree "):
                other = Path(field.removeprefix("worktree ")).resolve()
                if other != tree and other.is_relative_to(tree):
                    raise RuntimeError(f"Nested Git worktree at {other}; retained {tree}")

        def fail(error):
            raise error
        for directory, dirs, files in os.walk(tree, onerror=fail):
            path = Path(directory)
            if path != tree and (".git" in dirs or ".git" in files
                                 or ("HEAD" in files and "objects" in dirs and "refs" in dirs)):
                raise RuntimeError(f"Nested Git repository at {path}; retained {tree}")

    def _shares(self, settings: Settings) -> tuple[str, ...]:
        return tuple(str(self._prefix / path) for path in settings.worktree_share)

    async def acquire_branch_worktree(self, node_id: str, settings: Settings | None = None) -> str:
        settings = settings or self._settings
        key = self._key(node_id)
        tree = self._tree(key)
        ref = self.snapshots.ref(key, "in")
        async with self._locks.setdefault(key, asyncio.Lock()), self._creation:
            prepare = asyncio.create_task(asyncio.to_thread(self._prepare_tree, key, tree, ref, settings))
            try:
                await asyncio.shield(prepare)
            except asyncio.CancelledError:
                await prepare
                raise
            dest = tree / self._prefix
            if dest.resolve() != dest:
                raise RuntimeError(f"Snapshot working directory contains a symlink: {dest}")
            dest.mkdir(parents=True, exist_ok=True)
            self._worktrees[key] = str(dest)
            materialize_shares(str(self._base), str(dest), settings.worktree_share)
            await ensure_setup(base=str(self._base), dest=str(dest), commands=settings.worktree_setup)
            return str(dest)

    async def execute(self, node: Node, context: dict[str, Any], workdir: str | None = None,
                      settings_override: Settings | None = None) -> ExecutionResult:
        settings = settings_override or self._settings
        await wait_for_memory(settings.memory_threshold_pct, node_id=node.id, poll_interval=self._memory_poll_s)
        isolated = node.effective_worktree(settings) == "isolated"
        try:
            dest = await self.acquire_branch_worktree(node.id, settings) if isolated else str(workdir or self._base)
            if node.output_contract:
                scaffold_output_directory(node.output_contract, dest)
            if not isolated:
                return await self._inner.execute(node, context, workdir=dest, settings_override=settings_override)

            key = self._key(node.id)
            tree = self._tree(key)
            record = self._owner(tree).with_name("sqrlly-dispatch.json")
            cached = None
            if record.exists():
                cached = json.loads(record.read_text())
                if cached is None:
                    raise RuntimeError(f"Interrupted dispatch has no recorded result; retained {tree}. Start a fresh run after inspecting it.")
            replay = self._recover and key not in self._dispatched and key not in self.reexecute
            self._dispatched.add(key)
            if replay and cached and cached["success"]:
                cached["retained_refs"] = set(cached["retained_refs"])
                result = ExecutionResult(**cached)
            else:
                # A crash before a result is recorded leaves the tree available for inspection.
                record.write_text("null")
                result = await self._inner.execute(node, context, workdir=dest, settings_override=settings_override)
                result.worktree = dest
                data = asdict(result)
                data["retained_refs"] = sorted(result.retained_refs)
                temporary = record.with_suffix(".tmp")
                temporary.write_text(json.dumps(data))
                os.replace(temporary, record)
            self.snapshots.capture_out(key, str(tree), exclude_paths=self._shares(settings))
            return result
        except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
            return ExecutionResult(success=False, error=str(exc), error_kind="infra")

    def settle(self, node: Node, settings: Settings, ids: list[str]) -> set[str]:
        keys = [self._key(node_id) for node_id in ids]
        trees = [(key, self._tree(key)) for key in keys if self._tree(key).exists()]
        for key, tree in trees:
            self._check_removable(key, tree)
            self.snapshots.capture_out(key, str(tree), exclude_paths=self._shares(settings))
        retained = set()
        if trees and node.effective_promote(settings):
            globs = node.output_contract.required_paths() if node.output_contract else None
            plan = self.snapshots.promote([(key, globs) for key, tree in trees], settings.on_promote_conflict,
                                          excludes=settings.promote_exclude, prefix=self._prefix.as_posix() if self._prefix.parts else "")
            if plan.conflicts and settings.on_promote_conflict == "warn":
                logger.warning("Promote conflicts: %s", plan.conflicts)
        else:
            retained.update(self.snapshots.ref(key, "out") for key, tree in trees)
        for key, tree in trees:
            self._check_removable(key, tree)
            try:
                _git(self._root, "worktree", "remove", str(tree))
            except RuntimeError:
                self._check_removable(key, tree)
                _git(self._root, "worktree", "remove", "--force", str(tree))
            self._worktrees.pop(key, None)
        return retained

    def get_worktree(self, node_id: str) -> str | None:
        return self._worktrees.get(self._key(node_id))

    def worktree_map(self) -> dict[str, str]:
        return dict(self._worktrees)

    def get_backend(self, settings: Settings | None = None) -> PromptBackend | None:
        if hasattr(self._inner, "get_backend"):
            return self._inner.get_backend(settings)
        return None

    async def close(self, clean: bool = False) -> None:
        self.snapshots.close(clean=clean)
        if hasattr(self._inner, "close"):
            await self._inner.close()
