"""Node factories for dynamic children (fan-out via Send)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from sqrlly.compile.nodes import (
    _make_execution_node,
    build_context,
    settle,
    run_with_timeout,
    make_failure_update,
)
from sqrlly.runtime.executor.prompt import retry_delay
from sqrlly.runtime.result import ExecutionResult
from sqrlly.runtime.state import REDUCERS, WorkflowState
from sqrlly.schema.models import Node, Graph, Settings

if TYPE_CHECKING:
    from sqrlly.runtime.result import NodeExecutor


def _merge_updates(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge `extra` into `base` using the same reducers LangGraph uses.

    The fan-out node accumulates state inline across its retry loop; once
    it returns, LangGraph's super-step reducers fold the aggregate into
    prior state. Using `state.REDUCERS` here keeps the inline merge
    semantically identical to the boundary merge — single source of truth.
    Keys not in REDUCERS (e.g. `_fan_out_item`) overwrite by assignment.
    """
    out = dict(base)
    for key, value in extra.items():
        if key in out and key in REDUCERS:
            out[key] = REDUCERS[key](out[key], value)
        else:
            out[key] = value
    return out


def _make_fan_out_node(
    parent_node: Node,
    config: Graph,
    executor: NodeExecutor | None,
    *,
    compile_fn: Any,
    base_dir: Any,
    depth: int,
    logger: Any | None = None,
    effective_settings: Settings | None = None,
):
    """Create a template node function for dynamic children.

    Each invocation receives a different ``_fan_out_item`` via LangGraph's
    Send. Gated templates run their retry loop **inline** inside this
    node body — LangGraph merges Send-dispatched branches at any
    conditional-edge boundary, which would strip per-branch
    ``_fan_out_item`` state and break graph-level retry self-loops.
    Inline retry preserves per-item state trivially.

    When the template declares a workflow, the
    template is a subgraph reference. The Stage-5b carve-out: each
    Send branch invokes the compiled subgraph with the per-item context
    rendered into ``inputs:``, and the subgraph's terminal output flows
    back as if it were a single-node executor result. That lets gates,
    retries, and aggregation all work uniformly across "template runs a
    prompt" and "template runs a multi-step subgraph."
    """
    settings = effective_settings or config.settings
    template = parent_node.fan_out.template
    timeout = parent_node.effective_timeout(settings)
    retry_backoff = settings.retry_backoff

    from sqrlly.compile.subgraph import make_fan_out_subgraph_invoker

    sub_invoker = None
    if template.workflow is not None:
        sub_invoker = make_fan_out_subgraph_invoker(
            template.workflow.file,
            template.workflow.inputs,
            compile_fn=compile_fn,
            base_dir=base_dir,
            depth=depth,
            executor=executor,
            logger=logger,
            parent_settings=settings,
            template_worktree=template.worktree,
        )

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        item = state.get("_fan_out_item", {})
        item_id = item.get("id", "unknown")
        child_id = f"{parent_node.id}::{item_id}"

        if child_id in state.get("failed_nodes", set()):
            return {}

        # Resume skip: freeze a child purely on its OWN id being in the frozen
        # snapshot. Only ids that completed cleanly last run are in the
        # snapshot, so a completed child stays frozen (not re-billed) even when
        # a sibling failed and the parent re-fans. The formerly-failed child is
        # never in prior_completed → never in skip → it runs. Stable-id-safe: a
        # re-fan that drifts the manifest yields new ids absent from skip.
        skip = state.get("_resume_skip")
        if skip and child_id in skip:
            return {}

        if state.get("dry_run", False):
            return {
                "node_outputs": {child_id: f"[dry-run] child {item_id}"},
                "child_outputs": {child_id: f"[dry-run] child {item_id}"},
                "completed_nodes": {child_id},
            }

        if executor is None:
            return {
                "node_outputs": {
                    child_id: f"[no-executor] child {item_id}"
                },
                "child_outputs": {
                    child_id: f"[no-executor] child {item_id}"
                },
                "completed_nodes": {child_id},
            }

        synthetic_node = Node(
            id=child_id,
            name=f"{parent_node.name} - {item.get('name', item_id)}",
            eval=template.eval, route=template.route,
            **({template.kind: getattr(template, template.kind)} if template.kind else {}),
            # Per-fan-out worktree override (FanOutTemplate.worktree). None
            # inherits settings.worktree — identical to a top-level node with
            # no worktree set. Node-mode-wins-over-settings in
            # effective_worktree applies the override at the foreman gate.
            worktree=template.worktree,
        )

        # Build context up front — dep outputs + per-item manifest fields
        # do not change across retries within this node invocation.
        base_context = build_context(parent_node, state)
        parent_output = state.get("node_outputs", {}).get(parent_node.id, "")
        base_context[parent_node.id] = parent_output
        for key, value in item.items():
            base_context[key] = str(value)

        # Simulated state that accumulates inline across retries within this
        # branch. Starts from the caller-provided state so reducers at the
        # super-step boundary see the whole history.
        update: dict[str, Any] = {}
        while True:
            current = _merge_updates(state, update)
            context = {**base_context, **build_context(synthetic_node, current)}
            retries = current.get("retries", {}).get(child_id, 0)
            if retries:
                await asyncio.sleep(retry_delay(retries, retry_backoff))

            if sub_invoker is not None:
                # Subgraph template: each Send branch runs the subgraph
                # with rendered inputs and surfaces its terminal output
                # the same way an executor would surface stdout / prompt
                # text. Timeout enforcement on the per-child subgraph
                # uses asyncio.wait_for at the same boundary as the
                # executor case for symmetry. The branch's child_id
                # (e.g. ``reviewer_pool::maverick``) becomes the JSONL
                # prefix so each per-Send subgraph's internals surface
                # under their own namespace.
                exec_result = await run_with_timeout(
                    sub_invoker(
                        context,
                        state.get("workdir", "."),
                        state.get("dry_run", False),
                        prefix=child_id,
                    ),
                    timeout,
                )
            else:
                exec_result = await run_with_timeout(
                    executor.execute(
                        synthetic_node, context,
                        settings_override=effective_settings,
                    ),
                    timeout,
                )
            update["attempts"] = {child_id: current.get("attempts", {}).get(child_id, 0) + 1}
            if not exec_result.success:
                return _merge_updates(update, make_failure_update(
                    child_id, exec_result.error,
                    kind=exec_result.error_kind or "node_error",
                ))

            exec_update: dict[str, Any] = {
                "node_outputs": {child_id: exec_result.output},
                "child_outputs": {child_id: exec_result.output},
            }
            if exec_result.worktree:
                exec_update["node_worktrees"] = {child_id: exec_result.worktree}
            update = _merge_updates(update, exec_update)

            if not (template.eval or template.route):
                update.setdefault("completed_nodes", set()).add(child_id)
                return update

            settlement, target = await settle(
                synthetic_node, _merge_updates(state, update), settings, executor, context,
            )
            update = _merge_updates(update, settlement)
            if target != "retry":
                if target and target != "fail":
                    from langgraph.types import Command
                    return Command(update=update, goto=target)
                return update

    node_fn.__name__ = f"branch_{parent_node.id}"
    return node_fn


