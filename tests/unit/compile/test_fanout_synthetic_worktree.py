"""Synthetic run children honor their parent worktree policy."""
from __future__ import annotations

import pytest

from sqrlly.compile.dynamic import _make_fan_out_node
from sqrlly.runtime.result import ExecutionResult
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import (
    FanOut, FanOutTemplate, Graph, Node, Settings,
)

from mock_executor import MockExecutor


def _parent(worktree):
    return Node(
        id="build", name="build", run=["true"], **({"worktree": worktree} if worktree else {}),
        fan_out=FanOut(
            template=FanOutTemplate(
                run=["/bin/sh", "-c", "true"],
            ),
        ),
    )


class _CapturingExecutor(MockExecutor):
    """Captures the Node passed to execute(); returns a trivial success."""
    def __init__(self):
        super().__init__()
        self.captured: list[Node] = []

    async def execute(self, node, context, workdir=None, settings_override=None):
        self.captured.append(node)
        return ExecutionResult(success=True, output="ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("worktree", ["off", "isolated", None])
async def test_synthetic_child_inherits_worktree(worktree):
    parent = _parent(worktree)
    config = Graph(name="t", nodes=[parent],
                   settings=Settings())
    executor = _CapturingExecutor()
    node_fn = _make_fan_out_node(
        parent, config, executor,
        compile_fn=lambda *a, **k: None, base_dir=".", depth=0,
        effective_settings=config.settings,
    )
    state = make_initial_state(workdir=".")
    state["_fan_out_item"] = {"id": "x"}
    await node_fn(state)

    assert len(executor.captured) == 1
    synthetic = executor.captured[0]
    assert synthetic.id == "build::x"
    assert synthetic.worktree == (worktree or "isolated")
