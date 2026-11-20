"""Prompt measurements through a real provider CLI."""

import json
import sys

import pytest
from click.testing import CliRunner

from sqrlly.cli.main import cli
from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph


@pytest.mark.cli
@pytest.mark.asyncio
async def test_llm_measures_node_output(tmp_path):
    config = Graph(nodes=[{
        "id": "draft", "run": ["echo", "-n", "fox"],
        "eval": {"llm": {"text": 'Return score 1.0 and copy "{{output}}" into explanation.'},
                 "timeout": 180},
        "route": [{"when": "eval.score != 1", "then": "fail"}],
    }])
    graph = build_workflow_graph(config, DispatchExecutor(str(tmp_path), config.settings))
    result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
    assert "draft" in result["completed_nodes"]
    measurement = result["evaluations"]["draft"][0]["result"]
    assert measurement["score"] == 1
    assert measurement["explanation"] == "fox"


@pytest.mark.cli
@pytest.mark.asyncio
async def test_escalation_prompt_receives_final_measurement(tmp_path):
    from secrets import token_hex

    token = token_hex(12)
    predicate = "eval.exit_code != 0"
    config = Graph(nodes=[
        {"id": "draft", "run": ["echo", "draft"],
         "eval": {"run": [sys.executable, "-c", f"print('{token}'); raise SystemExit(1)"]},
         "route": [{"when": predicate, "then": "retry", "max_attempts": 3},
                   {"when": predicate, "then": "escalate"}]},
        {"id": "escalate", "llm": {"text": "Report the attempt number and measured token from the evaluation."},
         "timeout": 120},
    ])
    graph = build_workflow_graph(config, DispatchExecutor(str(tmp_path), config.settings))
    state = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
    assert [r["attempt"] for r in state["evaluations"]["draft"]] == [1, 2, 3]
    answer = state["node_outputs"]["escalate"]
    assert token in answer
    assert "3" in answer.replace(token, "")


@pytest.mark.cli
def test_malformed_prompt_eval_fails_node(tmp_path):
    workflow = tmp_path / "workflow.yaml"
    log = tmp_path / "events.jsonl"
    workflow.write_text("""
nodes:
  - id: checked
    run: [echo, ready]
    eval:
      agent:
        text: Assess the result.
        provider: openai
        cli_args: [--version]
    route:
      - else: next
  - id: next
    run: [echo, approved]
""")
    result = CliRunner().invoke(
        cli, ["run", str(workflow), "-w", str(tmp_path), "--log", str(log), "-q"],
    )

    assert result.exit_code == 1, result.output
    events = [json.loads(line) for line in log.read_text().splitlines()]
    failure = next(event for event in events if event["event"] == "node_failed")
    assert failure["node"] == "checked"
    assert failure["kind"] == "gate_failure"
    assert "eval.outputs.schema eval-score.schema.json: Expecting value" in failure["error"]
    end = next(event for event in events if event["event"] == "workflow_end")
    assert end["failed_kinds"] == {"checked": "gate_failure"}
    assert end["completed"] == 0
