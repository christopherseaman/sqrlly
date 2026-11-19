"""Manifest identity, source selection, drift, and dispatcher failure state."""
import json

import pytest
from langgraph.graph import END

from sqrlly.compile._manifest import _read_manifest
from sqrlly.compile.graph import _make_fan_dispatcher
from sqrlly.runtime.result import ManifestError
from sqrlly.schema.models import Node, Settings


def repeat(source):
    return Node(id="work", run=["echo", "{{item}}"], for_each=source,
                depends_on=[source.split(".")[0]] if isinstance(source, str) else [])


def test_value_ids_survive_insertions_order_and_canonical_object_keys():
    original = [{"a": 1, "b": 2}, None, [3], {"id": "alpha"}, None, "str", False, 2]
    updated = ["new", {"b": 2, "a": 1}, *original[1:]]
    def read(items):
        return _read_manifest({"node_structured_outputs": {"plan": items}}, repeat("plan"))
    before, after = read(original), read(updated)
    assert [branch for branch, _ in before] == [branch for branch, _ in after[1:]]
    assert [item for _, item in before] == original
    assert len(set(branch for branch, _ in before)) == len(original)
    assert before[3][0] == "work::alpha"
    edited = read([{"a": 9, "b": 2}])
    assert edited[0][0] != before[0][0]


def test_explicit_ids_cannot_collide_with_hash_or_nested_branches():
    def read(items):
        return _read_manifest({"node_structured_outputs": {"src": items}}, repeat("src"))
    hashed, = read(["idless"])
    raw = [hashed[0].split("::")[1], "a::b", "a%3A%3Ab", 1, "1", True, "!true"]
    branches = [hashed[0], *(key for key, _ in read([{"id": value} for value in raw]))]
    assert len(set(branches)) == len(branches)
    assert all(key.count("::") == 1 for key in branches)
    with pytest.raises(ManifestError, match="duplicate manifest id"):
        read([{"id": "abc"}, {"id": "abc", "other": 2}])


@pytest.mark.parametrize("state,source", [
    ({"node_outputs": {"plan": '{"items":[{"id":"a"}]}'}}, "plan"),
    ({"node_outputs": {"plan": '```json\n[{"id":"a"}]\n```'}}, "plan"),
    ({"node_structured_outputs": {"plan": {"nested": {"items": [{"id": "a"}]}}}}, "plan.nested.items"),
    ({"node_structured_outputs": {"plan": [{"id": "a"}]}, "node_outputs": {"plan": "other"}}, "plan"),
])
def test_supported_sources(state, source):
    assert _read_manifest(state, repeat(source)) == [("work::a", {"id": "a"})]


@pytest.mark.parametrize("state,source", [
    ({}, "plan"),
    ({"node_outputs": {"plan": "broken JSON"}}, "plan"),
    ({"node_structured_outputs": {"plan": {"items": []}}}, "plan"),
    ({"node_structured_outputs": {"plan": {}}}, "plan.tasks"),
    ({"node_outputs": {"plan": '{"tasks":[]}' }}, "plan.tasks"),
    ({"node_structured_outputs": {"plan": {"tasks": False}}}, "plan.tasks"),
])
def test_malformed_or_missing_source_fails_parent(state, source):
    dispatch = _make_fan_dispatcher(repeat(source), ["join"], Settings(), None)
    result = dispatch({**state, "completed_nodes": {"plan"}})
    assert result.goto == END
    assert result.update["failed_nodes"] == {"work"}
    assert source in result.update["errors"][0]["error"]


def test_file_source_is_workflow_relative_and_empty_is_valid(tmp_path):
    directory = tmp_path / "workflow"
    directory.mkdir()
    state = {"workdir": str(tmp_path)}
    (tmp_path / "manifest.json").write_text('["wrong"]')
    node = repeat({"file": "manifest.json"})
    with pytest.raises(ManifestError, match="file not found"):
        _read_manifest(state, node, workflow_dir=directory)
    path = directory / "manifest.json"
    path.write_text('{"items":[]}')
    assert _read_manifest(state, node, workflow_dir=directory) == []
    path.write_text(json.dumps([{"id": "from-file"}]))
    assert _read_manifest(state, node, workflow_dir=directory)[0][0] == "work::from-file"

def test_dotted_source_ignores_unrelated_sibling():
    state = {"node_structured_outputs": {"plan": {"work": [{"id": "right"}]},
                                        "plan.work": [{"id": "wrong"}]}}
    assert _read_manifest(state, repeat("plan.work"))[0][0] == "work::right"
    node = repeat("plan.work")
    node._ancestors = ["plan", "plan.work"]
    assert _read_manifest(state, node)[0][0] == "work::wrong"
