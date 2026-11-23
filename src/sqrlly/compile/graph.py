"""Top-level graph builder: YAML config → compiled LangGraph StateGraph."""

from __future__ import annotations

import logging
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from sqrlly.compile._manifest import (
    _read_manifest,
    item_hash,
    manifest_drift,
)
from sqrlly.compile.lint import iter_route_edges
from sqrlly.compile.dynamic import item_node, make_item_worker, make_item_join
from sqrlly.compile.nodes import (
    _make_execution_node,
    _make_gate_node,
    make_failure_update,
    all_deps_completed,
    is_frozen,
    check_dep_failed,
)
from sqrlly.runtime.result import ManifestError
from sqrlly.runtime.state import WorkflowState
from sqrlly.schema.models import Graph, Node, Settings

if TYPE_CHECKING:
    from sqrlly.runtime.result import NodeExecutor


def _detect_cycles(config: Graph) -> None:
    """Raise ``ValueError`` if ``depends_on`` edges form a cycle.

    Delegates to ``graphlib.TopologicalSorter`` — its ``prepare()``
    raises ``CycleError`` (carrying the offending cycle) on a cyclic
    graph. Edges to undeclared node ids are dropped; ``_validate_
    depends_on`` already rejects those at schema time.
    """
    node_ids = {n.id for n in config.nodes}
    adj = {
        n.id: {dep for dep in n.depends_on if dep in node_ids}
        for n in config.nodes
    }
    try:
        TopologicalSorter(adj).prepare()
    except CycleError as e:
        cycle = " → ".join(e.args[1])
        raise ValueError(f"Circular dependency detected: {cycle}") from e


def _wire_evaluation_pair(
    builder: StateGraph,
    node: Node,
    exec_id: str,
    pass_targets: list[str],
    config: Graph,
    *,
    executor: NodeExecutor | None = None,
    effective_settings: Settings | None = None,
) -> None:
    """Measure and route after the execution update reaches the checkpoint."""
    gate_id = f"_eval_{exec_id}"
    builder.add_edge(exec_id, gate_id)
    builder.add_node(
        gate_id,
        _make_gate_node(
            node, config, executor,
            exec_id=exec_id,
            pass_targets=pass_targets,
            effective_settings=effective_settings,
        ),
    )


def _fan_drift_command(
    node: Node,
    prior_children: "set[str] | None",
    new_child_ids: set[str],
    resume_skip: set[str],
    settings: Settings,
) -> "Command | None":
    """Fan-out ``--resume`` manifest-drift check, shared by the empty-manifest
    and per-item dispatch paths.

    ``prior_children`` is the frozen prior-run DIRECT branch id set for this
    parent (``None``/empty on a fresh run => no check). Returns a fail
    ``Command`` when the re-fan DROPPED a prior branch under
    ``on_manifest_drift='fail'``; otherwise ``None`` (no drift, or ``'warn'``
    logged and proceed). An empty ``new_child_ids`` (drained manifest on
    resume) is the maximal drift — every prior branch dropped. The drift is
    partitioned by the frozen skip: dropped ids in ``resume_skip`` completed
    last run (silent data loss); dropped ids not in skip were the
    failed/never-run children (orphaned).
    """
    if not prior_children:
        return None
    drifted = manifest_drift(prior_children, new_child_ids)
    if not drifted:
        return None
    vanished = sorted(c for c in drifted if c in resume_skip)
    orphaned = sorted(c for c in drifted if c not in resume_skip)
    msg = (
        f"fan-out {node.id!r}: manifest DRIFTED on --resume — {len(drifted)} "
        f"prior branch id(s) are absent from the re-fanned manifest, so "
        f"completed siblings {vanished!r} would silently vanish and "
        f"orphaned/failed children {orphaned!r} would never re-run "
        f"({len(new_child_ids)} fresh child(ren) would run in their place). "
        f"Give manifest items stable, deterministic 'id's across runs; set "
        f"'settings.on_manifest_drift: warn' to proceed anyway."
    )
    if settings.on_manifest_drift == "fail":
        return Command(update=make_failure_update(node.id, msg), goto=END)
    logging.getLogger(__name__).warning(
        "%s (on_manifest_drift=warn; proceeding)", msg
    )
    return None


