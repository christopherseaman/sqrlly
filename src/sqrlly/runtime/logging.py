"""Structured JSONL event logging for workflow execution."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any


def node_start_event(node: str) -> dict[str, Any]:
    return {"event": "node_started", "node": node, "_started_at": time.monotonic()}


def failed_kinds_map(
    failed_nodes: "set[str] | list[str]", errors: "list[dict[str, Any]]",
) -> dict[str, str]:
    """Map each failed node id to its failure ``kind`` for ``workflow_end``.

    Reads the kind from the node's error record (default ``node_error``).
    Error records for nodes NOT in ``failed_nodes`` — e.g. a ``warn_continue``
    note on a node that COMPLETED — are excluded, so the map only ever names
    genuine failures. ``errors`` accumulates within a run (``operator.add``),
    so a node carrying more than one record resolves last-write-wins to its
    latest kind. It does NOT span ``--resume``: each resumed process reseeds
    ``errors`` to ``[]`` and wipes the checkpoint (``cli/main.py::_seed_state``),
    so this map covers only the current run's failures — the append-mode
    ``--log`` file holds the cross-attempt history.
    """
    failed = set(failed_nodes)
    out: dict[str, str] = {}
    for err in errors or []:
        node = err.get("node")
        if node in failed:
            out[node] = err.get("kind", "node_error")
    return out


def _emit_update_events(emitter: Any, update: dict[str, Any]) -> None:
    """Derive workflow events from a single super-step partial update.

    A LangGraph ``stream_mode="updates"`` chunk is shaped
    ``{node_name: partial_state_update}``; the partial update is the
    delta the node returned (after reducer application). The event
    types we surface — ``node_model`` (LLM nodes only: which
    provider and model ran), ``node_completed``, ``node_failed``,
    ``gate_evaluated``, ``node_retried`` — each correspond to a
    specific shape inside that delta. We read them directly here
    rather than diffing successive snapshots.

    Free function rather than a method so ``JsonlLogger`` and
    ``SubgraphLogger`` share the derivation logic and only differ in
    how their ``emit()`` formats the event (``SubgraphLogger`` prefixes
    the ``node`` field). The ``emitter`` argument duck-types ``emit()``.

    Emission order is fixed: ``node_model`` → ``gate_evaluated`` →
    ``node_completed`` → ``node_failed`` → ``node_retried``. Tests
    asserting on event order depend on this. The collapsed gate emits
    the record + the outcome (completed / failed) in ONE update, so
    ``gate_evaluated`` precedes BOTH ``node_completed`` and
    ``node_failed`` — "gate ran, then the node settled."

    Command-only nodes (route dispatchers that emit ``Command(goto=...)``
    without any state update) appear in the updates stream as
    ``{node_name: None}``. Skip them — no events to derive.
    """
    if update is None:
        return
    for node, info in (update.get("node_models") or {}).items():
        emitter.emit({
            "event": "node_model",
            "node": node,
            "model": info.get("model"),
            "provider": info.get("provider"),
        })

    for node, records in (update.get("evaluations") or {}).items():
        for record in records:
            emitter.emit({
                "event": "gate_evaluated", "node": node,
                "attempt": record["attempt"], "result": record["result"],
            })

    completed = update.get("completed_nodes") or ()
    if isinstance(completed, dict):
        completed = [key for key, present in completed.items() if present]
    for node in completed:
        emitter.emit({"event": "node_completed", "node": node})

    for node in update.get("failed_nodes") or []:
        error = ""
        # Fail-safe default: an unclassified failure reads as `node_error`
        # (deterministic → halt), never an accidental transient retry kind.
        kind = "node_error"
        for err in update.get("errors") or []:
            if err.get("node") == node:
                error = err.get("error", "")
                kind = err.get("kind", "node_error")
                break
        emitter.emit(
            {"event": "node_failed", "node": node, "error": error, "kind": kind}
        )

    for node, count in (update.get("retries") or {}).items():
        emitter.emit({"event": "node_retried", "node": node, "attempt": count})


class JsonlLogger:
    """Emits structured JSONL events to a file, one JSON object per line."""

    def __init__(self, dest: str | Path | IO[str], *, invocation_id: str | None = None) -> None:
        self._invocation_id = invocation_id
        self._node_starts: dict[str, float] = {}
        if isinstance(dest, (str, Path)):
            self._file: IO[str] = open(dest, "a")
            self._owns_file = True
        else:
            self._file = dest
            self._owns_file = False

    def close(self) -> None:
        if self._owns_file:
            self._file.close()

    def emit(self, event: dict[str, Any]) -> None:
        """Write a single event as a JSON line with a timestamp."""
        record = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        if self._invocation_id is not None:
            record["invocation_id"] = self._invocation_id
        kind = event.get("event")
        if kind in ("workflow_start", "workflow_end"):
            self._node_starts.clear()
        elif kind == "node_started":
            started_at = record.pop("_started_at", None)
            self._node_starts[event["node"]] = time.monotonic() if started_at is None else started_at
        elif kind in ("node_completed", "node_failed"):
            started_at = self._node_starts.pop(event["node"], None)
            if started_at is not None:
                record["duration_s"] = time.monotonic() - started_at
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()

    def log_update(self, update: dict[str, Any]) -> None:
        """Derive events from a LangGraph super-step partial update."""
        _emit_update_events(self, update)


class SubgraphLogger:
    """Prefix the node IDs on events from a child graph.

    Child loggers compose their prefixes, for example
    `paper::reconcile::step1`. They forward both direct events and
    events derived from state updates.
    """

    def __init__(self, base: "JsonlLogger | SubgraphLogger", prefix: str) -> None:
        self._base = base
        self._prefix = prefix

    def emit(self, event: dict[str, Any]) -> None:
        if "node" in event:
            event = {**event, "node": f"{self._prefix}::{event['node']}"}
        self._base.emit(event)

    def log_update(self, update: dict[str, Any]) -> None:
        # Same derivation logic as JsonlLogger; events flow through
        # this instance's prefixing emit().
        _emit_update_events(self, update)
