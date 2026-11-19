"""A dependent reads both branch deltas after fan-out settlement."""

import json

from helpers import init_git_repo
from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph


async def test_join_reads_promoted_branch_files(tmp_path):
    init_git_repo(tmp_path)
    config = Graph(nodes=[
        {"id": "build", "run": ["echo", json.dumps([{"id": "a"}, {"id": "b"}])],
         "fan_out": {"template": {"run": ["sh", "-c", "echo {{id}} > {{id}}.txt"]}}},
        {"id": "read", "depends_on": ["build"], "run": ["cat", "a.txt", "b.txt"]},
    ])
    executor = ForemanExecutor(DispatchExecutor(str(tmp_path)), str(tmp_path))
    try:
        graph = build_workflow_graph(config, executor, _base_dir=tmp_path)
        state = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert state["failed_nodes"] == set(), state["errors"]
        assert state["node_outputs"]["read"].splitlines() == ["a", "b"]
        assert not list((tmp_path / ".sqrlly").glob("wt-*"))
    finally:
        await executor.close()
