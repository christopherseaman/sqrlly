"""Repeated command execution, aggregation and eval through the compiled graph."""

import json
import sys

import pytest

from helpers import make_config
from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state


async def execute(nodes, workdir):
    config = make_config(nodes)
    graph = build_workflow_graph(config, DispatchExecutor(str(workdir)), _base_dir=workdir)
    return await graph.ainvoke(make_initial_state(workdir=str(workdir)))


@pytest.mark.parametrize("size", [0, 1, 32])
async def test_join_receives_every_result_in_manifest_order(tmp_path, size):
    items = [{"id": f"i{i}", "name": f"name{i}"} for i in range(size)]
    state = await execute([
        {"id": "plan", "run": ["echo", json.dumps({"items": items})]},
        {"id": "prefix", "run": ["printf", "ok"]},
        {"id": "build", "for_each": "plan", "depends_on": ["plan", "prefix"],
         "run": [sys.executable, "-c",
                 "import sys,time; time.sleep(.08 if sys.argv[1]=='i0' else 0); print(sys.argv[2],end='')",
                 "{{item.id}}", "{{prefix}}-{{item.name}}"]},
        {"id": "first_join", "depends_on": ["build"], "run": ["printf", "%s", "{{build}}"]},
        {"id": "second_join", "depends_on": ["build"], "run": ["printf", "%s", "{{build}}"]},
    ], tmp_path)
    assert not state["failed_nodes"], state["errors"]
    expected = [f"ok-{item['name']}" for item in items]
    for join in ("first_join", "second_join"):
        assert json.loads(state["node_outputs"][join]) == expected
    assert all(f"build::{item['id']}" in state["completed_nodes"] for item in items)


async def test_children_retry_command_eval_independently(tmp_path):
    items = [{"id": "a"}, {"id": "b"}]
    state = await execute([
        {"id": "plan", "run": ["echo", json.dumps(items)]},
        {"id": "build", "depends_on": ["plan"], "for_each": "plan",
         "run": ["printf", "%s", "{{item.id}}"],
         "eval": {"run": [sys.executable, "-c",
                          "import sys; print(sys.argv[2]); sys.exit(int(sys.argv[1]) == 1)",
                          "{{attempt}}", "{{output}}"]}},
    ], tmp_path)
    assert not state["failed_nodes"], state["errors"]
    for item in items:
        records = state["evaluations"][f"build::{item['id']}"]
        assert [record["attempt"] for record in records] == [1, 2]
        assert [record["result"]["exit_code"] for record in records] == [1, 0]
        assert {record["result"]["text"].strip() for record in records} == {item["id"]}


async def test_failed_manifest_producer_blocks_children(tmp_path):
    state = await execute([
        {"id": "plan", "run": ["echo", '[{"id":"x"}]'], "eval": {"run": ["false"]}},
        {"id": "build", "for_each": "plan", "depends_on": ["plan"], "run": ["touch", "unexpected"]},
    ], tmp_path)
    assert "plan" in state["failed_nodes"]
    assert not (tmp_path / "unexpected").exists()


async def test_static_items_dry_run_traces_children_without_executing_them(tmp_path):
    (tmp_path / "items.json").write_text('[{"id":"x"},{"id":"y"}]')
    config = make_config([{
        "id": "build", "for_each": {"file": "items.json"},
        "run": ["touch", "should-not-exist"],
    }])
    graph = build_workflow_graph(config, DispatchExecutor(str(tmp_path)), _base_dir=tmp_path)
    state = await graph.ainvoke(make_initial_state(workdir=str(tmp_path), dry_run=True))
    assert state["completed_nodes"] == {"build", "build::x", "build::y"}
    assert not (tmp_path / "should-not-exist").exists()
