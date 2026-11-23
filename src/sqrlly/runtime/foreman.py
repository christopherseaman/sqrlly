"""Isolate nodes in snapshot-backed Git worktrees and settle their deltas."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import uuid
from dataclasses import fields
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from sqrlly.runtime.gates import matching_output_files
from sqrlly.runtime.memory_gate import POLL_INTERVAL_S, wait_for_memory
from sqrlly.runtime.promote import PromoteConflictError
from sqrlly.runtime.result import ExecutionResult, NodeExecutor, PromptBackend
from sqrlly.runtime.snapshots import SnapshotRun
from sqrlly.runtime.worktree_share import ensure_setup, materialize_shares
from sqrlly.schema.models import Node, Settings

logger = logging.getLogger(__name__)
_DISPATCH_RECORD = TypeAdapter(ExecutionResult | None)
_RESULT_FIELDS = {field.name for field in fields(ExecutionResult)}


def _template_identity(value: Any, seen: dict[int, int]) -> list:
    # Jinja's sameas observes object identity. Number objects in traversal
    # order so equivalent binding graphs match across processes.
    address = id(value)
    if address in seen:
        return ["ref", seen[address]]
    seen[address] = len(seen)

    kind = type(value)
    if isinstance(value, dict):
        content = [[_template_identity(key, seen), _template_identity(item, seen)]
                   for key, item in value.items()]
    elif isinstance(value, (list, tuple)):
        content = [_template_identity(item, seen) for item in value]
    elif value is None or kind in (str, int, float, bool):
        return ["value", value]
    else:
        raise TypeError("unsupported template binding")

    # Structured output wrappers retain the original stdout in their attributes.
    attributes = getattr(value, "__dict__", None)
    return [kind.__module__, kind.__qualname__, content,
            _template_identity(attributes, seen) if attributes is not None else None]


def _read_receipt(path: Path) -> dict[str, Any]:
    checksum, payload = path.read_bytes().split(b"\n", 1)
    if hashlib.sha256(payload).hexdigest().encode() != checksum:
        raise ValueError("receipt checksum differs")
    return json.loads(payload)


def _write_receipt(path: Path, payload: bytes) -> None:
    path.parent.mkdir(exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(hashlib.sha256(payload).hexdigest().encode() + b"\n" + payload)
    os.replace(temporary, path)


def _retained_dispatch_evidence(
    scope: str, before: str, after: str, identity: str, raw: bytes,
) -> dict[str, str]:
    return {"scope": scope, "before": before, "after": after, "identity": identity,
            "dispatch": hashlib.sha256(raw).hexdigest()}


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
        self._names: tuple[str | tuple[str, int], ...] = ()
        self._fan_versions: dict[str, int] = {}
        self._recover = recover
        self._repeated_scope = False
        self._settled_item: str | None = None
        self._retained_only = False
        self.reexecute: set[str] = set()
        self._dispatched: set[str] = set()
        self._fan_children: set[str] = set()
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
        child._names = (*self._names, self._component(name))
        child._fan_versions = {}
        child._repeated_scope = self._repeated_scope or self._key(name) in self._fan_children
        return child

    def for_settled_item(self, item_digest: str, *, retained_only: bool = False) -> ForemanExecutor:
        from copy import copy

        child = copy(self)
        child._settled_item = item_digest
        child._retained_only = retained_only
        return child

    def _supports_result_recovery(self, node: Node, settings: Settings) -> bool:
        return (self._settled_item is not None and node.run is not None
                and node.effective_worktree(settings) == "isolated"
                and not node.eval and not node.route)

    def settlement_identity(self, node: Node, settings: Settings, context: dict[str, Any]) -> str | None:
        if not self._supports_result_recovery(node, settings):
            return None
        workflow_dir = str(settings.workflow_dir or self._base)
        try:
            identity = ["template-graph-v1", self._settled_item, workflow_dir,
                        _template_identity(context, {}),
                        node.model_dump(mode="json"), settings.model_dump(mode="json"),
                        node.outputs.schema_document if node.outputs else None]
            if self._retained_only:
                identity.append("retained-only")
            encoded = json.dumps(identity, allow_nan=False).encode()
        except (TypeError, ValueError, RecursionError):
            # Bindings may render normally without a supported receipt identity.
            return None
        return hashlib.sha256(encoded).hexdigest()

    def recover_settled(self, node: Node, settings: Settings,
                        identity: str | None, *,
                        consume_rejection: bool = False) -> tuple[ExecutionResult, int, bool] | None:
        """Recover a validated result and whether its promotion already completed."""
        # Ordinary references already recover through the owned-worktree
        # dispatch cache and idempotent snapshot settlement.
        if not self._repeated_scope or not self._recover:
            return None
        key = self._key(node.id)
        if key in self._dispatched or key in self.reexecute:
            return None
        path = self.snapshots.receipt_path(key)
        after = self.snapshots.revision(key, "out")
        settled = self.snapshots.revision(key, "out", settled=True)
        before = self.snapshots.revision(key, "in")
        if not (path.exists() or settled):
            # Prior dispatch evidence belongs to the scope even if the current
            # node no longer qualifies for a receipt or an isolated worktree.
            tree = self._tree(key)
            if not (after or tree.exists()):
                # Input capture precedes worktree creation and is safe to retry.
                return None
            if tree.exists():
                owner = self._owner(tree)
                dispatch = owner.with_name("sqrlly-dispatch.json")
                if dispatch.exists():
                    raw = dispatch.read_bytes()
                    recorded = _DISPATCH_RECORD.validate_json(raw)
                    if (recorded is not None and not recorded.success
                            and node.effective_worktree(settings) == "isolated"):
                        # A recorded failure can use the existing dispatch retry path.
                        return None
                    if (recorded is not None and recorded.success and identity is not None
                            and not node.effective_promote(settings) and before and after):
                        if self._has_retained_dispatch(node, settings, key, before, after, identity):
                            # Reenter ordinary cached dispatch and output validation.
                            return None
                elif not after:
                    return None  # Checkout interrupted before dispatch began.
        try:
            if self._retained_only:
                if not (after and after == settled and node.effective_promote(settings)):
                    raise ValueError("retained result has no matching settlement")
                tree = self._tree(key)
                owner = self._owner(tree)
                path = owner.with_name("sqrlly-retained-dispatch.json")
            record = _read_receipt(path)
            if (identity is not None
                    and type(record["version"]) is int and record["version"] == 1
                    and record["scope"] == self.snapshots.ref(key, "out")
                    and record["identity"] == identity and before and record["before"] == before
                    and after and record["after"] == after
                    and record["result"].keys() == _RESULT_FIELDS):
                stored = record["result"]
                # Fresh schema validation uses Python's JSON decoder, whose
                # value domain includes escaped lone surrogates. Pydantic
                # validates the dispatch metadata; structured_output is Any.
                metadata = {**stored, "structured_output": None}
                result = _DISPATCH_RECORD.validate_json(json.dumps(metadata), strict=True)
                count = record["executions"]
                if result is not None and type(count) is int and 1 <= count <= 3:
                    # Keep stdout from the receipt's per-document decoder.
                    # Pydantic can share equal strings across unrelated receipts.
                    result.output = stored["output"]
                    result.structured_output = stored["structured_output"]
                    if result.success and node.outputs and node.outputs.schema_file:
                        # Command output validation parses stdout with json.loads.
                        # Its per-document key sharing is observable in templates.
                        structured = json.loads(result.output)
                        if json.dumps(structured, allow_nan=False) != json.dumps(
                            record["result"]["structured_output"], allow_nan=False,
                        ):
                            raise ValueError("structured result differs from stdout")
                        result.structured_output = structured
                    if result.success and after == settled:
                        if self._retained_only:
                            raw = owner.with_name("sqrlly-dispatch.json").read_bytes()
                            if (owner.read_text() != self.snapshots.ref(key, "in")
                                    or record["dispatch"] != hashlib.sha256(raw).hexdigest()
                                    or not self._matches_retained_output(node, settings, tree, after)):
                                raise ValueError("retained worktree or dispatch differs")
                            # Normal idempotent settlement removes the verified tree.
                            self._dispatched.add(key)
                            return result, count, False
                        return result, count, True
                    if (result.success and not settled and record.get("promotion_conflict") is True
                            and node.effective_promote(settings)):
                        tree = self._tree(key)
                        if (tree.is_dir() and self._owner(tree).read_text() == self.snapshots.ref(key, "in")
                                and self._matches_retained_output(node, settings, tree, after)):
                            # Consume the conflict before retrying settlement. An interrupted
                            # retry must supply its own evidence of the outcome.
                            del record["promotion_conflict"]
                            _write_receipt(path, json.dumps(record, allow_nan=False).encode())
                            return result, count, False
                    if not result.success and count == 3 and not settled:
                        tree = self._tree(key)
                        owner = self._owner(tree)
                        if owner.read_text() != self.snapshots.ref(key, "in"):
                            raise ValueError("worktree owner differs")
                        raw = owner.with_name("sqrlly-dispatch.json").read_bytes()
                        if record["rejected_dispatch"] != hashlib.sha256(raw).hexdigest():
                            raise ValueError("rejected dispatch differs")
                        cached = _DISPATCH_RECORD.validate_json(raw)
                        if (cached is not None and cached.success
                                and self._matches_retained_output(node, settings, tree, after, allow_missing=True)):
                            # Acquisition can run setup and change the tree. Keep
                            # this evidence until the post-acquisition check.
                            if consume_rejection:
                                path.unlink()
                            return None
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            pass
        raise RuntimeError(f"Previous dispatch evidence for {self.snapshots.ref(key, 'out')}; "
                           "checkpoint completion is missing and settled receipt is unavailable or mismatched.")

    def _has_retained_dispatch(self, node: Node, settings: Settings, key: str,
                               before: str, after: str, identity: str) -> bool:
        tree = self._tree(key)
        if not tree.is_dir():
            return False
        try:
            owner = self._owner(tree)
            scope = self.snapshots.ref(key, "in")
            raw = owner.with_name("sqrlly-dispatch.json").read_bytes()
            result = _DISPATCH_RECORD.validate_json(raw)
            evidence = _read_receipt(owner.with_name("sqrlly-retained-dispatch.json"))
            expected = _retained_dispatch_evidence(scope, before, after, identity, raw)
            return (result is not None and result.success and owner.read_text() == scope
                    and evidence == expected
                    and self._matches_retained_output(node, settings, tree, after))
        except (OSError, ValueError, RuntimeError):
            return False

    def _matches_retained_output(
        self, node: Node, settings: Settings, tree: Path, commit: str, *, allow_missing: bool = False,
    ) -> bool:
        # File validation also sees ignored and excluded files that Git omits.
        required_files = set()
        for pattern in node.outputs.files if node.outputs else ():
            matches = list(matching_output_files(pattern, str(tree / self._prefix)))
            if not matches and not allow_missing:
                return False
            required_files.update(path.relative_to(tree).as_posix() for path in matches)
        return self.snapshots.matches_output(
            tree, commit, exclude_paths=self._shares(settings), required_files=required_files,
        )

    def record_result_for_settlement(self, node: Node, identity: str, result: ExecutionResult, executions: int) -> None:
        if self._retained_only and not result.success:
            return
        key = self._key(node.id)
        path = self.snapshots.receipt_path(key)
        record = {
            "version": 1, "scope": self.snapshots.ref(key, "out"), "identity": identity,
            "before": self.snapshots.revision(key, "in"), "after": self.snapshots.revision(key, "out"),
            "result": {**vars(result), "retained_refs": sorted(result.retained_refs)},
            "executions": executions,
        }
        if self._retained_only:
            owner = self._owner(self._tree(key))
            path = owner.with_name("sqrlly-retained-dispatch.json")
            raw = owner.with_name("sqrlly-dispatch.json").read_bytes()
            record["dispatch"] = hashlib.sha256(raw).hexdigest()
        elif not result.success:
            raw = self._owner(self._tree(key)).with_name("sqrlly-dispatch.json").read_bytes()
            record["rejected_dispatch"] = hashlib.sha256(raw).hexdigest()
        try:
            payload = json.dumps(record, allow_nan=False).encode()
        except (TypeError, ValueError):
            # A validated result can exceed the receipt format's JSON domain.
            # Clear old evidence before allowing this result to settle.
            path.unlink(missing_ok=True)
            return
        _write_receipt(path, payload)

    def _component(self, node_id: str) -> str | tuple[str, int]:
        version = self._fan_versions.get(node_id, 0)
        return (node_id, version) if version else node_id

    def _key(self, node_id: str) -> str:
        component = self._component(node_id)
        if not self._names and isinstance(component, str) and not component.startswith("["):
            return component
        return json.dumps([*self._names, component], separators=(",", ":"))

    def may_replay_child(self, child_id: str, node: Node, settings: Settings) -> bool:
        if node.workflow:
            return self._recover  # Inner nodes recover their own dispatch records.
        return (self._recover and child_id not in self.reexecute
                and node.effective_worktree(settings, fan_child=True) == "isolated")

    def set_fan_versions(self, versions: dict[str, int]) -> None:
        self._fan_versions.update(versions)

    def start_fan_child(self, child_id: str) -> None:
        # Only the join can remove these trees, after all child success stamps are checkpointed.
        key = self._key(child_id)
        self._fan_children.add(key)
        if child_id in self.reexecute:
            self.reexecute.add(key)

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
            if self._recover and key not in self.reexecute and key not in self._dispatched and key not in self._fan_children:
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
                      settings_override: Settings | None = None, *,
                      invocation_identity: str | None = None) -> ExecutionResult:
        settings = settings_override or self._settings
        await wait_for_memory(settings.memory_threshold_pct, node_id=node.id, poll_interval=self._memory_poll_s)
        isolated = node.effective_worktree(settings) == "isolated"
        try:
            dest = await self.acquire_branch_worktree(node.id, settings) if isolated else str(workdir or self._base)
            if not isolated:
                return await self._inner.execute(node, context, workdir=dest, settings_override=settings_override)

            key = self._key(node.id)
            tree = self._tree(key)
            owner = self._owner(tree)
            record = owner.with_name("sqrlly-dispatch.json")
            evidence_path = owner.with_name("sqrlly-retained-dispatch.json")
            cached = None
            if record.exists():
                raw = record.read_bytes()
                cached = _DISPATCH_RECORD.validate_json(raw)
                if cached is None:
                    raise RuntimeError(f"Interrupted dispatch has no recorded result; retained {tree}. Start a fresh run after inspecting it.")
            replay = self._recover and key not in self._dispatched and key not in self.reexecute
            if replay and cached and cached.success and self._repeated_scope:
                # Setup may change the files cached output validation will see.
                # Recheck the snapshot before recapturing or using the result.
                if self.recover_settled(node, settings, invocation_identity, consume_rejection=True) is not None:
                    raise RuntimeError(f"Settlement changed during worktree acquisition; retained {tree}")
                cached.worktree = dest
            self._dispatched.add(key)
            promote = node.effective_promote(settings)
            replay_success = replay and cached is not None and cached.success
            if replay_success:
                result = cached
                # Pydantic can share equal stdout strings across dispatch records.
                result.output = json.loads(raw).get("output", result.output)
            else:
                # A crash before a result is recorded leaves the tree available for inspection.
                record.write_text("null")
                evidence_path.unlink(missing_ok=True)
                result = await self._inner.execute(node, context, workdir=dest, settings_override=settings_override)
                result.worktree = dest
                temporary = record.with_suffix(".tmp")
                raw = _DISPATCH_RECORD.dump_json(result)
                temporary.write_bytes(raw)
                os.replace(temporary, record)
            self.snapshots.capture_out(key, str(tree), exclude_paths=self._shares(settings))
            if not replay_success and result.success and not promote and invocation_identity is not None:
                before = self.snapshots.revision(key, "in")
                after = self.snapshots.revision(key, "out")
                if before and after:
                    # A prior attempt can leave an output ref at this same scope.
                    # Publish proof only after capturing this dispatch's output.
                    evidence = _retained_dispatch_evidence(
                        self.snapshots.ref(key, "in"), before, after, invocation_identity, raw,
                    )
                    _write_receipt(evidence_path, json.dumps(evidence).encode())
            return result
        except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as exc:
            return ExecutionResult(success=False, error=str(exc), error_kind="infra")

    def settle(self, node: Node, settings: Settings, ids: list[str]) -> set[str]:
        if node.effective_worktree(settings) == "off":
            return set()
        keys = [self._key(node_id) for node_id in ids]
        trees = [(key, self._tree(key)) for key in keys if self._tree(key).exists()]
        for key, tree in trees:
            self._check_removable(key, tree)
            self.snapshots.capture_out(key, str(tree), exclude_paths=self._shares(settings))
        retained = set()
        if trees and node.effective_promote(settings):
            globs = node.outputs.files if node.outputs else None
            try:
                plan = self.snapshots.promote([(key, globs) for key, tree in trees], settings.on_promote_conflict,
                                              excludes=settings.promote_exclude, prefix=self._prefix.as_posix() if self._prefix.parts else "")
            except PromoteConflictError:
                if not self._retained_only and self._supports_result_recovery(node, settings) and len(trees) == 1:
                    key, _ = trees[0]
                    path = self.snapshots.receipt_path(key)
                    if path.exists():
                        record = _read_receipt(path)
                        if record["after"] == self.snapshots.revision(key, "out"):
                            record["promotion_conflict"] = True
                            _write_receipt(path, json.dumps(record, allow_nan=False).encode())
                raise
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
