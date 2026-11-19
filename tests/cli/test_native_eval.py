"""Real provider evaluations use the same schema transport as workflow nodes."""

import json

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import load_workflow


@pytest.mark.cli
@pytest.mark.parametrize("provider,custom", [("openai", False), ("anthropic", True)])
async def test_native_eval_schema(tmp_path, provider, custom):
    (tmp_path / "integer.json").write_text('{"type":"integer"}')
    schema = {
        "type": "object", "additionalProperties": False, "required": ["rating"],
        "properties": {"rating": {"$ref": "integer.json"}},
    }
    (tmp_path / "rating.json").write_text(json.dumps(schema))
    check = "eval.rating == 7" if custom else "eval.score == 7"
    prompt = {"text": "Set the numeric result to 7. Explain briefly if asked.", "provider": provider}
    evaluation = {"llm": prompt}
    if custom:
        evaluation = {"agent": {**prompt, "tools": []}, "outputs": {"schema": "rating.json"}}
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(json.dumps({"nodes": [{
        "id": "answer", "run": ["echo", "seven"], "eval": evaluation,
        "route": [{"when": check, "then": "confirmed"}, {"else": "fail"}],
    }, {"id": "confirmed", "run": ["echo", "accepted"]}]}))
    config = load_workflow(workflow)
    dispatch = DispatchExecutor(str(tmp_path), config.settings)
    graph = build_workflow_graph(config, dispatch)
    state = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
    assert state["node_outputs"]["confirmed"].strip() == "accepted"
    fields = state["evaluations"]["answer"][0]["result"]
    assert fields["rating" if custom else "score"] == 7
    assert fields["text"]
    if not custom:
        assert isinstance(fields["explanation"], str)
