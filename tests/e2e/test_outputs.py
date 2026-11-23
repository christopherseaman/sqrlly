import json
import sys

import pytest
import yaml
from click.testing import CliRunner

from sqrlly.cli.main import cli
from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.schema.models import load_workflow


def workflow(directory, nodes):
    path = directory / "workflow.yaml"
    path.write_text(yaml.safe_dump({"nodes": nodes}))
    return path


@pytest.mark.parametrize(("nodes", "message"), [
    ([{"id": "a", "run": ["echo", "{{b}}"]}, {"id": "b", "run": "true"}], "not an ancestor"),
    ([{"id": "a", "run": "true"}, {"id": "b", "run": ["echo", "{{a.title}}"], "depends_on": ["a"]}], "requires outputs.schema"),
    ([{"id": "a", "llm": {"text": "category"}}, {"id": "b", "depends_on": ["a"],
       "route": [{"when": "a == 'ok'", "then": "fail"}]}], "comparing a text"),
])
def test_invalid_references_fail_validate(tmp_path, nodes, message):
    result = CliRunner().invoke(cli, ["validate", str(workflow(tmp_path, nodes))])
    assert result.exit_code == 1
    assert message in result.output


@pytest.mark.parametrize("repair", [True, False])
async def test_schema_retry_and_transitive_binding(tmp_path, repair):
    (tmp_path / "schema.json").write_text(json.dumps({
        "type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"],
    }))
    script = tmp_path / "report.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        "feedback = sys.argv[1]\n"
        "with Path('attempts').open('a') as f: f.write(str(bool(feedback)) + '\\n')\n"
        f"print('{{\"count\":3}}' if {repair!r} and 'required property' in feedback else '{{}}')\n"
    )
    path = workflow(tmp_path, [
        {"id": "report", "run": [sys.executable, str(script), "{{_retry_reason}}"],
         "outputs": {"schema": "schema.json", "files": ["att*"]}},
        {"id": "middle", "depends_on": ["report"], "run": "echo middle"},
        {"id": "consume", "depends_on": ["middle"], "run": ["echo", "{{report}}={{report.count}}"]},
    ])
    cfg = load_workflow(path)
    graph = build_workflow_graph(cfg, DispatchExecutor(str(tmp_path), cfg.settings))
    result = await graph.ainvoke({"workdir": str(tmp_path)})
    attempts = (tmp_path / "attempts").read_text().splitlines()
    if repair:
        assert attempts == ["False", "True"]
        assert result["node_structured_outputs"]["report"] == {"count": 3}
        assert result["node_outputs"]["consume"] == '{"count":3}\n=3\n'
    else:
        assert attempts == ["False", "True", "True"]
        assert "report" in result["failed_nodes"]
        assert "required property" in result["errors"][0]["error"]


def test_invalid_schema_fails_validate(tmp_path):
    (tmp_path / "bad.json").write_text('{"type": "unknown"}')
    path = workflow(tmp_path, [{"id": "report", "run": "echo 1", "outputs": {"schema": "bad.json"}}])
    result = CliRunner().invoke(cli, ["validate", str(path)])
    assert result.exit_code == 1
    assert "invalid JSON schema" in result.output


def test_local_refs_resolve_across_documents_and_anchors(tmp_path):
    from jsonschema import ValidationError, validate

    (tmp_path / "nested").mkdir()
    (tmp_path / "types").mkdir()
    (tmp_path / "types/field.json").write_text(json.dumps({
        "$anchor": "number", "type": "integer",
    }))
    (tmp_path / "nested/value.json").write_text(json.dumps({
        "$id": "offset/new.json",
        "type": "object",
        "properties": {"count": {"$ref": "../../types/field.json#number"}},
        "required": ["count"],
    }))
    (tmp_path / "schema.json").write_text(json.dumps({
        "type": "object", "properties": {"value": {"$ref": "nested/value.json"}},
    }))
    path = workflow(tmp_path, [{"id": "sample", "run": "echo 1", "outputs": {"schema": "schema.json"}}])
    outputs = load_workflow(path).nodes[0].outputs
    for verify in (outputs.validate_value, lambda value: validate(value, outputs.schema_document)):
        verify({"value": {"count": 1}})
        with pytest.raises(ValidationError):
            verify({"value": {"count": "wrong"}})


@pytest.mark.parametrize("ref", ["https://example.invalid/schema", "../../outside.json", "escape.json"])
def test_nested_schema_reference_cannot_escape(tmp_path, ref):
    (tmp_path / ".git").mkdir()
    root = tmp_path / "workflow"
    (root / "nested").mkdir(parents=True)
    (tmp_path / "outside.json").write_text("{}")
    (root / "nested/escape.json").symlink_to(tmp_path / "outside.json")
    (root / "schema.json").write_text('{"$ref":"nested/types.json"}')
    (root / "nested/types.json").write_text(json.dumps({"$ref": ref}))
    path = workflow(root, [{"id": "sample", "run": "echo 1", "outputs": {"schema": "schema.json"}}])
    result = CliRunner().invoke(cli, ["validate", str(path)])
    assert result.exit_code == 1
    assert "unsupported external" in result.output or "resolves outside" in result.output


