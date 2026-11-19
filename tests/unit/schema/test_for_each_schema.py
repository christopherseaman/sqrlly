import pytest
import yaml

from sqrlly.schema.models import Graph, load_workflow


def load(tmp_path, nodes):
    (tmp_path / "schema.json").write_text('{"type":"object"}')
    (tmp_path / "items.json").write_text('["one"]')
    path = tmp_path / "flow.yaml"
    path.write_text(yaml.safe_dump({"nodes": nodes}))
    return load_workflow(path)


@pytest.mark.parametrize("source", ["unrelated", "unrelated.tasks", "plan.tasks"])
def test_source_requires_an_ancestor_and_a_schema_for_fields(tmp_path, source):
    with pytest.raises(ValueError, match="ancestor|outputs.schema"):
        load(tmp_path, [
            {"id": "unrelated", "run": "echo unrelated"},
            {"id": "plan", "run": "echo raw"},
            {"id": "build", "depends_on": ["plan"], "for_each": source, "run": "echo build"},
        ])


def test_transitive_dotted_source_and_child_item_bindings(tmp_path):
    (tmp_path / "child.yaml").write_text("nodes: [{id: report, run: echo child}]")
    graph = load(tmp_path, [
        {"id": "plan.part", "run": "echo raw", "outputs": {"schema": "schema.json"}},
        {"id": "middle", "depends_on": ["plan.part"]},
        {"id": "build", "depends_on": ["middle"], "for_each": "plan.part.data.tasks",
         "workflow": {"file": "child.yaml", "with": {"value": "{{item}}", "name": "{{item.title}}"}}},
    ])
    assert graph.nodes[-1].for_each == "plan.part.data.tasks"
    assert graph.nodes[-1].workflow.inputs == {"value": "{{item}}", "name": "{{item.title}}"}


@pytest.mark.parametrize("template", ["{{title}}", "{{sibling}}"])
def test_item_shape_does_not_introduce_free_names(tmp_path, template):
    with pytest.raises(ValueError, match="not an ancestor"):
        load(tmp_path, [
            {"id": "sibling"},
            {"id": "build", "for_each": {"file": "items.json"}, "run": ["echo", template]},
        ])


@pytest.mark.parametrize("projection", [
    {"run": ["echo", "{{build.field}}"]},
    {"run": ["echo", '{{build["field"]}}']},
    {"run": "echo next", "for_each": "build.field"},
    {"route": [{"when": "build.field == 1", "then": "fail"}]},
])
def test_repeating_node_publishes_a_list_even_with_child_schema(tmp_path, projection):
    with pytest.raises(ValueError, match="for_each result list"):
        load(tmp_path, [
            {"id": "build", "run": "echo build", "for_each": {"file": "items.json"},
             "outputs": {"schema": "schema.json"}},
            {"id": "next", "depends_on": ["build"], **projection},
        ])


def test_repeating_node_output_can_be_indexed(tmp_path):
    graph = load(tmp_path, [
        {"id": "build", "run": "echo build", "for_each": {"file": "items.json"}},
        {"id": "next", "depends_on": ["build"], "run": ["echo", "{{build[0]}}"]},
    ])
    assert graph.nodes[1].depends_on == ["build"]


@pytest.mark.parametrize("node_id", ["parent::child", "outer|inner"])
def test_authored_ids_cannot_contain_checkpoint_separators(node_id):
    with pytest.raises(ValueError, match="reserved node id"):
        Graph(nodes=[{"id": node_id, "run": "echo x"}])


def test_join_cannot_repeat():
    with pytest.raises(ValueError, match="requires an executable kind"):
        Graph(nodes=[{"id": "just_join", "for_each": {"file": "items.json"}}])


def test_repeating_eval_needs_child_schema_for_own_fields(tmp_path):
    with pytest.raises(ValueError, match="build field access requires outputs.schema"):
        load(tmp_path, [{
            "id": "build", "for_each": {"file": "items.json"}, "run": "echo build",
            "eval": {"run": ["echo", "{{build.label}}"]},
        }])
