"""Unit tests for compile/subgraph.py — single functions, known-good/bad pairs.

End-to-end recursive composition is exercised in tests/e2e/test_recursive_subgraph.py.
This module covers the helpers in isolation.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from sqrlly.compile.subgraph import (
    SubgraphCycleError,
    detect_config_cycle,
    load_graph,
)
from sqrlly.schema.models import Graph


def _write_yaml(tmp_path, name: str, body: dict) -> str:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(body))
    return name


class TestLoadGraph:
    def test_loads_valid_graph(self, tmp_path):
        rel = _write_yaml(tmp_path, "ok.yaml", {
            "name": "G",
            "nodes": [{"id": "x", "name": "X", "agent": {"text": "test"}}],
        })
        g = load_graph(rel, base_dir=tmp_path)
        assert isinstance(g, Graph)
        assert g.nodes[0].id == "x"

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_graph("nope.yaml", base_dir=tmp_path)

    def test_raises_on_invalid_schema(self, tmp_path):
        rel = _write_yaml(tmp_path, "bad.yaml", {"name": "G"})
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            load_graph(rel, base_dir=tmp_path)


class TestDetectConfigCycle:
    """Walk config-reference DAG; raise on cycle, return None on valid graph."""

    def test_no_cycle_passes(self, tmp_path):
        _write_yaml(tmp_path, "leaf.yaml", {
            "name": "Leaf",
            "nodes": [{"id": "x", "name": "X", "agent": {"text": "test"}}],
        })
        _write_yaml(tmp_path, "root.yaml", {
            "name": "Root",
            "nodes": [
                {"id": "uses_leaf", "name": "Uses Leaf", "workflow": "leaf.yaml"}
            ],
        })
        # No exception
        detect_config_cycle("root.yaml", base_dir=tmp_path)

    def test_self_reference_cycle(self, tmp_path):
        _write_yaml(tmp_path, "loop.yaml", {
            "name": "Loop",
            "nodes": [
                {"id": "self", "name": "Self", "workflow": "loop.yaml"}
            ],
        })
        with pytest.raises(SubgraphCycleError) as exc:
            detect_config_cycle("loop.yaml", base_dir=tmp_path)
        assert "loop.yaml" in str(exc.value)

    def test_two_step_cycle(self, tmp_path):
        _write_yaml(tmp_path, "a.yaml", {
            "name": "A",
            "nodes": [{"id": "x", "name": "X", "workflow": "b.yaml"}],
        })
        _write_yaml(tmp_path, "b.yaml", {
            "name": "B",
            "nodes": [{"id": "y", "name": "Y", "workflow": "a.yaml"}],
        })
        with pytest.raises(SubgraphCycleError) as exc:
            detect_config_cycle("a.yaml", base_dir=tmp_path)
        assert "a.yaml" in str(exc.value)


import yaml as _yaml

from sqrlly.compile.subgraph import make_fan_out_subgraph_invoker
from sqrlly.runtime.result import ExecutionResult
from sqrlly.schema.models import Settings as _Settings


def _write_trivial_sub(tmp_path) -> str:
    sub = {
        "name": "sub",
        "nodes": [{"id": "n", "name": "n",
                   "run": ["/bin/sh", "-c", "true"]}],
    }
    (tmp_path / "sub.yaml").write_text(_yaml.safe_dump(sub))
    return "sub.yaml"


class _RecordingExecutor:
    """Records whether the per-branch worktree was acquired. Implements the
    NodeExecutor Protocol bits the invoker touches (execute + the optional
    acquire_branch_worktree); duck-typed, not a unittest.mock. The compiled
    subgraph is stubbed below, so execute() here is never actually reached —
    the observable signal is whether acquire_branch_worktree was called."""

    def __init__(self) -> None:
        self.acquired: list[str] = []

    async def acquire_branch_worktree(self, branch_id: str) -> str:
        self.acquired.append(branch_id)
        return f"/wt/{branch_id}"

    async def execute(self, node, context, workdir=None, settings_override=None):
        return ExecutionResult(success=True, output="ok")


class _StubCompiled:
    """Stands in for a compiled LangGraph — only ainvoke is exercised."""
    async def ainvoke(self, state):
        # Minimal terminal state: the subgraph's single node 'n' produced
        # output. _terminal_node_output reads node_outputs[terminals[-1]].
        return {"node_outputs": {"n": "done"}, "failed_nodes": set()}


def _stub_compile_fn(c, executor=None, _depth=0, effective_settings=None, logger=None):
    return _StubCompiled()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent_worktree,template_worktree,expect_acquire",
    [
        # No override → inherit parent settings.
        ("isolated", None, True),
        ("off", None, False),
        # Template override wins over parent settings.
        ("isolated", "off", False),     # the builder's exact use-case
        ("off", "isolated", True),
    ],
)
async def test_fanout_invoker_branch_acquire_follows_effective_worktree(
    tmp_path, parent_worktree, template_worktree, expect_acquire,
):
    sub_yaml = _write_trivial_sub(tmp_path)
    executor = _RecordingExecutor()
    invoker = make_fan_out_subgraph_invoker(
        sub_yaml, {}, compile_fn=_stub_compile_fn, base_dir=tmp_path, depth=0,
        executor=executor, parent_settings=_Settings(worktree=parent_worktree),
        template_worktree=template_worktree,
    )

    result = await invoker({}, str(tmp_path), False, prefix="build::alpha")

    assert result.success is True
    if expect_acquire:
        assert executor.acquired == ["build::alpha"], (
            "expected a branch worktree to be acquired (isolation on)"
        )
        # The acquired tree is surfaced for node_worktrees recording.
        assert result.worktree == "/wt/build::alpha"
    else:
        assert executor.acquired == [], (
            "expected NO branch worktree (worktree:off → run in shared workdir)"
        )
        assert result.worktree is None


def test_workflow_inputs():
    node = Graph(nodes=[{"id": "sub", "workflow": {
        "file": "child.yaml", "with": {"topic": "{{intake}}"},
    }}]).nodes[0]
    assert node.workflow.file == "child.yaml"
    assert node.workflow.inputs == {"topic": "{{intake}}"}
