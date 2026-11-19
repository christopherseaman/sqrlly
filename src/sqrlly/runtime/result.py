"""Execution result type and executor protocols.

ExecutionResult is the single result type for backends, executors, and
subprocesses. NodeExecutor and PromptBackend are the two duck-typed
protocols that produce them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqrlly.schema.models import Node, Settings


@dataclass
class ExecutionResult:
    success: bool = True
    output: str = ""
    error: str | None = None
    # Failure classification carried up to the JSONL `node_failed.kind` field
    # (via `make_failure_update`). Set at the point the failure is classified
    # (`overload` / `backend_error` in the prompt executor, `timeout` in
    # `run_with_timeout`); `None` on success or an unclassified dispatch
    # failure, which the failure funnel defaults to `node_error` (fail-safe
    # toward halt). See SCHEMA.md for the kind vocabulary + dispositions.
    error_kind: str | None = None
    structured_output: Any = None
    # The directory the node actually executed in (a foreman git
    # worktree, when active). Set by ForemanExecutor; None means the node
    # ran directly in the workdir. Used to validate output_contract
    # against where the node really wrote its files.
    worktree: str | None = None
    retained_refs: set[str] = field(default_factory=set)
    # The CLI chooses its own model when the workflow leaves it unset.
    model: str | None = None
    provider: str | None = None
    exit_code: int | None = None


class OverloadError(Exception):
    """Raised by a PromptBackend when the API returns 529/overloaded."""

    pass


class EvaluationError(RuntimeError):
    """An invalid measurement or failed route prevents the node from settling."""


class EvaluationBackendError(RuntimeError):
    """An LLM gate's backend dispatch failed after its downgrade chain and
    ``backend_max_retries``. ``kind`` is the dispatch's ``error_kind``
    (``backend_error`` / ``overload`` / ``timeout``); the gate fails the
    node with it."""

    def __init__(self, error: str, kind: str):
        super().__init__(error)
        self.kind = kind


class ManifestError(RuntimeError):
    """The repetition source could not be read or did not yield valid, unique child identities."""


class RouteError(ValueError):
    """A ``route:`` ``when:`` predicate failed to evaluate (parse error,
    name error, or sandbox violation). Halts with a clean message naming
    the route and predicate rather than dumping a raw traceback.

    Subclasses ``ValueError`` (a malformed predicate *is* a value error)
    so callers/tests catching ``ValueError`` keep working while the CLI
    can catch ``RouteError`` specifically for a clean halt."""


@runtime_checkable
class NodeExecutor(Protocol):
    async def execute(
        self, node: Node, context: dict[str, Any],
        workdir: str | None = None,
        settings_override: Settings | None = None,
    ) -> ExecutionResult: ...


@runtime_checkable
class PromptBackend(Protocol):
    async def send_prompt(
        self, prompt: str, model: str | None, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult: ...

    async def close(self) -> None: ...
