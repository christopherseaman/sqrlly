"""Compiled eval routes use the node worktree and preserve every measurement."""

import sys

import pytest
from langgraph.graph import END

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.compile.nodes import _make_gate_node
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph, Node, Settings


async def test_measurement_reads_file_from_node_worktree(tmp_path):
    tree = tmp_path / "branch"
    tree.mkdir()
    (tree / "artifact").write_text("from branch")
    node = Node(id="build", eval={"run": [sys.executable, "-c", "print(open('artifact').read())"]})
    gate = _make_gate_node(node, Graph(nodes=[node]), exec_id="build", pass_targets=["review"])
    state = make_initial_state(workdir=str(tmp_path))
    state["node_outputs"] = {"build": "done"}
    state["node_worktrees"] = {"build": str(tree)}
    command = await gate(state)
    assert command.goto == "review"
    record, = command.update["evaluations"]["build"]
    assert record["attempt"] == 1
    assert record["result"]["text"].strip() == "from branch"


@pytest.mark.parametrize("escalate", [True, False])
async def test_exhausted_eval_retries_route_or_fail(tmp_path, escalate):
    predicate = "eval.exit_code != 0"
    route = [{"when": predicate, "then": "retry", "max_attempts": 3}]
    if escalate:
        route.append({"when": predicate, "then": "escalate"})
    node = Node(id="build", run=[sys.executable, "-c", "open('calls','a').write('x')"],
                eval={"run": [sys.executable, "-c", "print('broken'); raise SystemExit(1)"]}, route=route)
    nodes = [node]
    if escalate:
        nodes.append(Node(id="escalate", run=[sys.executable, "-c", "print('escalated')"]))
    config = Graph(nodes=nodes, settings=Settings(retry_backoff=[0]))
    graph = build_workflow_graph(config, DispatchExecutor(str(tmp_path)))
    state = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
    assert (tmp_path / "calls").read_text() == "xxx"
    assert [r["attempt"] for r in state["evaluations"]["build"]] == [1, 2, 3]
    if escalate:
        assert state["completed_nodes"] == {"build", "escalate"}
        assert state["node_outputs"]["escalate"].strip() == "escalated"
    else:
        assert state["failed_nodes"] == {"build"}
        message = state["errors"][-1]["error"]
        assert "retry" in message and predicate in message and "3" in message


async def test_failed_or_frozen_body_skips_measurement(tmp_path):
    node = Node(id="build", eval={"run": ["unused"]})
    gate = _make_gate_node(node, Graph(nodes=[node]), exec_id="build", pass_targets=["next"])
    assert (await gate({"failed_nodes": {"build"}})).goto == END
    assert (await gate({"_resume_skip": {"build"}})).goto == "next"
    assert await gate({"node_outputs": {}}) == {}


async def test_dry_run_skips_subprocess(tmp_path):
    node = Node(id="build", eval={"run": ["unused"]})
    gate = _make_gate_node(node, Graph(nodes=[node]), exec_id="build", pass_targets=["next"])
    state = make_initial_state(workdir=str(tmp_path), dry_run=True)
    state["node_outputs"] = {"build": "[dry-run]"}
    command = await gate(state)
    assert command.goto == "next"
    assert command.update["completed_nodes"] == {"build"}


@pytest.mark.parametrize("context", ["last", "none"])
async def test_escalation_prompt_context_reaches_subprocess(tmp_path, monkeypatch, context):
    from sqrlly.runtime.executor import dispatch
    from sqrlly.runtime.executor.backends.cli import CLIBackend

    # cat echoes the exact bytes sent to the prompt subprocess.
    backend = CLIBackend(("cat",))
    monkeypatch.setattr(dispatch, "create_backend", lambda *args, **kwargs: backend)
    check = {"when": "eval.exit_code != 0", "then": "retry", "max_attempts": 3}
    escalate = {"when": check["when"], "then": "review", "context": context}
    node = Node(id="build", run=["true"], route=[check, escalate],
                eval={"run": [sys.executable, "-c", "print('missing artifact'); raise SystemExit(1)"]})
    review = Node(id="review", llm={"text": "Review the build."})
    config = Graph(nodes=[node, review], settings=Settings(retry_backoff=[0]))
    graph = build_workflow_graph(config, DispatchExecutor(str(tmp_path)))
    state = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
    prompt = state["node_outputs"]["review"]
    if context == "last":
        assert "Attempt 3" in prompt
        assert "missing artifact" in prompt
        assert prompt.rstrip().endswith("Review the build.")
    else:
        assert prompt == "Review the build."
