"""Synthetic run children honor their parent worktree policy."""
from __future__ import annotations

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph

from mock_executor import MockExecutor


class _CapturingExecutor(MockExecutor):
    """Captures the Node passed to execute(); returns a trivial success."""
    def __init__(self):
        super().__init__()
        self.captured = []

    async def execute(self, node, context, workdir=None, settings_override=None):
        self.captured.append(node)
        return await super().execute(node, context, workdir, settings_override)


@pytest.mark.parametrize("worktree", ["off", "isolated", None])
async def test_synthetic_child_inherits_worktree(tmp_path, worktree):
    (tmp_path / "items.json").write_text('[{"id":"x"}]')
    node = {"id": "build", "run": ["pwd"], "for_each": {"file": "items.json"}}
    if worktree:
        node["worktree"] = worktree
    executor = _CapturingExecutor()
    compiled = build_workflow_graph(Graph(nodes=[node]), executor, _base_dir=tmp_path)
    result = await compiled.ainvoke(make_initial_state(workdir=str(tmp_path)))
    assert result["failed_nodes"] == set(), result["errors"]
    child, = executor.captured
    assert child.id == "build::x"
    assert child.worktree == (worktree or "isolated")
