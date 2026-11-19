import pytest
from pydantic import ValidationError

from sqrlly.schema.models import Graph, load_workflow


def test_route_destinations_and_static_fallback():
    graph = Graph(nodes=[
        {"id": "build", "eval": {"llm": {"text": "judge"}}, "route": [
            {"when": "eval.score < 1", "then": "retry", "max_attempts": 3},
            {"when": "eval.score < 1", "then": ["review", "log"], "context": "all"},
        ]},
        {"id": "ship", "depends_on": ["build"]}, {"id": "review"}, {"id": "log"},
    ])
    first, second = graph.nodes[0].route
    assert first.targets == ["retry"]
    assert first.context == "last"
    assert second.targets == ["review", "log"]
    assert graph.nodes[1].depends_on == ["build"]


@pytest.mark.parametrize(("fields", "error"), [
    ({"id": "fail"}, "reserved node id"),
    ({"id": "retry"}, "reserved node id"),
    ({"eval": {"agent": {"text": "review"}}}, "requires a route"),
    ({"eval": {"llm": {"text": "review"}}}, "requires a route"),
    ({"eval": {"run": "true", "llm": {"text": "review"}}}, "exactly one"),
    ({"eval": {"run": "true", "model": "model"}}, "cannot set model"),
    ({"eval": {"run": "true", "threshold": 1}}, "threshold"),
    ({"evaluation": {"validator": "true"}}, "evaluation"),
    ({"route": {"goto": "fail"}}, "valid list"),
    ({"route": [{"then": "x", "include_eval": True}]}, "include_eval"),
    ({"route": [{"then": "x", "else": "fail"}]}, "exactly one"),
    ({"route": [{"else": "fail"}, {"then": "x"}]}, "last"),
    ({"route": [{"else": "x", "when": "True"}]}, "predicate"),
    ({"route": [{"then": ["retry", "x"]}]}, "single"),
    ({"route": [{"then": "retry", "max_attempts": 0}]}, "greater than 0"),
    ({"route": [{"else": "ghost"}]}, "unknown node ghost"),
    ({"fan_out": {}, "route": [{"else": "x"}]}, "fan_out.*route"),
])
def test_invalid_route(fields, error):
    with pytest.raises(ValidationError, match=error):
        Graph(nodes=[{"id": "x", **fields}])


def test_eval_files_follow_settings_prompts(tmp_path):
    path = tmp_path / "workflow.yaml"
    path.write_text("""
settings: {prompts: reviews}
nodes:
  - id: build
    run: echo test
    eval: {llm: judge.md, outputs: {schema: result.json}}
    route: [{else: fail}]
""")
    with pytest.raises(ValueError, match="judge.md"):
        load_workflow(path)
    (tmp_path / "reviews").mkdir()
    (tmp_path / "reviews/judge.md").write_text("Judge")
    with pytest.raises(ValueError, match="result.json"):
        load_workflow(path)
    (tmp_path / "result.json").write_text("{}")
    assert load_workflow(path).nodes[0].eval.outputs.schema_file == "result.json"
