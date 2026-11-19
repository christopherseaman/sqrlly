"""Compile child workflows and project terminal results into their parent."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any


from sqrlly.compile._manifest import find_terminal_nodes
from sqrlly.compile.nodes import (
    all_deps_completed,
    build_context,
    check_dep_failed,
    make_failure_update,
)
from sqrlly.runtime.executor.prompt import render_template
from sqrlly.runtime.logging import SubgraphLogger
from sqrlly.runtime.result import ExecutionResult
from sqrlly.runtime.settings_merge import merge_settings
from sqrlly.runtime.state import WorkflowState, make_initial_state
from sqrlly.schema.models import Graph, Node, Settings, load_workflow

if TYPE_CHECKING:
    from sqrlly.runtime.result import NodeExecutor


class SubgraphCycleError(ValueError):
    """Raised when the config-reference DAG contains a cycle."""


# Cap on recursive subgraph nesting (Stage 4c). A fixed guard against
# accidental infinite recursion, not a tunable — real workflows nest
# two or three levels deep.
MAX_SUBGRAPH_DEPTH = 10


class SubgraphDepthError(ValueError):
    """Raised when subgraph nesting exceeds MAX_SUBGRAPH_DEPTH."""


def load_graph(
    config_path: str, base_dir: str | Path = ".", parent_settings: Settings | None = None,
) -> Graph:
    """Load and parse a Graph YAML file. Path is relative to base_dir."""
    resolve = (lambda child: merge_settings(parent_settings, child, Path(base_dir))) if parent_settings else None
    return load_workflow(Path(base_dir) / config_path, resolve_settings=resolve)


def _terminal_node_output(sub_state: dict[str, Any], sub_config: Graph) -> str:
    """Pick the subgraph's terminal node and return its output.

    A terminal node is one not depended on by any other node. If multiple
    terminals exist, picks the last one defined. Returns "" if subgraph
    has no node_outputs.
    """
    terminals = find_terminal_nodes(sub_config)
    if not terminals:
        return ""
    outputs = sub_state.get("node_outputs", {})
    return outputs.get(terminals[-1], "")


def make_subgraph_node(
    parent_node: Node,
    sub_config: Graph,
    compile_fn: Any,
    executor: "NodeExecutor | None",
    depth: int,
    logger: Any | None = None,
    timeout: float | None = None,
):
    """Create the wrapper async function added as the parent graph's node.

    The wrapper compiles the subgraph (passing depth+1 so cycles bottom
    out at MAX_SUBGRAPH_DEPTH), then on each invocation:
      1. Renders `inputs:` templates against parent context.
      2. Builds a fresh subgraph initial state with rendered inputs.
      3. Invokes the compiled subgraph.
      4. Returns the last terminal output to the parent.

    When ``logger`` is supplied, the wrapper streams subgraph state
    snapshots through a ``SubgraphLogger`` that prefixes node ids with
    the parent's id, so subgraph-internal completions surface in the
    parent JSONL keyed as ``parent_id::child_id``.

    """
    sub_logger = SubgraphLogger(logger, parent_node.id) if logger else None
    sub_graph = compile_fn(
        sub_config, executor=executor.scoped(parent_node.id) if hasattr(executor, "scoped") else executor, _depth=depth + 1,
        effective_settings=sub_config.settings, logger=sub_logger,
    )

    parent_id = parent_node.id
    inputs_decl = parent_node.workflow.inputs

    async def run_child(parent_state: WorkflowState) -> dict[str, Any]:
        parent_context = build_context(parent_node, parent_state)
        rendered_inputs = {
            k: render_template(v, parent_context) if isinstance(v, str) else v
            for k, v in inputs_decl.items()
        }

        sub_state = make_initial_state(
            workflow_name=sub_config.name,
            workdir=parent_state.get("workdir", "."),
            dry_run=parent_state.get("dry_run", False),
        )
        sub_state["node_inputs"] = rendered_inputs

        if sub_logger is not None:
            sub_result = sub_state
            async for chunk_type, payload in sub_graph.astream(
                sub_state, stream_mode=["updates", "values"],
            ):
                if chunk_type == "values":
                    sub_result = payload
                elif chunk_type == "updates":
                    for _name, update in payload.items():
                        sub_logger.log_update(update)
        else:
            sub_result = await sub_graph.ainvoke(sub_state)

        update: dict[str, Any] = {"completed_nodes": {parent_id}}
        if sub_result.get("retained_refs"):
            update["retained_refs"] = sub_result["retained_refs"]
        update["node_outputs"] = {parent_id: _terminal_node_output(sub_result, sub_config)}

        if sub_result.get("failed_nodes"):
            update["failed_nodes"] = {parent_id}
            update["errors"] = [{
                "node": parent_id,
                "error": (
                    f"subgraph '{sub_config.name}' had failed nodes: "
                    f"{sub_result['failed_nodes']}"
                ),
                # The real failure is an inner subgraph node (see the prefixed
                # `parent::inner` id in the same stream), not this reference node.
                "kind": "upstream_failed",
            }]
            update["completed_nodes"] = set()

        return update

    async def wrapper(parent_state: WorkflowState) -> dict[str, Any]:
        if (parent_id in parent_state.get("_resume_skip", set())
                or parent_id in parent_state.get("failed_nodes", set())):
            return {}
        # Dep-join: same semantics as a regular execution node. The
        # `completed_nodes` re-entry guard removed in audit fix #19
        # (Stage 5c) is intentionally absent here too — Command(goto=)
        # re-fires must re-execute the subgraph wrapper, not no-op.
        if (failure := check_dep_failed(parent_node, parent_state)) is not None:
            return failure
        if parent_node.depends_on and not all_deps_completed(
            parent_node, parent_state,
        ):
            return {}

        try:
            async with asyncio.timeout(timeout):
                return await run_child(parent_state)
        except TimeoutError:
            return make_failure_update(parent_id, f"Node timed out after {timeout}s", kind="timeout")

    wrapper.__name__ = f"subgraph_{parent_id}"
    return wrapper


def make_fan_out_subgraph_invoker(
    workflow_file: str,
    inputs_decl: dict[str, Any],
    compile_fn: Any,
    base_dir: str | Path,
    depth: int,
    executor: "NodeExecutor | None",
    logger: Any | None = None,
    parent_settings: Settings | None = None,
) -> Any:
    """Per-Send-branch subgraph invoker for fan-out templates.

    Returns an async callable ``invoke(context, workdir, dry_run,
    prefix) -> ExecutionResult`` that the fan-out node body calls in
    place of ``executor.execute(...)``.

    Each branch qualifies its inner node ids with the manifest item id.

    Inputs (``workflow.with``) render against the per-item
    context; the rendered map seeds the sub-invocation's
    ``node_inputs``. The subgraph's terminal-node output flows back as
    ``ExecutionResult.output`` so downstream gate evaluation, retries,
    and aggregation paths in dynamic.py stay unchanged.
    """
    # Cycle detection mirrors the recursive-subgraph path: only the
    # outermost build runs it (depth=0 callers), so nested fan-out
    # subgraphs don't re-walk the same chains.
    if depth == 0:
        detect_config_cycle(workflow_file, base_dir=base_dir, parent_settings=parent_settings)
    sub_config = load_graph(workflow_file, base_dir, parent_settings)

    async def invoke(
        context: dict[str, Any], workdir: str, dry_run: bool,
        prefix: str | None = None,
    ) -> ExecutionResult:
        """``prefix`` (typically the per-Send child_id like
        ``reviewer_pool::maverick``) names the per-branch subgraph in
        the parent JSONL when ``logger`` is set."""
        branch_tree = workdir
        branch_exec = executor.scoped(prefix) if prefix and hasattr(executor, "scoped") else executor
        sub_logger = SubgraphLogger(logger, prefix) if logger and prefix else None
        sub_compiled = compile_fn(
            sub_config, executor=branch_exec, _depth=depth + 1,
            effective_settings=sub_config.settings, logger=sub_logger,
        )

        rendered_inputs = {
            k: render_template(v, context) if isinstance(v, str) else v for k, v in inputs_decl.items()
        }
        sub_state = make_initial_state(
            workflow_name=sub_config.name,
            workdir=branch_tree,
            dry_run=dry_run,
        )
        sub_state["node_inputs"] = rendered_inputs

        try:
            if sub_logger is not None:
                sub_result = sub_state
                async for chunk_type, payload in sub_compiled.astream(
                    sub_state, stream_mode=["updates", "values"],
                ):
                    if chunk_type == "values":
                        sub_result = payload
                    elif chunk_type == "updates":
                        for _name, update in payload.items():
                            sub_logger.log_update(update)
            else:
                sub_result = await sub_compiled.ainvoke(sub_state)
        except Exception as e:
            return ExecutionResult(
                success=False,
                error=f"subgraph '{sub_config.name}' raised: {e}",
            )

        if sub_result.get("failed_nodes"):
            return ExecutionResult(
                success=False,
                error=(
                    f"subgraph '{sub_config.name}' had failed nodes: "
                    f"{sub_result['failed_nodes']}"
                ),
                # An inner branch-subgraph node is the real failure.
                error_kind="upstream_failed",
                retained_refs=sub_result.get("retained_refs", set()),
            )
        return ExecutionResult(
            success=True,
            output=_terminal_node_output(sub_result, sub_config),
            retained_refs=sub_result.get("retained_refs", set()),
        )

    return invoke


def node_subgraph_path(n: Node) -> str | None:
    return n.workflow.file if n.workflow is not None else None


def detect_config_cycle(
    config_path: str,
    visited: list[str] | None = None,
    base_dir: str | Path = ".",
    parent_settings: Settings | None = None,
) -> None:
    """Check ordinary and fan-out workflow references for ancestor cycles."""
    visited = list(visited or [])
    abs_path = str((Path(base_dir) / config_path).resolve())
    if abs_path in visited:
        chain = " -> ".join(visited + [abs_path])
        raise SubgraphCycleError(f"Subgraph reference cycle: {chain}")
    visited.append(abs_path)
    sub = load_graph(config_path, base_dir, parent_settings)
    for node in sub.nodes:
        steps = [node]
        if node.fan_out:
            if node.fan_out.template:
                steps.append(node.fan_out.template)
            steps.extend(node.fan_out.final_nodes)
        for step in steps:
            if step.workflow:
                detect_config_cycle(step.workflow.file, visited, Path(abs_path).parent, sub.settings)
