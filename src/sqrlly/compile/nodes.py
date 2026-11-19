"""Execute node bodies and settle their ordered route cases."""

from __future__ import annotations

import asyncio
import subprocess
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from langgraph.graph import END
from langgraph.types import Command

from sqrlly.compile.evaluation import select_case
from sqrlly.compile.lint import iter_route_edges
from sqrlly.compile.outputs import execute_with_outputs
from sqrlly.runtime.gates import build_eval_preamble
from sqrlly.runtime.executor.prompt import retry_delay
from sqrlly.runtime.gates import run_evaluation
from sqrlly.runtime.result import EvaluationBackendError, EvaluationError, ExecutionResult
from sqrlly.runtime.state import WorkflowState
from sqrlly.schema.models import Node, Settings, Graph

if TYPE_CHECKING:
    from sqrlly.runtime.result import NodeExecutor


def settle_nodes(executor, node: Node, settings: Settings, ids: list[str]) -> dict:
    if not hasattr(executor, "settle"):
        return {}
    try:
        retained = executor.settle(node, settings, ids)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        return make_failure_update(node.id, f"Snapshot settlement failed: {exc}")
    update = {"node_worktrees": dict.fromkeys(ids)}
    if retained:
        update["retained_refs"] = retained
    return update


def is_frozen(node_id: str, state: WorkflowState) -> bool:
    required = state.get("required_attempts", {}).get(node_id, 0)
    settled = state.get("settled_attempts", {}).get(node_id, 0)
    return node_id in state.get("_resume_skip", set()) and required <= settled


def check_dep_failed(node: Node, state: WorkflowState) -> dict | None:
    failed = state.get("failed_nodes", set())
    for dep in node.depends_on:
        if dep in failed:
            return {
                "failed_nodes": {node.id},
                "errors": [
                    {
                        "node": node.id,
                        "error": f"Skipped: dependency '{dep}' failed",
                        # This node never ran — the real failure is the dep.
                        "kind": "upstream_failed",
                    }
                ],
            }
    return None


def all_deps_completed(node: Node, state: WorkflowState) -> bool:
    """A repeated route requires fresh completions only inside its downstream graph."""
    if state.get("_cold_entry") == node.id and node.id not in state.get("completed_nodes", set()):
        return True
    completed = state.get("completed_nodes", set())
    required = state.get("required_attempts", {})
    settled = state.get("settled_attempts", {})
    fan_ready = state.get("fan_ready", {})
    return all(dep in completed and fan_ready.get(dep, True) and settled.get(dep, 0) >= required.get(dep, 0)
               for dep in node.depends_on)


def check_dry_run(node: Node, state: WorkflowState) -> dict | None:
    if not state.get("dry_run", False):
        return None
    update: dict[str, Any] = {
        "node_outputs": {node.id: f"[dry-run] {node.name}"},
    }
    if not (node.eval or node.route):
        update["completed_nodes"] = {node.id}
    return update


class _TemplateOutput:
    """Jinja renders the original text and indexes the validated JSON value."""

    def __init__(self, text, value):
        self._text = text
        self._value = value

    def __str__(self):
        return self._text

    def __getitem__(self, key):
        return self._value[key]


def bind_output(state: WorkflowState, node_id: str):
    text = state.get("node_outputs", {}).get(node_id, "")
    structured = state.get("node_structured_outputs", {})
    return _TemplateOutput(text, structured[node_id]) if node_id in structured else text


