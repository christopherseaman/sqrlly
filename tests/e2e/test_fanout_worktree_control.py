"""A node-level worktree override applies to every run branch."""

import json
from pathlib import Path

import pytest

from helpers import init_git_repo
from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph


@pytest.mark.parametrize("worktree", ["off", "isolated"])
async def test_run_children_obey_worktree_override(tmp_path, worktree):
    init_git_repo(tmp_path)
    node = {"id": "build", "worktree": worktree, "run": ["echo", json.dumps([{"id": "a"}])],
            "fan_out": {"template": {"run": ["pwd"]}}}
    executor = ForemanExecutor(DispatchExecutor(str(tmp_path)), str(tmp_path))
    try:
        graph = build_workflow_graph(Graph(nodes=[node]), executor, _base_dir=tmp_path)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        path = Path(result["node_outputs"]["build::a"].strip())
        assert result["failed_nodes"] == set(), result["errors"]
        if worktree == "off":
            assert path == tmp_path
        else:
            assert path.parent == tmp_path / ".sqrlly"
            assert not path.exists()
    finally:
        await executor.close()
