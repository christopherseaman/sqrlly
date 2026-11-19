from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.compile.nodes import build_context
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph, Node, Settings
from helpers import init_git_repo

_PARENT_ID = "writer_pool"


# ---------------------------------------------------------------------------
# Shared fixtures helpers (mirror test_subgraph_fanout_worktree.py)
# ---------------------------------------------------------------------------


def _init_git_repo(path):
    init_git_repo(path)

def _write_sub_yaml(tmp_path: Path, *, gen_worktree: str | None = None) -> str:
    """Two-node subgraph: gen writes a file, polish reads and extends it.

    ``gen_worktree`` — when set, injects a ``worktree:`` directive on the
    ``gen`` node so Task-5 can verify the directive is neutralised.
    """
    gen_node: dict[str, Any] = {
        "id": "gen",
        "name": "Generate",
        "run": ["/bin/sh", "-c", "echo generated-{{item_id}} > gen.txt"],
    }
    if gen_worktree is not None:
        gen_node["worktree"] = gen_worktree

    sub = {
        "name": "write-sub",
        "nodes": [
            gen_node,
            {
                "id": "polish",
                "name": "Polish",
                "depends_on": ["gen"],
                "run": ["/bin/sh", "-c", "echo polished-{{item_id}} > polish.txt"],
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
    return Graph(
        name="subgraph-fanout-worktree-pin-test",
        nodes=[
            {
                "id": _PARENT_ID,
                "name": "Writer Pool",
                "run": ["/bin/sh", "-c", f"printf '%s' '{manifest}'"],
                "fan_out": {
                    "template": {
                        "workflow": {"file": sub_yaml, "with": {"item_id": "{{id}}"}},
                    },
                },
            },
        ],
    )


def _make_foreman(tmp_path: Path) -> tuple[ForemanExecutor, DispatchExecutor]:
    inner = DispatchExecutor(workdir=str(tmp_path))
    foreman = ForemanExecutor(inner, base_workdir=str(tmp_path), max_parallel_jobs=4)
    return foreman, inner


class TestCapabilityFallthrough:
    @pytest.mark.asyncio
    async def test_non_foreman_executor_runs_in_workdir_no_wt_dirs(self, tmp_path):
        """A plain directory runs every subgraph in the shared workdir."""
        # Build the same 3-item subgraph fan-out but with plain DispatchExecutor.
        sub_yaml = _write_sub_yaml(tmp_path)
        config = _build_parent_config(tmp_path, sub_yaml)

        # No git init — DispatchExecutor does not require a git repo.
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor, _base_dir=tmp_path)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        # All 3 branches must complete (no-isolation is a valid execution mode).
        for item_id in ("alpha", "beta", "gamma"):
            child_id = f"{_PARENT_ID}::{item_id}"
            assert child_id in final_state["completed_nodes"], (
                f"{child_id} not in completed_nodes: {final_state['completed_nodes']}"
            )

        # No wt-* directories were created (no foreman = no worktree allocation).
        all_wt_dirs = list(tmp_path.rglob("wt-*"))
        assert all_wt_dirs == [], (
            f"Expected no wt-* dirs with plain DispatchExecutor, found: "
            f"{[str(d) for d in all_wt_dirs]}"
        )

        # node_worktrees has no branch entries (ExecutionResult.worktree == None).
        wts = final_state.get("node_worktrees", {})
        branch_wt_keys = [k for k in wts if k.startswith(f"{_PARENT_ID}::")]
        assert branch_wt_keys == [], (
            f"Expected no branch entries in node_worktrees with plain executor, "
            f"found: {branch_wt_keys}"
        )


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------


_OUTER_ID = "outer_pool"
_INNER_ID = "inner_pool"


def _write_leaf_sub_yaml(tmp_path: Path) -> str:
    """Leaf subgraph: single node writes leaf.txt — used as inner template."""
    leaf = {
        "name": "leaf-sub",
        "nodes": [
            {
                "id": "leaf_writer", "worktree": "isolated",
                "name": "Leaf Writer",
                "run": [
                    "/bin/sh", "-c",
                    "echo leaf-{{outer_id}}-{{inner_id}} > {{outer_id}}-{{inner_id}}.txt",
                ],
            },
        ],
    }
    leaf_path = tmp_path / "leaf_sub.yaml"
    leaf_path.write_text(yaml.safe_dump(leaf))
    return "leaf_sub.yaml"


def _write_middle_sub_yaml(tmp_path: Path, leaf_yaml: str) -> str:
    """Middle subgraph: one fan-out node that fans over 2 inner items,
    each running the leaf subgraph. Passes outer_id through as input."""
    inner_items = [{"id": "p"}, {"id": "q"}]
    inner_manifest = json.dumps({"items": inner_items})
    middle = {
        "name": "middle-sub",
        "nodes": [
            {
                "id": _INNER_ID,
                "name": "Inner Pool",
                "run": ["/bin/sh", "-c", f"printf '%s' '{inner_manifest}'"],
                "fan_out": {
                    "template": {
                        "workflow": {
                            "file": leaf_yaml,
                            "with": {"outer_id": "{{outer_id}}", "inner_id": "{{id}}"},
                        },
                    },
                },
            },
        ],
    }
    middle_path = tmp_path / "middle_sub.yaml"
    middle_path.write_text(yaml.safe_dump(middle))
    return "middle_sub.yaml"


def _build_nested_fanout_config(tmp_path: Path, middle_yaml: str) -> Graph:
    """Top-level config: outer fan-out over 2 items; template is the middle subgraph."""
    outer_items = [{"id": "x"}, {"id": "y"}]
    outer_manifest = json.dumps({"items": outer_items})
    return Graph(
        name="nested-fanout-shared-outer-tree-test",
        nodes=[
            {
                "id": _OUTER_ID,
                "name": "Outer Pool",
                "run": ["/bin/sh", "-c", f"printf '%s' '{outer_manifest}'"],
                "fan_out": {
                    "template": {
                        "workflow": {"file": middle_yaml, "with": {"outer_id": "{{id}}"}},
                    },
                },
            },
        ],
    )


class TestNestedFanOut:
    @pytest.mark.asyncio
    async def test_nested_fanout_promotes_all_leaf_files(self, tmp_path):
        """Each isolated leaf promotes its qualified output before the nested workflow completes."""
        _init_git_repo(tmp_path)
        leaf_yaml = _write_leaf_sub_yaml(tmp_path)
        middle_yaml = _write_middle_sub_yaml(tmp_path, leaf_yaml)
        config = _build_nested_fanout_config(tmp_path, middle_yaml)

        foreman, _inner = _make_foreman(tmp_path)
        graph = build_workflow_graph(config, foreman, _base_dir=tmp_path)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        # Both outer branches must have completed.
        for outer_id in ("x", "y"):
            child_id = f"{_OUTER_ID}::{outer_id}"
            assert child_id in final_state["completed_nodes"], (
                f"{child_id} not in completed_nodes: {final_state['completed_nodes']}"
            )

        assert final_state["failed_nodes"] == set(), final_state["errors"]
        for outer in ("x", "y"):
            for inner in ("p", "q"):
                assert (tmp_path / f"{outer}-{inner}.txt").read_text().strip() == f"leaf-{outer}-{inner}"
        assert not list((tmp_path / ".sqrlly").glob("wt-*"))
        await foreman.close()
