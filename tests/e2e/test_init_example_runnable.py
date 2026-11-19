"""Every scaffolded example compiles from its own dir; a no-backend one runs."""
from __future__ import annotations


import pytest

from sqrlly.cli.init import EXAMPLES, init_example
from sqrlly.cli.main import load_config
from sqrlly.compile.graph import build_workflow_graph


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_scaffolded_example_validates(name, tmp_path):
    dest = tmp_path / name
    init_example(name, str(dest))
    config = load_config(str(dest / "workflow.yaml"))
    build_workflow_graph(config)
    assert config.name


def test_scaffolded_explicit_join_runs(tmp_path):
    import asyncio

    from sqrlly.runtime.executor.dispatch import DispatchExecutor
    from sqrlly.runtime.state import make_initial_state

    dest = tmp_path / "ej"
    init_example("explicit_join", str(dest))
    config = load_config(str(dest / "workflow.yaml"))
    graph = build_workflow_graph(config, DispatchExecutor(workdir=str(dest)))
    result = asyncio.run(graph.ainvoke(make_initial_state(workdir=str(dest))))
    assert set(result["completed_nodes"]) == {"branch_a", "branch_b", "branch_c", "sync", "report"}
    assert result["node_outputs"]["report"].strip() == "A=A done | B=B done | C=C done"
    assert not result.get("failed_nodes")