def build_context(node: Node, state: WorkflowState) -> dict[str, Any]:
    import json as _json

    context: dict[str, Any] = {}
    outputs = state.get("node_outputs", {})
    structured = state.get("node_structured_outputs", {})
    worktrees = state.get("node_worktrees", {})
    # Subgraph inputs (Stage 4c): inputs declared on a parent's subgraph-
    # reference node are projected into the subgraph's state.node_inputs
    # before invocation. Subgraph nodes see them as plain template vars,
    # alongside their own dep outputs. Top-level graphs have no inputs.
    inputs = state.get("node_inputs", {}) or {}
    context.update(inputs)
    context["workflow_inputs"] = inputs
    for dep in node._ancestors or node.depends_on:
        if dep in outputs:
            context[dep] = bind_output(state, dep)
        if dep in structured:
            context[f"{dep}_structured"] = structured[dep]
        if dep in worktrees:
            context[f"{dep}_worktree"] = worktrees[dep]
        # Subgraph `outputs:` projects values to `node_outputs[dep.key]`.
        # Bind those under `{dep}_{key}` so templates can reach them
        # without dotted-attribute syntax (Jinja can't dot-into a string).
        dotted_prefix = f"{dep}."
        for k, v in outputs.items():
            if k.startswith(dotted_prefix):
                suffix = k[len(dotted_prefix):]
                context[f"{dep}_{suffix}"] = v
    # When a node has multiple deps, provide aggregate collections so
    # templates can iterate inputs generically without hardcoding names.
    if len(node.depends_on) > 1:
        dep_outputs_map = {}
        dep_worktrees_map = {}
        for dep in node.depends_on:
            if dep in outputs:
                dep_outputs_map[dep] = outputs[dep]
            if dep in worktrees:
                dep_worktrees_map[dep] = worktrees[dep]
        context["_deps"] = _json.dumps(dep_outputs_map)
        if dep_worktrees_map:
            context["_dep_worktrees"] = _json.dumps(dep_worktrees_map)

    # Stage 5c: cross-cutting eval access. `evals[node_id]` returns the
    # latest evaluation result dict for any node that has run an eval.
    # Always-on so templates can reference `{{evals.classify.score}}`
    # without declaring classify as a dep.
    history = state.get("evaluations", {}) or {}
    context["evals"] = {
        nid: (records[-1].get("result", {}) or {}) if records else {}
        for nid, records in history.items()
    }

    routes = state.get("route_context", {})
    if node.id not in routes:
        context.update(state.get("workflow_route_context", {}))
    incoming = routes.get(node.id) or {}
    sender_id = incoming.get("sender")
    if sender_id is not None:
        context["sender_id"] = sender_id
        if sender_id in outputs:
            context["sender"] = outputs[sender_id]
        if sender_id in structured:
            context["sender_structured"] = structured[sender_id]
        if sender_id in worktrees:
            context["sender_worktree"] = worktrees[sender_id]

    preamble = incoming.get("preamble")
    if preamble:
        context["_route_eval_preamble"] = preamble

    if state.get("_fan_child_id") == node.id:
        context["item"] = state["_fan_out_item"]
    return context


async def run_with_timeout(
    awaitable: Any, timeout: float | None,
) -> ExecutionResult:
    """Await an execute-coroutine with an optional timeout.

    On timeout, returns a failure ``ExecutionResult`` (rather than a
    sentinel) so callers handle it through the ordinary
    ``if not result.success`` path. ``timeout=None`` awaits unbounded.
    The awaitable must resolve to an ``ExecutionResult``.
    """
    if timeout is None:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except asyncio.TimeoutError:
        return ExecutionResult(
            success=False, error=f"Node timed out after {timeout}s",
            error_kind="timeout",
        )


def make_failure_update(
    node_id: str, error_message: str, kind: str = "node_error",
) -> dict[str, Any]:
    """Build a single-node failure update. ``kind`` is the failure
    classification surfaced on the JSONL ``node_failed`` event; it defaults to
    ``node_error`` (deterministic → halt), the fail-safe for any site that
    can't name a more specific kind."""
    return {
        "failed_nodes": {node_id},
        "errors": [{"node": node_id, "error": error_message, "kind": kind}],
    }


def assemble_success_update(node: Node, result: ExecutionResult) -> dict[str, Any]:
    update: dict[str, Any] = {
        "node_outputs": {node.id: result.output},
    }
    if result.structured_output is not None or (node.outputs and node.outputs.schema_file):
        update["node_structured_outputs"] = {node.id: result.structured_output}
    if result.model is not None or result.provider is not None:
        update["node_models"] = {
            node.id: {"model": result.model, "provider": result.provider}
        }
    return update