async def test_fan_child_can_read_parent_schema_fields(tmp_path):
    schema = {"type": "object", "properties": {
        "items": {"type": "array"}, "title": {"type": "string"}, "nested": {"type": "object"},
    }}
    (tmp_path / "schema.json").write_text(json.dumps(schema))
    data = {"title": "hi", "nested": {"items": "ready"}, "items": [{"id": "a"}]}
    path = workflow(tmp_path, [{
        "id": "plan", "run": ["echo", json.dumps(data)], "outputs": {"schema": "schema.json"},
    }, {
        "id": "write", "depends_on": ["plan"], "for_each": "plan.items",
        "run": ["echo", "{{plan.title}} {{plan.nested.items}}"],
    }])
    cfg = load_workflow(path)
    result = await build_workflow_graph(cfg, DispatchExecutor(str(tmp_path), cfg.settings)).ainvoke(
        {"workdir": str(tmp_path)}
    )
    assert result["node_outputs"]["write::a"] == "hi ready\n"


def test_native_array_envelope_preserves_references_into_the_root(tmp_path):
    from jsonschema import ValidationError, validate
    from sqrlly.schema.json_schema import load_validator, provider_envelope

    path = tmp_path / "schema.json"
    path.write_text(json.dumps({
        "type": "array", "prefixItems": [{"type": "integer"}, {"$ref": "#/prefixItems/0"}],
    }))
    schema, _ = load_validator(path, tmp_path)
    native = provider_envelope(schema)
    validate({"value": [1, 2]}, native)
    with pytest.raises(ValidationError):
        validate({"value": [1, "wrong"]}, native)


@pytest.mark.parametrize(("step", "message"), [
    ({"eval": {"run": ["echo", "{{later}}"]}}, "later is not an ancestor"),
    ({"route": [{"then": "fail", "context": "{{later}}"}]}, "later is not an ancestor"),
    ({"eval": {"run": ["echo", "{{writer.title}}"]}}, "field access requires outputs.schema"),
    ({"route": [{"when": "writer == 'ok'", "then": "fail"}]}, "comparing writer text"),
])
def test_measurement_and_context_references_fail_at_load(tmp_path, step, message):
    path = workflow(tmp_path, [
        {"id": "writer", "agent": {"text": "work"}, **step},
        {"id": "later", "run": "echo later"},
    ])
    with pytest.raises(ValueError, match=message):
        load_workflow(path)


def test_eval_can_read_current_output_and_load_local_schema(tmp_path):
    (tmp_path / "field.json").write_text('{"type":"integer"}')
    (tmp_path / "result.json").write_text('{"type":"object","properties":{"count":{"$ref":"field.json"}}}')
    path = workflow(tmp_path, [{
        "id": "report", "run": ["echo", '{"count": 2}'], "outputs": {"schema": "result.json"},
        "eval": {"llm": {"text": "{{node_id}} {{attempt}} {{report.count}}"},
                 "outputs": {"schema": "result.json"}},
        "route": [{"when": "eval.count == report.count", "then": "fail",
                   "context": "{{report.count}} {{eval.count}} {{history | length}}"}],
    }])
    node = load_workflow(path).nodes[0]
    assert node.eval.outputs.schema_document == node.outputs.schema_document
    assert "$defs" in node.eval.outputs.schema_document


async def test_child_input_namespace_standalone_and_called(tmp_path):
    params = {"source": "written", "items": ["one", "two"], "workflow_inputs": "same name"}
    child = tmp_path / "child.yaml"
    child.write_text(yaml.safe_dump({"nodes": [{
        "id": "consume", "run": ["echo", "{{workflow_inputs.source}}",
                                "{{workflow_inputs.items | join(',')}}",
                                "{{workflow_inputs['workflow_inputs']}}"],
    }]}))
    assert CliRunner().invoke(cli, ["validate", str(child)]).exit_code == 0
    path = workflow(tmp_path, [{"id": "child", "workflow": {"file": "child.yaml", "with": params}}])
    config = load_workflow(path)
    executor = DispatchExecutor(str(tmp_path), config.settings)
    result = await build_workflow_graph(config, executor).ainvoke({"workdir": str(tmp_path)})
    assert result["node_outputs"]["child"] == "written one,two same name\n"

    child.write_text(child.read_text().replace("workflow_inputs.source", "workflow_inputs.missing"))
    validation = CliRunner().invoke(cli, ["validate", str(path)])
    assert validation.exit_code == 1
    assert "workflow.with does not supply 'missing'" in validation.output
    child.write_text(child.read_text().replace("workflow_inputs.missing", "typo"))
    validation = CliRunner().invoke(cli, ["validate", str(child)])
    assert validation.exit_code == 1
    assert "typo is not an ancestor" in validation.output


async def test_ancestor_output_named_workflow_inputs_wins(tmp_path):
    (tmp_path / "schema.json").write_text('{"type":"object"}')
    path = workflow(tmp_path, [
        {"id": "workflow_inputs", "run": ["echo", '{"source":"ancestor"}'], "outputs": {"schema": "schema.json"}},
        {"id": "consumer", "depends_on": ["workflow_inputs"], "run": ["echo", "{{workflow_inputs.source}}"]},
    ])
    cfg = load_workflow(path)
    result = await build_workflow_graph(cfg, DispatchExecutor(str(tmp_path), cfg.settings)).ainvoke(
        {"workdir": str(tmp_path)}
    )
    assert result["node_outputs"]["consumer"] == "ancestor\n"
