"""Compile child workflows and project terminal results into their parent."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any


from sqrlly.compile.outputs import execute_with_outputs
from sqrlly.compile._manifest import find_terminal_nodes
from sqrlly.compile.nodes import (
    count_attempts,
    run_with_timeout,
    all_deps_completed,
    is_frozen,
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
    *, inputs: frozenset[str] | None = None,
) -> Graph:
    """Load and parse a Graph YAML file. Path is relative to base_dir."""
    resolve = (lambda child: merge_settings(parent_settings, child, Path(base_dir))) if parent_settings else None
    return load_workflow(Path(base_dir) / config_path, resolve_settings=resolve, inputs=inputs)


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

    async def run_child(parent_state: WorkflowState, parent_context: dict) -> ExecutionResult:
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
        sub_state["workflow_route_context"] = {
            name: parent_context[name] for name in (
                "sender_id", "sender", "sender_structured", "sender_worktree", "_route_eval_preamble"
            ) if name in parent_context
        }
        if reason := parent_context.get("_output_retry_reason"):
            rendered_inputs["_output_retry_reason"] = reason
            rendered_inputs["_retry_reason"] = reason

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

        output = _terminal_node_output(sub_result, sub_config)
        retained = sub_result.get("retained_refs", set())
        if failures := sub_result.get("failed_nodes"):
            return ExecutionResult(success=False, output=output, error_kind="upstream_failed", retained_refs=retained,
                                   error=f"subgraph '{sub_config.name}' had failed nodes: {failures}")
        return ExecutionResult(output=output, retained_refs=retained)

    async def wrapper(parent_state: WorkflowState) -> dict[str, Any]:
        if (is_frozen(parent_id, parent_state)
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

        result, executions = await execute_with_outputs(
            lambda ctx: run_with_timeout(run_child(parent_state, ctx), timeout),
            build_context(parent_node, parent_state),
            None if parent_state.get("dry_run") else parent_node.outputs,
            parent_state.get("workdir", "."),
        )
        update = {"node_outputs": {parent_id: result.output},
                  "attempts": {parent_id: parent_state.get("attempts", {}).get(parent_id, 0) + executions}}
        if result.retained_refs:
            update["retained_refs"] = result.retained_refs
        if not result.success:
            update.update(make_failure_update(parent_id, result.error, result.error_kind))
        elif not (parent_node.eval or parent_node.route):
            update["completed_nodes"] = {parent_id}
        return update

    wrapper.__name__ = f"subgraph_{parent_id}"
    return count_attempts(parent_node, wrapper)


def node_subgraph_path(n: Node) -> str | None:
    return n.workflow.file if n.workflow is not None else None


def detect_config_cycle(
    config_path: str,
    visited: list[str] | None = None,
    base_dir: str | Path = ".",
    parent_settings: Settings | None = None,
    *, inputs: frozenset[str] | None = None,
) -> None:
    """Check ordinary and fan-out workflow references for ancestor cycles."""
    visited = list(visited or [])
    abs_path = str((Path(base_dir) / config_path).resolve())
    if abs_path in visited:
        chain = " -> ".join(visited + [abs_path])
        raise SubgraphCycleError(f"Subgraph reference cycle: {chain}")
    visited.append(abs_path)
    sub = load_graph(config_path, base_dir, parent_settings, inputs=inputs)
    for node in sub.nodes:
        if node.workflow:
            detect_config_cycle(node.workflow.file, visited, Path(abs_path).parent, sub.settings,
                                inputs=frozenset(node.workflow.inputs))
