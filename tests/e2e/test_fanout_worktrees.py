"""Fan-out children execute in distinct worktrees and clear their paths at settlement."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph
from helpers import init_git_repo

_ECHO = shutil.which("echo") or "/bin/echo"


def _init_git_repo(path):
    init_git_repo(path)

def _build_fan_out_config(workdir: Path) -> Graph:
    """The manifest contains two distinct run branches."""
    manifest = json.dumps({"items": [{"id": "0"}, {"id": "1"}]})
    return Graph(
        name="fanout-worktree-test",
        nodes=[
            {
                "id": "parent",
                "name": "parent",
                "run": [_ECHO, "-n", manifest],
                "fan_out": {
                    "template": {
                        "run": ["pwd"],
                    },
                },
            },
        ],
    )


class TestFanOutWorktrees:
    @pytest.mark.asyncio
    async def test_settlement_clears_child_worktrees(self, tmp_path):
        """Both child processes report their actual working directories."""
        _init_git_repo(tmp_path)

        config = _build_fan_out_config(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(
            inner,
            base_workdir=str(tmp_path),
            max_parallel_jobs=4,
        )

        graph = build_workflow_graph(config, foreman)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        # Parent should complete
        assert "parent" in final_state["completed_nodes"]

        # Both children should complete
        assert "parent::0" in final_state["completed_nodes"]
        assert "parent::1" in final_state["completed_nodes"]

        paths = {Path(final_state["node_outputs"][f"parent::{i}"].strip()) for i in (0, 1)}
        assert len(paths) == 2
        assert all(path.parent == tmp_path / ".sqrlly" and not path.exists() for path in paths)
        assert final_state["node_worktrees"] == {}
        await foreman.close()