def _make_final_fan_out_node(
    parent_node: Node,
    final_node,
    config: Graph,
    executor: NodeExecutor | None = None,
    *,
    is_first: bool = False,
    build_subgraph=None,
    effective_settings: Settings | None = None,
):
    """Create a node function for a final node in a dynamic child group.

    Branch aggregates reach the final node through `build_context`'s
    suffix synthesis (same mechanism any non-final downstream uses).

    The FIRST final node in the chain has an incoming static edge from
    `_sub_<parent>`. With LangGraph's Send-dispatch semantics, that edge
    fires once per Send branch — i.e. once per fan-out child. Without a
    barrier, the first final would dispatch on the FIRST child's
    completion, before sibling children land in `child_outputs`, so its
    `{{<parent>_branches}}` template var would render against an
    incomplete state. The wrapper below short-circuits with `{}` until
    all expected manifest items have completed.

    Subsequent finals chain through the prior final and don't need the
    barrier (their predecessor already ran with full state).
    """
    node_id = f"_final_{parent_node.id}_{final_node.id}"

    synthetic = Node(
        id=node_id,
        name=final_node.name,
        description=final_node.description,
        eval=final_node.eval, route=final_node.route,
        depends_on=[parent_node.id],
        **({final_node.kind: getattr(final_node, final_node.kind)} if final_node.kind else {}),
        output_contract=final_node.output_contract,
    )

    inner = build_subgraph(synthetic) if synthetic.workflow else _make_execution_node(
        synthetic, config, executor,
        effective_settings=effective_settings,
    )
    inner.__name__ = f"final_{parent_node.id}_{final_node.id}"

    if not is_first:
        return inner

    # First-final barrier: wait until every manifest item's child has
    # landed in completed_nodes (or failed_nodes — failures count as
    # "settled" so we don't wait forever on a hung child).
    from sqrlly.compile._manifest import _read_manifest

    parent_id = parent_node.id

    async def barrier(state: WorkflowState) -> dict[str, Any]:
        # Defer (return {}) in any of three cases:
        #   1. already done — LangGraph re-fires the node every super-step
        #      until termination; without this skip, inner runs twice.
        #   2. items known but not all settled — fan-out children still
        #      in flight; wait for them.
        #   3. items empty AND parent hasn't run yet — LangGraph fires
        #      conditional edges pre-emptively (when an upstream-of-parent
        #      completes), routing 'no_items' here before the parent's
        #      prompt produces a manifest. Empty items here means "unknown",
        #      not "zero children".
        completed = state.get("completed_nodes", set())
        failed = state.get("failed_nodes", set())
        items = _read_manifest(state, parent_node)
        settled = set(completed) | set(failed)
        children_pending = items and any(
            f"{parent_id}::{it.get('id', 'unknown')}" not in settled for it in items
        )
        parent_unsettled = parent_id not in completed and parent_id not in failed
        should_defer = (
            node_id in completed
            or children_pending
            or (not items and parent_unsettled)
        )
        if should_defer:
            return {}
        return await inner(state)

    barrier.__name__ = f"final_{parent_node.id}_{final_node.id}"
    return barrier
