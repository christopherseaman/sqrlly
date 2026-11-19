from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph, Settings
from helpers import init_git_repo

_PARENT_ID = "writer_pool"


def _init_git_repo(path):
    init_git_repo(path)

def _write_sub_yaml(tmp_path: Path) -> str:
    """An isolated writer promotes its per-item file before a serial reader runs."""
    sub = {
        "name": "write-sub",
        "nodes": [
            {
                "id": "gen", "worktree": "isolated",
                "name": "Generate",
                "run": ["/bin/sh", "-c", "echo generated-{{item_id}} > gen-{{item_id}}.txt"],
            },
            {
                "id": "polish",
                "name": "Polish",
                "depends_on": ["gen"],
                "run": ["/bin/sh", "-c", "cat gen-{{item_id}}.txt > polish-{{item_id}}.txt"],
            },
        ],
    }
    sub_path = tmp_path / "write_sub.yaml"
    sub_path.write_text(yaml.safe_dump(sub))
    return "write_sub.yaml"


def _build_parent_config(tmp_path: Path, sub_yaml: str) -> Graph:
    """Parent fan-out over 3 manifest items; template is the subgraph."""
    items = [{"id": "alpha"}, {"id": "beta"}, {"id": "gamma"}]
    manifest = json.dumps({"items": items})
    # printf avoids echo quoting issues with JSON strings
    return Graph(
        name="subgraph-fanout-worktree-test",
        nodes=[
            {"id": "manifest",
             "run": ["/bin/sh", "-c", f"printf '%s' '{manifest}'"]},
            {"id": _PARENT_ID,
             "for_each": "manifest",
             "depends_on": ["manifest"],
             "workflow": {"file": sub_yaml, "with": {"item_id": "{{item.id}}"}}},
        ],
    )


class TestSubgraphFanOutWorktrees:
    @pytest.mark.asyncio
    async def test_per_branch_worktree_isolation(self, tmp_path):
        """Three subgraph invocations preserve all three isolated writer outputs."""
        _init_git_repo(tmp_path)
        sub_yaml = _write_sub_yaml(tmp_path)
        config = _build_parent_config(tmp_path, sub_yaml)

        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner, base_workdir=str(tmp_path), max_parallel_jobs=4)

        graph = build_workflow_graph(config, foreman, _base_dir=tmp_path)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        assert final_state["failed_nodes"] == set(), final_state["errors"]
        for item_id in ("alpha", "beta", "gamma"):
            assert f"{_PARENT_ID}::{item_id}" in final_state["completed_nodes"]
            assert (tmp_path / f"polish-{item_id}.txt").read_text().strip() == f"generated-{item_id}"
        assert not list((tmp_path / ".sqrlly").glob("wt-*"))
        await foreman.close()
