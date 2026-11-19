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


def _strip_worktree(node: Node) -> Node:
    """Force a fan-out subgraph inner node onto the branch tree: clear its
    own worktree directives to `off` (node fields win in effective_worktree),
    so it runs in the branch worktree, not a per-inner-node tree."""
    return node.model_copy(update={"worktree": "off", "worktree_group": None})


class _BranchScopedExecutor:
    """Wraps the shared executor for one fan-out subgraph branch: every inner
    node is pinned to `branch_tree` (forced `off` + workdir). Duck-types the
    NodeExecutor Protocol; passes backend / get_worktree / worktree_map /
    reclaim / close through to the inner executor.

    `acquire_branch_worktree` is intentionally NOT forwarded: a fan-out nested
    inside a branch must share the outer branch tree (the outer branch is the
    isolation unit), so its `use_branch` check fails here and it runs in-tree."""

    def __init__(self, inner: "NodeExecutor", branch_tree: str) -> None:
        self._inner = inner
        self._branch_tree = branch_tree

    async def execute(
        self, node: Node, context: dict, workdir: str | None = None,
        settings_override: "Settings | None" = None,
    ) -> "ExecutionResult":
        # A caller-supplied workdir is intentionally ignored — the branch
        # tree always wins (that is the point of pinning the branch).
        return await self._inner.execute(
            _strip_worktree(node), context,
            workdir=self._branch_tree, settings_override=settings_override,
        )

    def get_backend(self, settings: Settings | None = None) -> object:
        return self._inner.get_backend(settings) if hasattr(self._inner, "get_backend") else None

    def get_worktree(self, node_id: str) -> "str | None":
        return self._inner.get_worktree(node_id) if hasattr(self._inner, "get_worktree") else None

    def worktree_map(self) -> dict:
        return self._inner.worktree_map() if hasattr(self._inner, "worktree_map") else {}

    async def reclaim(self) -> list:
        if hasattr(self._inner, "reclaim"):
            return await self._inner.reclaim()
        return []

    async def close(self) -> None:
        if hasattr(self._inner, "close"):
            await self._inner.close()


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
        sub_config, executor=executor, _depth=depth + 1,
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
    template_worktree: str | None = None,
) -> Any:
    """Per-Send-branch subgraph invoker for fan-out templates.

    Returns an async callable ``invoke(context, workdir, dry_run,
    prefix) -> ExecutionResult`` that the fan-out node body calls in
    place of ``executor.execute(...)``.

    When the effective worktree mode for this fan-out (per-fan-out
    ``template_worktree`` override, or parent scope's ``worktree`` if None)
    is not ``"off"`` and the executor has ``acquire_branch_worktree``,
    each Send branch gets its own isolated git worktree keyed by
    ``prefix`` (the child_id). Inner subgraph nodes are compiled against
    a ``_BranchScopedExecutor`` that pins every node to that tree (forced
    ``worktree: off`` + explicit workdir), so inner nodes never spin up
    their own trees. The branch worktree path is returned as
    ``ExecutionResult.worktree`` for upstream recording in
    ``node_worktrees``.

    Off/non-foreman paths fall through to ``workdir`` as before, and
    ``ExecutionResult.worktree`` is None (inline parity).

    The subgraph is compiled lazily inside ``invoke`` (not at factory
    build time) so each branch gets its own compiled graph wired to its
    own ``_BranchScopedExecutor``.

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

    # Gate: isolate iff the effective worktree mode for THIS fan-out is not
    # "off". A per-fan-out override (FanOutTemplate.worktree) wins over the
    # parent scope's settings.worktree; None inherits the parent scope.
    _effective_worktree = (
        template_worktree
        if template_worktree is not None
        else (parent_settings or Settings()).worktree
    )
    _isolate = _effective_worktree != "off"

    async def invoke(
        context: dict[str, Any], workdir: str, dry_run: bool,
        prefix: str | None = None,
    ) -> ExecutionResult:
        """``prefix`` (typically the per-Send child_id like
        ``reviewer_pool::maverick``) names the per-branch subgraph in
        the parent JSONL when ``logger`` is set."""
        # Determine branch worktree: acquire one per branch when isolated,
        # else fall back to the caller-supplied workdir.
        use_branch = prefix and _isolate and hasattr(executor, "acquire_branch_worktree")
        if use_branch:
            try:
                branch_tree = await executor.acquire_branch_worktree(prefix)
            except RuntimeError as e:
                # A subgraph fan-out BRANCH acquires its worktree here, not via
                # ForemanExecutor.execute — so the same transient-infra aborts
                # (git worktree add / worktree_share / worktree_setup) must be
                # caught here too, returned as this branch's `infra` failure
                # (the dynamic fan-out funnel forwards error_kind), so the graph
                # settles instead of the RuntimeError escaping as a traceback.
                return ExecutionResult(
                    success=False, error=str(e), error_kind="infra",
                )
        else:
            branch_tree = workdir

        # Wire the compiled subgraph to a branch-scoped executor that
        # pins every inner node to branch_tree (forced worktree:off +
        # explicit workdir), preventing inner nodes from spawning their
        # own per-node trees.
        branch_exec = (
            _BranchScopedExecutor(executor, branch_tree)
            if executor is not None else None
        )
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
            )
        return ExecutionResult(
            success=True,
            output=_terminal_node_output(sub_result, sub_config),
            worktree=branch_tree if use_branch else None,
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
