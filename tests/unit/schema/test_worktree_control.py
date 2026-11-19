import pytest
from pydantic import ValidationError

from sqrlly.schema.models import Graph, Node, Settings


@pytest.mark.parametrize(("node", "serial", "fan_child", "promote"), [
    ({"agent": {"text": "edit"}}, "isolated", "isolated", True),
    ({"run": "pwd"}, "off", "isolated", True),
    ({"llm": {"text": "read"}}, "off", "off", False),
    ({"workflow": "child.yaml"}, "off", "off", False),
    ({}, "off", "off", False),
])
def test_kind_defaults(node, serial, fan_child, promote):
    node = Node(id="step", **node)
    assert node.effective_worktree(Settings()) == serial
    assert node.effective_worktree(Settings(), fan_child=True) == fan_child
    assert node.effective_promote(Settings()) == promote


def test_node_overrides_kind_default():
    settings = Settings(defaults={"agent": {"worktree": "off", "promote": False}})
    node = Node(id="edit", agent={"text": "edit"})
    assert node.effective_worktree(settings) == "off"
    assert node.effective_promote(settings) is False
    node = Node(id="edit", agent={"text": "edit"}, worktree="isolated", promote=True)
    assert node.effective_worktree(settings) == "isolated"
    assert node.effective_promote(settings) is True


@pytest.mark.parametrize(("key", "graph"), [
    ("worktree", {"settings": {"worktree": "off"}}),
    ("worktree_group", {"settings": {"worktree_group": "team"}}),
    ("worktree_group", {"nodes": [{"id": "n", "worktree_group": "team"}]}),
    ("worktree_gc", {"settings": {"worktree_gc": "never"}}),
    ("promote_include", {"settings": {"promote_include": []}}),
    ("worktree_setup_exclude", {"settings": {"worktree_setup_exclude": []}}),
    ("fan_out", {"nodes": [{"id": "n", "fan_out": {"promote": True}}]}),
    ("fan_out", {"nodes": [{"id": "n", "fan_out": {"template": {"worktree": "isolated"}}}]}),
])
def test_removed_controls_raise(key, graph):
    with pytest.raises(ValidationError, match=key):
        Graph.model_validate({"nodes": [{"id": "n"}], **graph})


@pytest.mark.parametrize("kind", [{"llm": {"text": "read"}}, {"workflow": "child.yaml"}, {}])
@pytest.mark.parametrize("control", [{"worktree": "off"}, {"promote": False}])
def test_other_kinds_reject_explicit_controls(kind, control):
    with pytest.raises(ValidationError, match="only applies to agent and run"):
        Node(id="n", **kind, **control)


@pytest.mark.parametrize("value", ["auto", "none", False])
def test_removed_node_worktree_modes_raise(value):
    with pytest.raises(ValidationError, match="worktree"):
        Node(id="n", run="true", worktree=value)
