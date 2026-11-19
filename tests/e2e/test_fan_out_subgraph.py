"""Execute a child workflow for each item and collect its terminal output."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.compile.subgraph import SubgraphCycleError
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph

_ECHO = shutil.which("echo") or "/bin/echo"
_FALSE = shutil.which("false") or "/bin/false"


def _yaml(path: Path, body: dict) -> None:
    path.write_text(yaml.safe_dump(body))


def _parent_with_fan_out_subgraph(items, sub_yaml_name="single.yaml",
                                  inputs_decl=None):
    """Build a parent-graph dict whose fan-out template runs a subgraph."""
    manifest = json.dumps({"items": items})
    workflow = {"file": sub_yaml_name}
    if inputs_decl:
        workflow["with"] = inputs_decl
    return {
        "name": "Parent",
        "nodes": [
            {"id": "manifest",
             "run": [_ECHO, "-n", manifest]},
            {"id": "manifest_emitter",
             "for_each": "manifest",
             "depends_on": ["manifest"],
             "workflow": workflow},
        ],
    }


@pytest.mark.asyncio
async def test_fan_out_per_child_subgraph_dispatch(tmp_path):
    """2-item manifest → 2 Send branches → each runs a 2-node subgraph.

    Asserts both items' terminal outputs land in `node_outputs[parent::id]`.
    """
    # 2-node subgraph: step1 echoes input, step2 echoes f"final-{step1}".
    _yaml(tmp_path / "single.yaml", {
        "name": "Single",
        "nodes": [
            {
                "id": "step1",
                "name": "Step 1",
                "run": [_ECHO, "{{topic}}"],
            },
            {
                "id": "step2",
                "name": "Step 2",
                "depends_on": ["step1"],
                "run": [_ECHO, "final-{{step1}}"],
            },
        ],
    })

    items = [
        {"id": "alpha", "topic": "ALPHA"},
        {"id": "beta", "topic": "BETA"},
    ]
    parent_dict = _parent_with_fan_out_subgraph(
        items, inputs_decl={"topic": "{{item.topic}}"},
    )
    parent_config = Graph(**parent_dict)
    executor = DispatchExecutor(workdir=str(tmp_path))
    graph = build_workflow_graph(parent_config, executor, _base_dir=tmp_path)
    result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

    assert "manifest_emitter::alpha" in result["completed_nodes"]
    assert "manifest_emitter::beta" in result["completed_nodes"]

    # Each branch's terminal output is the subgraph's terminal node (step2)
    # which echoes "final-<topic>\n" — but echo strips the literal
    # newline because subprocess buffers it. Either way "final-ALPHA"
    # appears in the alpha-branch output, "final-BETA" in beta-branch.
    alpha_output = result["node_outputs"]["manifest_emitter::alpha"]
    beta_output = result["node_outputs"]["manifest_emitter::beta"]
    assert "final-ALPHA" in alpha_output, f"alpha got: {alpha_output!r}"
    assert "final-BETA" in beta_output, f"beta got: {beta_output!r}"
    # Per-item isolation: alpha branch must NOT see beta's topic
    assert "BETA" not in alpha_output
    assert "ALPHA" not in beta_output


@pytest.mark.asyncio
async def test_fan_out_subgraph_failure_surfaces_to_parent(tmp_path):
    """A failing subgraph node flips the branch's child_id into failed_nodes."""
    _yaml(tmp_path / "fail_sub.yaml", {
        "name": "Fail Sub",
        "nodes": [
            {
                "id": "always_fails",
                "name": "Always Fails",
                # `false` always exits 1
                "run": [_FALSE],
            },
        ],
    })

    items = [{"id": "doomed"}]
    parent_dict = _parent_with_fan_out_subgraph(
        items, sub_yaml_name="fail_sub.yaml",
    )
    parent_config = Graph(**parent_dict)
    executor = DispatchExecutor(workdir=str(tmp_path))
    graph = build_workflow_graph(parent_config, executor, _base_dir=tmp_path)
    result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

    # Subgraph internal failure surfaces as fan-out child failure on parent.
    assert "manifest_emitter::doomed" in result["failed_nodes"]
    assert "manifest_emitter::doomed" not in result["completed_nodes"]


@pytest.mark.asyncio
async def test_fan_out_subgraph_cycle_detected_at_compile_time(tmp_path):
    """Template subgraph that references back into the parent's chain
    raises SubgraphCycleError at compile (not runtime)."""
    # a.yaml references b.yaml; b.yaml references a.yaml
    _yaml(tmp_path / "a.yaml", {
        "name": "A",
        "nodes": [
            {
                "id": "ref_to_b",
                "name": "ref to b",
                "workflow": "b.yaml",
            },
        ],
    })
    _yaml(tmp_path / "b.yaml", {
        "name": "B",
        "nodes": [
            {
                "id": "ref_to_a",
                "name": "ref to a",
                "workflow": "a.yaml",
            },
        ],
    })

    items = [{"id": "x"}]
    parent_dict = _parent_with_fan_out_subgraph(
        items, sub_yaml_name="a.yaml",
    )
    parent_config = Graph(**parent_dict)
    executor = DispatchExecutor(workdir=str(tmp_path))
    # Cycle is detected during the subgraph compilation that the fan-out
    # invoker triggers — happens at parent-graph compile time, before
    # any runtime ainvoke.
    with pytest.raises(SubgraphCycleError):
        build_workflow_graph(parent_config, executor, _base_dir=tmp_path)


@pytest.mark.asyncio
async def test_fan_out_subgraph_inputs_render_per_item(tmp_path):
    """Each Send branch's `inputs:` render against its own manifest fields.

    The subgraph receives `{{persona}}` rendered freshly per branch. If
    state leaked, both branches would see the same persona.
    """
    _yaml(tmp_path / "persona_sub.yaml", {
        "name": "Persona Sub",
        "nodes": [
            {
                "id": "greet",
                "name": "Greet",
                "run": [_ECHO, "hello-{{persona}}"],
            },
        ],
    })

    items = [
        {"id": "x", "persona": "wizard"},
        {"id": "y", "persona": "rogue"},
    ]
    parent_dict = _parent_with_fan_out_subgraph(
        items, sub_yaml_name="persona_sub.yaml",
        inputs_decl={"persona": "{{item.persona}}"},
    )
    parent_config = Graph(**parent_dict)
    executor = DispatchExecutor(workdir=str(tmp_path))
    graph = build_workflow_graph(parent_config, executor, _base_dir=tmp_path)
    result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

    x_out = result["node_outputs"]["manifest_emitter::x"]
    y_out = result["node_outputs"]["manifest_emitter::y"]
    assert "hello-wizard" in x_out
    assert "hello-rogue" in y_out
    # Cross-branch isolation
    assert "rogue" not in x_out
    assert "wizard" not in y_out
