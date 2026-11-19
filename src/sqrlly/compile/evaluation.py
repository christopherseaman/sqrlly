"""Apply ordered route cases to the current attempt and evaluation history."""

from __future__ import annotations

from sqrlly.compile.route import build_route_namespace, build_safe_funcs, evaluate_case
from sqrlly.runtime.state import WorkflowState
from sqrlly.schema.v1 import Case

_RUN_DEFAULT = Case(when="eval.exit_code != 0", then="retry", max_attempts=3)


def select_case(node, state: WorkflowState, attempt: int, history: list[dict]):
    """Return the selected case, or the first exhausted case when none matched."""
    namespace = build_route_namespace(state, [*(node._ancestors or node.depends_on), node.id])
    namespace.update(eval=history[-1]["result"] if history else {}, attempt=attempt, history=history)
    functions = build_safe_funcs(state)
    cases = node.route or ([_RUN_DEFAULT] if node.eval and node.eval.run else [])
    exhausted = None
    for case in cases:
        if case.when and not evaluate_case(case.when, namespace, functions):
            continue
        if case.max_attempts is not None and attempt >= case.max_attempts:
            exhausted = exhausted or case
            continue
        return case, None
    return None, exhausted