def _make_execution_node(
    node: Node,
    config: Graph,
    executor: NodeExecutor | None = None,
    *,
    effective_settings: Settings | None = None,
    defer_promotion: bool = False,
):
    """Execute the node with its effective settings and verify its output files."""
    settings = effective_settings or config.settings
    timeout = node.effective_timeout(settings)

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        # Resume skip: a node frozen in the prior-run snapshot does not
        # re-execute. Its completed_nodes/node_outputs are already reseeded,
        # so downstream context and the deps join-barrier still see it.
        if is_frozen(node.id, state):
            return {}
        for check in (check_dep_failed, check_dry_run):
            if (r := check(node, state)) is not None:
                return r
        if node.depends_on and not all_deps_completed(node, state):
            # A gated predecessor routed here before its siblings finished.
            # Return no-op — subsequent pred completions re-fire this node.
            return {}
        if executor is None:
            update: dict[str, Any] = {
                "node_outputs": {node.id: f"[no-executor] {node.name}"},
            }
            if not (node.eval or node.route):
                update["completed_nodes"] = {node.id}
            return update

        context = build_context(node, state)
        retry_count = state.get("retries", {}).get(node.id, 0)
        if retry_count > 0:
            delay = retry_delay(retry_count, settings.retry_backoff)
            if delay > 0:
                await asyncio.sleep(delay)

        exec_result, executions = await execute_with_outputs(
            lambda ctx: run_with_timeout(
                executor.execute(node, ctx, settings_override=effective_settings), timeout,
            ),
            context, node.outputs, state.get("workdir", "."),
        )
        attempts = {node.id: state.get("attempts", {}).get(node.id, 0) + executions}
        if not exec_result.success:
            # Carry the classification the executor set (overload / backend_error
            # / timeout); an unclassified dispatch failure defaults to node_error.
            failure = make_failure_update(node.id, exec_result.error, kind=exec_result.error_kind or "node_error")
            return {**failure, "attempts": attempts}

        update = assemble_success_update(node, exec_result)
        update["attempts"] = attempts
        if exec_result.worktree:
            update["node_worktrees"] = {node.id: exec_result.worktree}
        elif hasattr(executor, "get_worktree"):
            wt = executor.get_worktree(node.id)
            if wt:
                update["node_worktrees"] = {node.id: wt}
        if not (node.eval or node.route):
            promotion = {} if defer_promotion else settle_nodes(executor, node, settings, [node.id])
            update.update(promotion)
            if not promotion.get("failed_nodes"):
                update["completed_nodes"] = {node.id}
        # Gated nodes hand off to _eval_{node.id} via plain edge; the
        # Evaluation node writes completed_nodes / retries / failed_nodes.

        return update

    node_fn.__name__ = f"node_{node.id}"
    return count_attempts(node, node_fn)


def count_attempts(node, body):
    """Count executed bodies, including failures resumed before measurement."""
    async def counted(state):
        update = await body(state)
        failed = node.id in update.get("failed_nodes", set())
        if node.id in update.get("node_outputs", {}) or (failed and all_deps_completed(node, state)):
            attempt = state.get("attempts", {}).get(node.id, 0) + 1
            attempts = update.setdefault("attempts", {node.id: attempt})
            if node.id in update.get("completed_nodes", set()):
                update["settled_attempts"] = attempts
        return update

    return counted