def _fan_inputs(items, state, node, generation, recovering, executor, settings):
    parent = node.id
    previous = state.get("fan_inputs", {}).get(parent, {})
    prior_children = state.get("_fan_prior_children", {}).get(parent, ())
    frozen = state.get("_resume_skip", set())
    refresh = state.get("_refresh_items", False)
    inputs = {}
    inherited = set()
    for child, value in items:
        digest = item_hash(value)
        prior = previous.get(child, (None, 0))
        if recovering and child in prior_children:
            reuse = child in frozen and (not refresh or digest == prior[0])
            if reuse:
                inherited.add(child)
            # An explicitly rerun completed wrapper needs a new nested scope.
            rerun_workflow = (node.workflow and
                              state.get("fan_child_attempts", {}).get(child) == generation - 1)
            if reuse or (digest == prior[0] and not rerun_workflow):
                inputs[child] = prior
                continue
            if not refresh and digest != prior[0]:
                # A cached Git dispatch can predate this manifest edit.
                ambiguous = hasattr(executor, "may_replay_child") and executor.may_replay_child(child, node, settings)
                inputs[child] = (None if ambiguous else digest, prior[1])
                continue
        inputs[child] = (digest, generation if generation > 1 else 0)
    return inputs, inherited


def _make_fan_dispatcher(node: Node, dependents, settings: Settings, workflow_dir=None, executor=None):
    def dispatch(state: WorkflowState):
        if node.id in state.get("failed_nodes", set()):
            return Command(goto=END)
        if failure := check_dep_failed(node, state):
            return Command(update=failure, goto=END)
        if not all_deps_completed(node, state):
            return {}
        frozen = state.get("_resume_skip", set())
        if is_frozen(node.id, state):
            return Command(goto=dependents or END)
        attempt = state.get("attempts", {}).get(node.id, 0)
        baseline = state.get("_fan_resume_attempts", {}).get(node.id)
        recovering = attempt == baseline and state.get("required_attempts", {}).get(node.id, 0) <= attempt
        generation = attempt + 1
        try:
            if state.get("dry_run") and isinstance(node.for_each, str):
                items = []
            else:
                items = _read_manifest(state, node, workflow_dir=workflow_dir)
            inputs, inherited = _fan_inputs(items, state, node, generation, recovering, executor, settings)
        except ManifestError as exc:
            failure = make_failure_update(node.id, str(exc))
            if not recovering:
                failure["fan_children"] = {node.id: []}
            return Command(update=failure, goto=END)
        children = [child for child, _ in items]
        if recovering:
            drift = _fan_drift_command(node, state.get("_fan_prior_children", {}).get(node.id),
                                      set(children), frozen, settings)
            if drift:
                return drift
        if hasattr(executor, "set_fan_versions"):
            executor.set_fan_versions({child: version for child, (_, version) in inputs.items()})
        update = {"fan_children": {node.id: children}, "fan_ready": {node.id: False},
                  "attempts": {node.id: generation}, "fan_inputs": {node.id: inputs},
                  "fan_child_attempts": dict.fromkeys(inherited, generation)}
        previous = state.get("fan_children", {}).get(node.id, ())
        # A resumed edit retires dropped children. A new loop wave keeps earlier, distinct completions.
        invalidated = (set(previous) - inherited if recovering else
                       set(children).intersection(state.get("completed_nodes", set())))
        if invalidated:
            update["completed_nodes"] = dict.fromkeys(invalidated, False)
        sends = [
            Send(f"_sub_{node.id}", {**state, "_fan_child_id": child, "_fan_out_item": item,
                                     "_fan_attempt": generation, "_resume_skip": frozen - {child}})
            for child, item in items if child not in inherited
        ]
        return Command(update=update, goto=sends or f"_settle_{node.id}")
    return dispatch


