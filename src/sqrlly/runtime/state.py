from __future__ import annotations

import operator
from typing import Annotated, Any, Callable, NotRequired

from typing_extensions import TypedDict


def _merge_dicts(left: dict, right: dict) -> dict:
    merged = left.copy()
    merged.update(right)
    return merged


def _merge_worktrees(left: dict, right: dict) -> dict:
    """Remove settled paths after their worktrees are reclaimed."""
    return {key: path for key, path in (left | right).items() if path is not None}


def _merge_evaluations(
    left: dict[str, list[dict[str, Any]]],
    right: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Append-per-key reducer for `evaluations` — history grows, never replaces."""
    merged: dict[str, list[dict[str, Any]]] = {k: list(v) for k, v in left.items()}
    for key, new_records in right.items():
        merged.setdefault(key, []).extend(new_records)
    return merged


def _merge_sets(left: set[str], right: set[str]) -> set[str]:
    """Set-union reducer for `completed_nodes` / `failed_nodes`.

    O(1) membership for the `in completed_nodes` guards scattered through
    `compile/nodes.py` and `compile/dynamic.py`, and structurally prevents
    duplicate accumulation when a goto-driven re-fire (or `--resume`-driven
    replay) writes the same node id more than once.
    """
    return left | right


# Reducer table — single source of truth for how state fields combine.
# Mirrors WorkflowState's Annotated metadata; consumed by both LangGraph
# (via the TypedDict annotations) and `dynamic._merge_updates` (when the
# fan-out node accumulates state inline across its retry loop).
REDUCERS: dict[str, Callable[[Any, Any], Any]] = {
    "completed_nodes": _merge_sets,
    "fan_children": _merge_dicts,
    "fan_child_attempts": _merge_dicts,
    "fan_ready": _merge_dicts,
    "retained_refs": _merge_sets,
    "failed_nodes": _merge_sets,
    "errors": operator.add,
    "node_outputs": _merge_dicts,
    "node_structured_outputs": _merge_dicts,
    "retries": _merge_dicts,
    "attempts": _merge_dicts,
    "settled_attempts": _merge_dicts,
    "required_attempts": _merge_dicts,
    "route_context": _merge_dicts,
    "route_destinations": _merge_dicts,
    "child_outputs": _merge_dicts,
    "node_worktrees": _merge_worktrees,
    "node_models": _merge_dicts,
    "evaluations": _merge_evaluations,
}


class WorkflowState(TypedDict):
    workflow_name: str
    fan_ready: Annotated[dict[str, bool], REDUCERS["fan_ready"]]
    fan_children: Annotated[dict[str, list[str]], REDUCERS["fan_children"]]
    fan_child_attempts: Annotated[dict[str, int], REDUCERS["fan_child_attempts"]]
    retained_refs: Annotated[set[str], REDUCERS["retained_refs"]]
    completed_nodes: Annotated[set[str], REDUCERS["completed_nodes"]]
    failed_nodes: Annotated[set[str], REDUCERS["failed_nodes"]]
    node_outputs: Annotated[dict[str, Any], REDUCERS["node_outputs"]]
    node_structured_outputs: Annotated[dict[str, Any], REDUCERS["node_structured_outputs"]]
    evaluations: Annotated[dict[str, list[dict[str, Any]]], REDUCERS["evaluations"]]
    retries: Annotated[dict[str, int], REDUCERS["retries"]]
    child_outputs: Annotated[dict[str, Any], REDUCERS["child_outputs"]]
    node_worktrees: Annotated[dict[str, str], REDUCERS["node_worktrees"]]
    node_models: Annotated[dict[str, Any], REDUCERS["node_models"]]
    errors: Annotated[list[dict], REDUCERS["errors"]]
    workdir: str
    dry_run: bool
    _fan_child_id: NotRequired[str]
    _fan_attempt: NotRequired[int]
    workflow_route_context: NotRequired[dict[str, Any]]
    _fan_out_item: NotRequired[Any]
    # Subgraph context: rendered inputs visible as template vars to subgraph
    # nodes. Set by the subgraph wrapper before subgraph invocation; not
    # populated at the top level. Merged into build_context output.
    node_inputs: NotRequired[dict[str, str]]
    _cold_entry: NotRequired[str]
    settled_attempts: Annotated[dict[str, int], REDUCERS["settled_attempts"]]
    required_attempts: Annotated[dict[str, int], REDUCERS["required_attempts"]]
    attempts: Annotated[dict[str, int], REDUCERS["attempts"]]
    route_destinations: Annotated[dict[str, str | list[str] | None], REDUCERS["route_destinations"]]
    route_context: Annotated[dict[str, dict], REDUCERS["route_context"]]
    # Resume skip-set (skip-completed --resume). A FROZEN snapshot of node
    # ids that completed in the prior run and are safe to skip this run.
    # Seeded ONCE at resume entry from the prior checkpoint; never written by
    # a node body, so it persists unchanged across super-steps. Last-write-wins
    # (NO REDUCER — must not set-union-accumulate). Guards read it; nothing
    # mutates it. Absent on a fresh run => skip nothing.
    _resume_skip: NotRequired[set[str]]
    # Prior fan-out branch ids per parent, for the `--resume` manifest-drift
    # guard. `{parent_id: {"<parent>::<item>", ...}}` — the DIRECT branch ids
    # (completed | failed) from the prior checkpoint. Seeded ONCE at resume
    # entry (`cli/main.py::_seed_state`), only for parents that had children,
    # and NOT on fresh / --entry / --rerun-all. Read by the `_fan_<id>`
    # dispatcher to detect a re-fan that DROPS a prior branch. Frozen,
    # last-write-wins (NO REDUCER). Absent => no drift check.
    _fan_prior_children: NotRequired[dict[str, set[str]]]
    _fan_resume_attempts: NotRequired[dict[str, int]]


def make_initial_state(
    workflow_name: str = "Workflow",
    workdir: str = ".",
    dry_run: bool = False,
    **overrides: Any,
) -> dict[str, Any]:
    """Create initial state dict for graph invocation.

    Single source of truth for all state fields — used by CLI and tests.
    """
    state: dict[str, Any] = {
        "workflow_name": workflow_name,
        "completed_nodes": set(),
        "failed_nodes": set(),
        "node_outputs": {},
        "node_structured_outputs": {},
        "evaluations": {},
        "retries": {},
        "attempts": {},
        "settled_attempts": {},
        "required_attempts": {},
        "route_context": {},
        "route_destinations": {},
        "child_outputs": {},
        "node_worktrees": {},
        "node_models": {},
        "errors": [],
        "workdir": workdir,
        "dry_run": dry_run,
    }
    state.update(overrides)
    return state
