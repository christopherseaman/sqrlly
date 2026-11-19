"""Top-level graph builder: YAML config → compiled LangGraph StateGraph."""

from __future__ import annotations

import json
import logging
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from sqrlly.compile._manifest import (
    _read_manifest,
    find_terminal_nodes,
    manifest_drift,
)
from sqrlly.compile.lint import iter_route_edges
from sqrlly.compile.dynamic import _make_final_fan_out_node, _make_fan_out_node, fan_child_node
from sqrlly.compile.nodes import (
    _make_execution_node,
    _make_gate_node,
    make_failure_update,
    settle_nodes,
)
from sqrlly.compile.subgraph import node_subgraph_path
from sqrlly.runtime.result import ManifestError
from sqrlly.runtime.state import WorkflowState
from sqrlly.schema.models import Graph, Node, Settings

if TYPE_CHECKING:
    from sqrlly.runtime.result import NodeExecutor


def _collect_route_targets(node):
    targets = set()
    for case in node.route or []:
        dest = case.then or case.otherwise
        targets.update(dest if isinstance(dest, list) else [dest])
    return targets - {"retry", "fail"}


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


def _make_fan_dispatcher(
    node: Node, no_items_targets: list[str], settings: Settings,
):
    """Build the ``_fan_<id>`` dispatcher node body for a fan-out parent.

    Reached only by ``Command(goto=_fan_<id>)`` from the gate (gated
    parent) or a plain edge (ungated parent). Returns ``Command(goto=...)``:
    ``END`` on parent failure, a plain ``{}`` defer while the parent is
    unsettled, ``Command(goto=no_items_targets)`` + WARN on an empty
    manifest, a ``make_failure_update`` Command on a `_read_manifest`
    ``ManifestError`` (unreadable / invalid-JSON / mis-shaped
    ``manifest_path``) or colliding child ids (NOT a raise — failing the
    node lets bare ``--resume`` dirty the parent and re-fan with a fresh
    manifest, rather than leaving the parent completed-but-not-failed and
    re-failing deterministically), and the per-item ``Send`` array
    otherwise. ``state`` here is committed post-gate state, so Send payloads
    need no update baking.
    """
    template_node_id = f"_sub_{node.id}"

    def dispatcher(state: WorkflowState):
        # 1. Parent failed → END, no fan-out.
        if node.id in state.get("failed_nodes", set()):
            return Command(goto=END)

        # 2. Parent not settled yet: defer. Safer than a pre-emptive fire —
        #    LangGraph re-fires _fan_ when the parent settles. The final-node
        #    barrier's case-3 guard stays as belt-and-braces.
        settled = state.get("completed_nodes", set()) | state.get(
            "failed_nodes", set()
        )
        if node.id not in settled:
            return {}

        # A ManifestError (unreadable / invalid-JSON / mis-shaped
        # manifest_path) FAILS the parent node rather than escaping as a
        # raise: the gate committed completed_nodes a super-step earlier, so a
        # raise would leave the parent completed-but-not-failed in the
        # checkpoint and bare --resume would freeze it and re-fail forever.
        # Failing the node puts it in failed_nodes → dirty on --resume → re-fan.
        try:
            items = _read_manifest(state, node)
        except ManifestError as e:
            return Command(
                update=make_failure_update(node.id, str(e)),
                goto=END,
            )

        # 3. Empty manifest → route to the no-items target(s). Concrete ids
        #    (not a route_map key) so an empty manifest can fan to EVERY
        #    dependent; the list may contain END. BUT on --resume with prior
        #    children, an empty re-fan is the MAXIMAL drift (every prior branch
        #    dropped, the failed child orphaned) — run it through the drift
        #    policy BEFORE the no-items short-circuit, else fail/warn is
        #    bypassed and the run silently completes green.
        if not items:
            drift_cmd = _fan_drift_command(
                node, state.get("_fan_prior_children", {}).get(node.id),
                set(), state.get("_resume_skip", set()), settings,
            )
            if drift_cmd is not None:
                return drift_cmd
            logging.getLogger(__name__).warning(
                "fan-out %r: manifest resolved to zero items — routing to "
                "no-items target(s) %s instead of fanning out.",
                node.id, no_items_targets,
            )
            # Emit the concrete-id list as-is (may contain END). Returning a
            # list — not an unwrapped scalar — is what lets an empty manifest
            # fan to EVERY dependent.
            return Command(update={"fan_ready": {node.id: True}}, goto=list(no_items_targets))

        # 4. Fail loud on colliding child ids. Each branch's child id is
        #    `<parent>::<item_id>` (matching dynamic._make_fan_out_node). Two
        #    items mapping to one id (literal duplicate, or ≥2 id-less dict
        #    items collapsing onto `::unknown`) would silently merge into one
        #    branch. Fail the node (not raise) so the run ends failed and bare
        #    --resume dirties the parent → re-fan with a fresh manifest.
        seen: set[str] = set()
        duplicates: set[str] = set()
        for item in items:
            child_id = f"{node.id}::{item.get('id', 'unknown')}"
            if child_id in seen:
                duplicates.add(child_id)
            seen.add(child_id)
        if duplicates:
            return Command(
                update=make_failure_update(
                    node.id,
                    f"fan-out {node.id!r}: manifest produces duplicate child "
                    f"id(s) {sorted(duplicates)!r} — each item needs a unique "
                    f"'id' (id-less items collapse onto '{node.id}::unknown').",
                ),
                goto=END,
            )

        # 4b. Manifest-drift guard (--resume only) — see _fan_drift_command.
        #     `_fan_prior_children` is seeded ONLY on resume; absent => no-op.
        drift_cmd = _fan_drift_command(
            node, state.get("_fan_prior_children", {}).get(node.id),
            seen, state.get("_resume_skip", set()), settings,
        )
        if drift_cmd is not None:
            return drift_cmd

        # 5. Dispatch one Send per item. state is committed post-gate state,
        #    so children see merged completed_nodes / evaluations directly.
        return Command(update={"fan_children": {node.id: [f"{node.id}::{item.get('id', 'unknown')}" for item in items]}}, goto=[
            Send(template_node_id, {**state, "_fan_out_item": item})
            for item in items
        ])

    dispatcher.__name__ = f"fan_{node.id}"
    return dispatcher


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
    terminal_ids = set(find_terminal_nodes(config))
    node_map = {p.id: p for p in config.nodes}

    # Reverse dependency map: dependents[x] = ids declaring `x` in their
    # depends_on, in declaration order. Built once instead of an O(N)
    # rescan of config.nodes per gated/dynamic node below.
    dependents: dict[str, list[str]] = {n.id: [] for n in config.nodes}
    for p in config.nodes:
        for dep in p.depends_on:
            if dep in dependents:
                dependents[dep].append(p.id)

    gated_node_ids: set[str] = set()
    dynamic_fan_out_ids: set[str] = set()
    subgraph_node_ids: set[str] = set()
    for node in config.nodes:
        if node.eval or node.route:
            gated_node_ids.add(node.id)
        if node.fan_out is not None:
            dynamic_fan_out_ids.add(node.id)
        if node_subgraph_path(node) is not None:
            subgraph_node_ids.add(node.id)
            # Cycle detection happens once at top-level — nested calls
            # see _depth>0 so they skip this and rely on the depth cap.
            if _depth == 0:
                detect_config_cycle(node.workflow.file, base_dir=base_dir, parent_settings=settings)
    # ----- Node registration -----

    # compile_fn is shared by recursive subgraph machinery and per-child
    # fan-out subgraphs: both compile a referenced YAML at parent build
    # time and invoke it later. Defined once so call sites don't drift.
    # `logger` propagates so nested subgraphs keep emitting prefixed events.
    # ``effective_settings`` (Phase 3) is the pre-merged scope view from
    # the subgraph wrapper; threaded into the recursive call so subgraph
    # nodes receive their own scope's settings.
    def compile_fn(c, executor=None, _depth=0, effective_settings=None, logger=None):
        return build_workflow_graph(
            c, executor=executor, _depth=_depth,
            _base_dir=base_dir, logger=logger,
            effective_settings=effective_settings,
        )

    def build_subgraph(node):
        child = load_graph(node.workflow.file, base_dir, settings, inputs=frozenset(node.workflow.inputs))
        return make_subgraph_node(
            node, child, compile_fn, executor, _depth, logger,
            timeout=node.effective_timeout(settings),
        )

    # Execution nodes for every configured node.
    for node in config.nodes:
        if node.id in subgraph_node_ids:
            builder.add_node(node.id, build_subgraph(node))
        else:
            builder.add_node(
                node.id,
                _make_execution_node(
                    node, config, executor,
                    effective_settings=settings,
                ),
            )

    # Dynamic node child template + final nodes.
    final_node_ids: dict[tuple[str, str], str] = {}
    gated_final_ids: set[str] = set()
    # Lookup the per-id Node object used for gate construction. Populated
    # for gated finals (synthetic Node mirrors the FanOutFinalNode config);
    # top-level gated nodes look up directly via node_map.
    final_synthetic_nodes: dict[str, Node] = {}
    for node_id in dynamic_fan_out_ids:
        node = node_map[node_id]
        builder.add_node(
            f"_sub_{node.id}",
            _make_fan_out_node(
                node, config, executor,
                compile_fn=compile_fn, base_dir=base_dir, depth=_depth,
                logger=logger,
                effective_settings=settings,
            ),
        )

        for idx, final_node in enumerate(node.fan_out.final_nodes):
            fid = f"_final_{node.id}_{final_node.id}"
            final_node_ids[(node.id, final_node.id)] = fid
            builder.add_node(
                fid, _make_final_fan_out_node(
                    node, final_node, config, executor, is_first=(idx == 0),
                    build_subgraph=build_subgraph,
                    effective_settings=settings,
                ),
            )
            if final_node.eval or final_node.route:
                gated_final_ids.add(fid)
                # Synthetic Node mirrors the FanOutFinalNode; its gate is
                # registered as ``_eval_<fid>`` by _wire_evaluation_pair
                # when the final chain is wired below.
                final_synthetic_nodes[fid] = Node(
                    id=fid, name=final_node.name,
                    eval=final_node.eval, route=final_node.route, depends_on=[node.id],
                )
                final_synthetic_nodes[fid]._ancestors = [*node._ancestors, node.id]

    # ----- exit_node: what downstream deps plain-edge from -----
    # For gated nodes, downstream waits on the eval node, not execution.

    exit_node: dict[str, str] = {}
    needs_conditional: set[str] = set()

    for node in config.nodes:
        if node.id in dynamic_fan_out_ids:
            dsc = node.fan_out
            if dsc.final_nodes:
                last_final_id = final_node_ids[(node.id, dsc.final_nodes[-1].id)]
                exit_node[node.id] = (
                    f"_eval_{last_final_id}"
                    if last_final_id in gated_final_ids
                    else last_final_id
                )
            else:
                exit_node[node.id] = f"_settle_{node.id}"
            needs_conditional.add(node.id)
        elif node.id in gated_node_ids:
            exit_node[node.id] = f"_eval_{node.id}"
            needs_conditional.add(node.id)
        else:
            exit_node[node.id] = node.id

    # ----- Plain edges: start + dep-to-node -----

    # Back edges may return to a root. Only forward routes suppress its START edge.
    route_goto_targets = {target for _, _, target, back_edge in iter_route_edges(config) if not back_edge}
    for node in config.nodes:
        if node.fan_out:
            for step in [node.fan_out.template, *node.fan_out.final_nodes]:
                route_goto_targets |= _collect_route_targets(step)

    has_incoming: set[str] = set()

    for node in config.nodes:
        if not node.depends_on:
            continue

        for dep in node.depends_on:
            if dep in needs_conditional:
                # Conditional edge from the dep (or its eval) handles routing.
                pass
            else:
                builder.add_edge(exit_node[dep], node.id)
            has_incoming.add(node.id)

    if entry is not None:
        builder.add_edge(START, entry)
    else:
        for node in config.nodes:
            if node.id not in has_incoming and node.id not in route_goto_targets:
                builder.add_edge(START, node.id)

    # ----- Top-level gated node wiring (non-dynamic) -----

    for node in config.nodes:
        if node.id not in gated_node_ids or node.id in dynamic_fan_out_ids:
            continue
        _wire_evaluation_pair(
            builder, node, node.id, dependents[node.id] or [END], config,
            executor=executor, effective_settings=settings,
        )

    # ----- Dynamic node wiring -----

    for node_id in dynamic_fan_out_ids:
        node = node_map[node_id]
        dsc = node.fan_out
        template_id = f"_sub_{node.id}"
        fan_id = f"_fan_{node.id}"

        settle_id = f"_settle_{node.id}"

        def settle_fan(state: WorkflowState, parent=node):
            children = state.get("fan_children", {}).get(parent.id, [])
            failed = state.get("failed_nodes", set())
            if any(child in failed for child in children):
                return make_failure_update(parent.id, "Fan-out child failed", kind="upstream_failed")
            if not all(child in state.get("completed_nodes", set()) for child in children):
                return {}
            update = settle_nodes(executor, fan_child_node(parent, settings), settings, children)
            if update.get("failed_nodes"):
                return update
            update["fan_ready"] = {parent.id: True}
            # Routed destinations may read promoted files once every branch has settled.
            routes = state.get("route_destinations", {})
            targets = []
            for child in children:
                target = routes.get(child)
                if target:
                    targets.extend(target if isinstance(target, list) else [target])
            if targets:
                return Command(update=update, goto=list(dict.fromkeys(targets)))
            return update

        builder.add_node(settle_id, settle_fan)
        builder.add_edge(template_id, settle_id)
        # _sub_ → first_final (child evaluates inline per branch).
        # Final chain: each gated final is followed by its eval with retry
        # self-loop; ungated finals get a plain edge to the next in line.
        if dsc.final_nodes:
            builder.add_edge(settle_id, final_node_ids[(node.id, dsc.final_nodes[0].id)])
            for i in range(len(dsc.final_nodes) - 1):
                cur = final_node_ids[(node.id, dsc.final_nodes[i].id)]
                nxt = final_node_ids[(node.id, dsc.final_nodes[i + 1].id)]
                if cur in gated_final_ids:
                    _wire_evaluation_pair(
                        builder, final_synthetic_nodes[cur], cur, [nxt],
                        config, executor=executor, effective_settings=settings,
                    )
                else:
                    builder.add_edge(cur, nxt)

        # Branch exit → parent's dependents.
        deps_of = dependents[node.id]
        last_final = (
            final_node_ids[(node.id, dsc.final_nodes[-1].id)]
            if dsc.final_nodes else None
        )
        if last_final and last_final in gated_final_ids:
            _wire_evaluation_pair(
                builder, final_synthetic_nodes[last_final], last_final,
                deps_of or [END], config,
                executor=executor, effective_settings=settings,
            )
        else:
            for tgt in (deps_of or [END]):
                builder.add_edge(exit_node[node.id], tgt)

        # Empty manifests continue at the first final node or the parent dependents.
        if dsc.final_nodes:
            no_items_targets = [f"_final_{node.id}_{dsc.final_nodes[0].id}"]
        else:
            no_items_targets = deps_of or [END]
        builder.add_node(
            fan_id, _make_fan_dispatcher(node, no_items_targets, settings)
        )

        if node.id in gated_node_ids:
            _wire_evaluation_pair(
                builder, node, node.id, [fan_id], config,
                executor=executor, effective_settings=settings,
            )
        else:
            # Ungated parent: plain edge parent → _fan_<id> (the dispatcher
            # defers until the parent settles).
            builder.add_edge(node.id, fan_id)

    # Nodes with an evaluation or route dispatch their own terminal command.
    for node in config.nodes:
        if (
            node.id in terminal_ids
            and node.id not in gated_node_ids
            and node.id not in dynamic_fan_out_ids
        ):
            builder.add_edge(node.id, END)

    return builder.compile(checkpointer=checkpointer)
