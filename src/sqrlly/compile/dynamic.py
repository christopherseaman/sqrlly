"""Run each manifest item independently and settle its outputs in manifest order."""

from __future__ import annotations

import json

from langgraph.graph import END
from langgraph.types import Command

from sqrlly.compile.nodes import _make_execution_node, make_failure_update, settle, settle_nodes
from sqrlly.runtime.state import REDUCERS, WorkflowState
from sqrlly.schema.models import Graph, Node, Settings


def _merge_updates(base: dict, extra: dict) -> dict:
    result = dict(base)
    for key, value in extra.items():
        result[key] = REDUCERS[key](result[key], value) if key in result and key in REDUCERS else value
    return result


def item_node(parent: Node, settings: Settings) -> Node:
    options = {"for_each": None, "depends_on": []}
    if parent.kind in ("agent", "run"):
        options["worktree"] = parent.effective_worktree(settings, fan_child=True)
    prototype = parent.model_copy(update=options)
    prototype._ancestors = parent._ancestors or parent.depends_on

    return prototype


def make_item_worker(prototype: Node, config: Graph, executor, settings: Settings, build_subgraph):
    async def worker(state: WorkflowState):
        child_id = state["_fan_child_id"]
        if hasattr(executor, "start_fan_child"):
            executor.start_fan_child(child_id)
        child = prototype.model_copy(update={"id": child_id})
        body = (build_subgraph(child) if child.workflow else _make_execution_node(
            child, config, executor, effective_settings=settings, defer_promotion=True,
        ))
        # Keep Send's item local through retries. Siblings merge only after returning.
        incoming = state.get("route_context", {}).get(prototype.id)
        current_routes = dict(state.get("route_context", {}))
        if incoming is None:
            current_routes.pop(child_id, None)
        else:
            current_routes[child_id] = incoming
        state = {**state, "route_context": current_routes}
        update = {}
        while True:
            current = _merge_updates(state, update)
            execution = await body(current)
            update = _merge_updates(update, execution)
            if child_id in execution.get("failed_nodes", set()):
                return update
            if child.eval and not state.get("dry_run"):
                decision, destination = await settle(
                    child, _merge_updates(state, update), settings, executor, authored_id=prototype.id,
                )
                update = _merge_updates(update, decision)
                if destination == "retry":
                    continue
                if destination == "fail":
                    return update
            update["completed_nodes"] = {child_id}
            update["fan_child_attempts"] = {child_id: state["_fan_attempt"]}
            return update

    return worker


def make_item_join(parent: Node, executor, settings: Settings, dependents):
    def join(state: WorkflowState):
        children = state["fan_children"][parent.id]
        failed = state.get("failed_nodes", set())
        if any(child in failed for child in children):
            return Command(update=make_failure_update(parent.id, "for_each child failed", "upstream_failed"), goto=END)
        completed = state.get("completed_nodes", set())
        if not all(child in completed for child in children):
            return {}
        update = settle_nodes(executor, parent, settings, children)
        if update.get("failed_nodes"):
            return Command(update=update, goto=END)
        structured = state.get("node_structured_outputs", {})
        raw = state.get("node_outputs", {})
        values = [structured[child] if child in structured else raw[child] for child in children]
        update.update(
            node_outputs={parent.id: json.dumps(values, ensure_ascii=False)},
            node_structured_outputs={parent.id: values},
            completed_nodes={parent.id}, fan_ready={parent.id: True},
            settled_attempts={parent.id: state["attempts"][parent.id]},
        )
        return Command(update=update, goto=dependents or END)

    return join
