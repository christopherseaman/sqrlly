"""Route selection uses a node's own attempt and evaluation history."""

import pytest

from sqrlly.compile.evaluation import select_case
from sqrlly.schema.models import Node


def choose(cases, attempt=1, exit_code=1):
    node = Node(id="build", depends_on=["plan"], eval={"run": ["true"]}, route=cases)
    history = [{"attempt": i, "result": {"exit_code": exit_code}} for i in range(1, attempt + 1)]
    state = {"node_structured_outputs": {"plan": {"ready": True}}}
    return select_case(node, state, attempt, history)


def test_first_matching_case_reads_outputs_and_history():
    cases = [
        {"when": "eval.exit_code and plan.ready and len(history) == attempt", "then": ["a", "b"]},
        {"else": "fallback"},
    ]
    selected, exhausted = choose(cases, attempt=2)
    assert selected.then == ["a", "b"]
    assert exhausted is None
    selected, exhausted = choose(cases, exit_code=0)
    assert selected.otherwise == "fallback"
    assert exhausted is None


@pytest.mark.parametrize("attempt,destination", [(1, "retry"), (2, "retry"), (3, "alert")])
def test_exhaustion_continues_through_repeated_predicate(attempt, destination):
    predicate = "eval.exit_code != 0"
    cases = [
        {"when": predicate, "then": "retry", "max_attempts": 3},
        {"when": predicate, "then": "alert"},
    ]
    selected, exhausted = choose(cases, attempt=attempt)
    assert selected.then == destination
    assert exhausted is None


def test_unmatched_condition_does_not_count_as_exhaustion():
    cases = [{"when": "False", "then": "fail", "max_attempts": 1}]
    assert choose(cases) == (None, None)


def test_default_run_eval_retries_three_times():
    selected, exhausted = choose(None, attempt=2)
    assert selected.then == "retry"
    assert exhausted is None
    selected, exhausted = choose(None, attempt=3)
    assert selected is None
    assert exhausted.max_attempts == 3
    assert choose(None, attempt=3, exit_code=0) == (None, None)