def build_workflow_graph(
    config: Graph,
    executor: NodeExecutor | None = None,
    checkpointer: Any = None,
    *,
    logger: Any | None = None,
    effective_settings: Settings | None = None,
    entry: str | None = None,
    _depth: int = 0,
    _base_dir: Any = None,
) -> Any:
    """Build a compiled LangGraph StateGraph from workflow config.

    If `checkpointer` is provided, the compiled graph will persist state
    after each node via LangGraph's checkpointer protocol.

    If `logger` (a JsonlLogger) is provided, subgraph wrappers will
    stream their internal `astream(stream_mode="values")` output through
    a SubgraphLogger that prefixes node ids with the parent node's id,
    so subgraph-internal completions surface in the parent JSONL keyed
    as `parent_id::child_id` (and nested as `parent::child::grandchild`).

    Subgraphs inherit dispatch, timeout and retry settings from their parent.

    `_depth` and `_base_dir` are internal: subgraph wrappers pass
    `_depth+1` to enforce MAX_SUBGRAPH_DEPTH and propagate
    the base directory so nested config: paths resolve correctly.
    """
    # Deferred import to break the compile/subgraph ↔ compile/graph
    # circularity (subgraph imports build_workflow_graph via compile_fn).
    from sqrlly.compile.subgraph import (
        MAX_SUBGRAPH_DEPTH,
        SubgraphDepthError,
        detect_config_cycle,
        load_graph,
        make_subgraph_node,
    )

    settings = effective_settings or config.settings

    _detect_cycles(config)

    if _depth > MAX_SUBGRAPH_DEPTH:
        raise SubgraphDepthError(
            f"Subgraph nesting exceeded MAX_SUBGRAPH_DEPTH={MAX_SUBGRAPH_DEPTH}"
        )

    base_dir = config.workflow_dir or Path(_base_dir or ".")

    builder = StateGraph(WorkflowState)
    dependents = {node.id: [] for node in config.nodes}
    for node in config.nodes:
        for dep in node.depends_on:
            dependents[dep].append(node.id)

    def compile_fn(c, executor=None, _depth=0, effective_settings=None, logger=None):
        return build_workflow_graph(
            c, executor=executor, _depth=_depth,
            _base_dir=base_dir, logger=logger,
            effective_settings=effective_settings,
        )

    forward_targets = {target for _, _, target, back in iter_route_edges(config) if not back}
    command_sources = {n.id for n in config.nodes if n.eval or n.route or n.for_each}
    for node in config.nodes:
        downstream = dependents[node.id]
        build_subgraph = None
        if node.workflow:
            inputs = frozenset(node.workflow.inputs)
            if _depth == 0:
                detect_config_cycle(node.workflow.file, base_dir=base_dir, parent_settings=settings, inputs=inputs)
            child_config = load_graph(node.workflow.file, base_dir, settings, inputs=inputs)

            def build_subgraph(reference, child=child_config, repeated=bool(node.for_each)):
                return make_subgraph_node(
                    reference, child, compile_fn, executor, _depth, logger,
                    timeout=reference.effective_timeout(settings),
                    recover_settled=(_depth == 0 and repeated),
                )

        if node.for_each:
            builder.add_node(node.id, _make_fan_dispatcher(node, downstream, settings, config.workflow_dir, executor))
            worker = f"_sub_{node.id}"
            join = f"_settle_{node.id}"
            prototype = item_node(node, settings)
            builder.add_node(worker, make_item_worker(prototype, config, executor, settings, build_subgraph))
            builder.add_node(join, make_item_join(prototype, executor, settings, downstream))
            builder.add_edge(worker, join)
        else:
            body = build_subgraph(node) if node.workflow else _make_execution_node(
                node, config, executor, effective_settings=settings,
            )
            builder.add_node(node.id, body)
            if node.eval or node.route:
                _wire_evaluation_pair(builder, node, node.id, downstream or [END], config,
                                      executor=executor, effective_settings=settings)
            elif not downstream:
                builder.add_edge(node.id, END)

        for dep in node.depends_on:
            if dep not in command_sources:
                builder.add_edge(dep, node.id)
        if entry == node.id or (entry is None and not node.depends_on and node.id not in forward_targets):
            builder.add_edge(START, node.id)

    return builder.compile(checkpointer=checkpointer)
