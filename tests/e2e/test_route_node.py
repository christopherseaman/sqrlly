from __future__ import annotations

import sys
import shutil
from pathlib import Path

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.result import ExecutionResult
from sqrlly.runtime.state import make_initial_state

from helpers import cmd_phase, make_config
from mock_executor import MockExecutor

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "route"

_ECHO = shutil.which("echo") or "/bin/echo"


@pytest.mark.parametrize("filename,ships", [("score_high.py", True), ("score_low.py", False)])
@pytest.mark.asyncio
async def test_measurement_routes_to_ship(tmp_path, filename, ships):
    config = make_config([
        cmd_phase("produce", output="draft"),
        {"id": "judge", "depends_on": ["produce"],
         "eval": {"run": [sys.executable, str(FIXTURES / filename)]},
         "route": [{"when": "history[-1]['result']['score'] >= 0.5", "then": "ship"}]},
        cmd_phase("ship", output="shipped"),
    ])
    graph = build_workflow_graph(config, DispatchExecutor(str(tmp_path)))
    state = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
    assert "judge" in state["completed_nodes"]
    assert ("ship" in state["completed_nodes"]) == ships


class TestFlowCStructuredOutputRouting:
    """Branch on structured upstream output supplied by the node executor."""

    @pytest.mark.asyncio
    async def test_urgent_category_routes_to_escalate(self, tmp_path):
        config = make_config([
            {
                "id": "produce", "name": "produce", "depends_on": [],
                "run": [_ECHO, "produced"],
            },
            {"id": "decide", "depends_on": ["produce"], "route": [
                    {"when": "produce['category'] == 'urgent'", "then": "escalate"},
                    {"when": "produce['category'] == 'normal'", "then": "ship"},
                ]},
            cmd_phase("escalate", output="escalated"),
            cmd_phase("ship", output="shipped"),
        ])
        executor = MockExecutor(results={
            "produce": ExecutionResult(
                success=True,
                output="produced",
                structured_output={"category": "urgent"},
            ),
            "escalate": ExecutionResult(success=True, output="escalated"),
            "ship": ExecutionResult(success=True, output="shipped"),
        })
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "produce" in result["completed_nodes"]
        assert "escalate" in result["completed_nodes"]
        assert "ship" not in result["completed_nodes"]
        assert result["node_outputs"]["escalate"] == "escalated"

    @pytest.mark.asyncio
    async def test_normal_category_routes_to_ship(self, tmp_path):
        config = make_config([
            {
                "id": "produce", "name": "produce", "depends_on": [],
                "run": [_ECHO, "produced"],
            },
            {"id": "decide", "depends_on": ["produce"], "route": [
                    {"when": "produce['category'] == 'urgent'", "then": "escalate"},
                    {"when": "produce['category'] == 'normal'", "then": "ship"},
                ]},
            cmd_phase("escalate", output="escalated"),
            cmd_phase("ship", output="shipped"),
        ])
        executor = MockExecutor(results={
            "produce": ExecutionResult(
                success=True,
                output="produced",
                structured_output={"category": "normal"},
            ),
            "escalate": ExecutionResult(success=True, output="escalated"),
            "ship": ExecutionResult(success=True, output="shipped"),
        })
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "produce" in result["completed_nodes"]
        assert "ship" in result["completed_nodes"]
        assert "escalate" not in result["completed_nodes"]