async def settle(node, state, settings, executor, context=None, *, authored_id=None):
    key = node.id
    attempt = state.get("attempts", {}).get(key, 1)
    history = list(state.get("evaluations", {}).get(key, []))
    update = {}
    variables = build_context(node, state) if context is None else dict(context)
    output = state.get("node_outputs", {}).get(key, "")
    variables.update(output=output, node_output=output, node_id=key, attempt=attempt)
    variables[authored_id or key] = bind_output(state, key)
    try:
        if node.eval:
            cwd = state.get("node_worktrees", {}).get(key) or state.get("workdir", ".")
            payload = await run_evaluation(node, variables, cwd, settings, executor)
            record = {"attempt": attempt, "result": payload,
                      "timestamp": datetime.now(timezone.utc).isoformat()}
            history.append(record)
            update["evaluations"] = {key: [record]}
        case, exhausted = select_case(node, state, attempt, history)
        if exhausted:
            description = exhausted.model_dump(by_alias=True, exclude_none=True)
            raise EvaluationError(f"Route case {description} exhausted at attempt {attempt}")
        dest = (case.then or case.otherwise) if case else None
        if dest == "fail":
            raise EvaluationError(f"Route selected fail at attempt {attempt}")
        previous = state.get("route_destinations", {}).get(key)
        old_targets = previous if isinstance(previous, list) else [previous]
        routed = {
            target: {} for target in [key, *old_targets] if target
            and (state.get("route_context", {}).get(target) or {}).get("sender") == key
        }
        if case:
            destinations = [key] if dest == "retry" else case.targets
            preamble = build_eval_preamble(case.context, history, attempt, variables)
            routed.update({
                d: ({"sender": key, "preamble": preamble} if case.context != "none" else {})
                for d in destinations
            })
        if routed:
            update["route_context"] = routed
        if dest == "retry":
            update["retries"] = {key: state.get("retries", {}).get(key, 0) + 1}
        else:
            update["completed_nodes"] = {key}
            update["settled_attempts"] = {key: attempt}
            update["route_destinations"] = {key: dest}
        return update, dest
    except EvaluationError as exc:
        failure = make_failure_update(key, str(exc), "gate_failure")
    except EvaluationBackendError as exc:
        failure = make_failure_update(key, str(exc), exc.kind)
    except Exception as exc:
        failure = make_failure_update(key, str(exc))
    return {**update, **failure}, "fail"


def _make_gate_node(node, config, executor=None, *, exec_id, pass_targets, effective_settings=None):
    settings = effective_settings or config.settings
    fallback = pass_targets[0] if len(pass_targets) == 1 else pass_targets

    routes = list(iter_route_edges(config)) if node.route else []
    forward = {
        destination for source, _, destination, back in routes
        if source.id == node.id and not back and destination not in {"retry", "fail"}
    }
    back_targets = {target for source, _, target, back in routes if source.id == node.id and back}
    repeat_from = {}
    if back_targets:
        downstream = {n.id: set() for n in config.nodes}
        for n in config.nodes:
            for dep in n.depends_on:
                downstream[dep].add(n.id)
        for source, _, target, back in routes:
            if not back and target in downstream:
                downstream[source.id].add(target)
        for target in back_targets:
            reached = set()
            pending = [target]
            while pending:
                current = pending.pop()
                if current not in reached:
                    reached.add(current)
                    pending.extend(downstream[current])
            repeat_from[target] = reached

    async def gate(state):
        if node.id in state.get("failed_nodes", set()):
            return Command(goto=END)
        if is_frozen(node.id, state):
            previous = state.get("route_destinations", {}).get(node.id)
            return Command(goto=previous or fallback)
        attempt = state.get("attempts", {}).get(node.id, 1)
        settled = state.get("settled_attempts", {}).get(node.id, 0)
        required = state.get("required_attempts", {}).get(node.id, 0)
        if attempt <= settled or attempt < required or node.id not in state.get("node_outputs", {}):
            return {}
        if state.get("dry_run"):
            # Measurements and branch predicates require real output. Trace forward paths once.
            targets = list(dict.fromkeys([*pass_targets, *sorted(forward)]))
            return Command(update={"completed_nodes": {node.id}},
                           goto=targets[0] if len(targets) == 1 else targets)
        update, target = await settle(node, state, settings, executor)
        if target == "fail":
            return Command(update=update, goto=END)
        if target == "retry":
            return Command(update=update, goto=exec_id)
        promotion = settle_nodes(executor, node, settings, [node.id])
        if promotion.get("failed_nodes"):
            update.pop("completed_nodes", None)
            update.pop("settled_attempts", None)
            return Command(update={**update, **promotion}, goto=END)
        update.update(promotion)
        destinations = target if isinstance(target, list) else [target]
        repeated = {child for dest in destinations for child in repeat_from.get(dest, ())}
        if repeated:
            attempts = state.get("attempts", {})
            update["required_attempts"] = {child: attempts.get(child, 0) + 1 for child in repeated}
        goto = target or fallback
        return Command(update=update, goto=goto)

    return gate
