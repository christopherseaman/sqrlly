"""A fresh or resumed for_each dispatch executes only unfrozen child ids."""

import pytest

from mock_executor import MockExecutor
from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph


@pytest.mark.parametrize("skip,expected", [
    (set(), {"write::a", "write::b"}),
    ({"write::a"}, {"write::b"}),
    ({"write::a", "write::b"}, set()),
])
async def test_frozen_child_ids_are_never_dispatched(tmp_path, skip, expected):
    (tmp_path / "items.json").write_text('[{"id":"a"},{"id":"b"}]')
    config = Graph(nodes=[{"id": "write", "agent": {"text": "Describe {{item.id}}"},
                           "for_each": {"file": "items.json"}}])
    executor = MockExecutor()
    compiled = build_workflow_graph(config, executor, _base_dir=tmp_path)
    state = make_initial_state(workdir=str(tmp_path), completed_nodes=skip, _resume_skip=skip,
                               node_outputs={node: node for node in skip})
    result = await compiled.ainvoke(state)
    assert set(executor.execution_order) == expected
    assert result["failed_nodes"] == set(), result["errors"]
