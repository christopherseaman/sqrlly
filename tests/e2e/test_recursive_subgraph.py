"""End-to-end recursive subgraph composition.

Multi-function flow tests following the testing methodology — known-good
artifacts (subgraph YAML files) drive concrete output assertions:

    1. Simple two-level: parent has one subgraph reference; subgraph
       has two nodes; assert subgraph terminal output projects to parent.
    2. Inputs projection: parent's `inputs:` declaration renders parent
       state into subgraph context; subgraph node uses {{input_var}}
       in its template.
    3. Multiple outputs: parent's `outputs:` exposes named subgraph
       node outputs; downstream parent nodes can consume them.
    4. Standalone runnability: the same YAML file is invokable both as
       a top-level workflow AND as a subgraph reference (proving the
       schema is identical).
    5. Cycle detection: A→B→A config chain raises SubgraphCycleError
       at compile time.
    6. Depth cap: nested chain longer than max_subgraph_depth raises.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.compile.subgraph import SubgraphCycleError, SubgraphDepthError
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph

_ECHO = shutil.which("echo") or "/bin/echo"
_FALSE = shutil.which("false") or "/bin/false"


def _yaml(path: Path, body: dict) -> None:
    path.write_text(yaml.safe_dump(body))


@pytest.mark.asyncio
async def test_simple_recursive_subgraph(tmp_path):
    """Parent has one subgraph reference; subgraph terminal output projects up."""
    _yaml(tmp_path / "sub.yaml", {
        "name": "Sub",
        "nodes": [
            {
                "id": "child_a",
                "name": "Child A",
                "run": [_ECHO, "-n", "from-child-a"],
            },
            {
                "id": "child_b",
                "name": "Child B",
                "depends_on": ["child_a"],
                "run": [_ECHO, "-n", "from-child-b"],
            },
        ],
    })
    _yaml(tmp_path / "parent.yaml", {
        "name": "Parent",
        "nodes": [
            {
                "id": "sub_node",
                "name": "Subgraph Reference",
                "workflow": "sub.yaml",
            },
        ],
    })

    raw = yaml.safe_load((tmp_path / "parent.yaml").read_text())
    parent_config = Graph(**raw)
    executor = DispatchExecutor(workdir=str(tmp_path))
    graph = build_workflow_graph(parent_config, executor, _base_dir=tmp_path)

    result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

    assert "sub_node" in result["completed_nodes"]
    # Default outputs: subgraph terminal node (child_b) → parent.node_outputs[sub_node]
    assert result["node_outputs"]["sub_node"] == "from-child-b"


@pytest.mark.asyncio
async def test_subgraph_runnable_standalone(tmp_path):
    """Same YAML file runs both standalone and as a subgraph reference.

    Proves graphs and subgraphs are definitionally identical — there's
    no special schema for a "subgraph YAML"; it's just a graph file.
    """
    _yaml(tmp_path / "shared.yaml", {
        "name": "Shared",
        "nodes": [{
            "id": "work",
            "name": "Work",
            "run": [_ECHO, "-n", "shared-work"],
        }],
    })

    # Standalone invocation
    raw = yaml.safe_load((tmp_path / "shared.yaml").read_text())
    standalone_config = Graph(**raw)
    executor = DispatchExecutor(workdir=str(tmp_path))
    standalone_graph = build_workflow_graph(standalone_config, executor)
    standalone_result = await standalone_graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
    assert standalone_result["node_outputs"]["work"] == "shared-work"

    # As a subgraph reference
    _yaml(tmp_path / "wrapper.yaml", {
        "name": "Wrapper",
        "nodes": [{"id": "ref", "name": "Ref", "workflow": "shared.yaml"}],
    })
    raw = yaml.safe_load((tmp_path / "wrapper.yaml").read_text())
    wrapper_config = Graph(**raw)
    wrapper_graph = build_workflow_graph(wrapper_config, executor, _base_dir=tmp_path)
    wrapper_result = await wrapper_graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
    assert wrapper_result["node_outputs"]["ref"] == "shared-work"


def test_cycle_detected_at_compile_time(tmp_path):
    _yaml(tmp_path / "a.yaml", {
        "name": "A",
        "nodes": [{"id": "x", "name": "X", "workflow": "b.yaml"}],
    })
    _yaml(tmp_path / "b.yaml", {
        "name": "B",
        "nodes": [{"id": "y", "name": "Y", "workflow": "a.yaml"}],
    })

    raw = yaml.safe_load((tmp_path / "a.yaml").read_text())
    config = Graph(**raw)
    executor = DispatchExecutor(workdir=str(tmp_path))
    with pytest.raises(SubgraphCycleError):
        build_workflow_graph(config, executor, _base_dir=tmp_path)


@pytest.mark.asyncio
async def test_subgraph_internal_failure_surfaces_to_parent(tmp_path):
    """Subgraph with a failing internal node → parent's failed_nodes[parent_id].

    Exercises the design rule: subgraph failures are NOT flattened into
    parent's state. The parent sees ONE failed_nodes entry (the parent
    node id), not the subgraph's internal node ids. This is encapsulation
    of the subgraph's internals.
    """
    _yaml(tmp_path / "sub.yaml", {
        "name": "Sub",
        "nodes": [{
            "id": "always_fails",
            "name": "Always Fails",
            "run": [_FALSE],  # exit 1
        }],
    })
    _yaml(tmp_path / "parent.yaml", {
        "name": "Parent",
        "nodes": [{
            "id": "sub_ref",
            "name": "Sub Ref",
            "workflow": "sub.yaml",
        }],
    })

    raw = yaml.safe_load((tmp_path / "parent.yaml").read_text())
    parent_config = Graph(**raw)
    executor = DispatchExecutor(workdir=str(tmp_path))
    graph = build_workflow_graph(parent_config, executor, _base_dir=tmp_path)
    result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

    assert "sub_ref" in result["failed_nodes"]
    # subgraph internal node id NOT flattened into parent state
    assert "always_fails" not in result["failed_nodes"]
    assert "sub_ref" not in result["completed_nodes"]
    error_strs = [e["error"] for e in result["errors"] if e["node"] == "sub_ref"]
    assert any("subgraph" in s.lower() for s in error_strs)


@pytest.mark.asyncio
async def test_subgraph_isolation_no_parent_state_leak(tmp_path):
    """Only explicitly passed parent inputs may appear in a child workflow."""
    _yaml(tmp_path / "sub.yaml", {
        "name": "Sub",
        "nodes": [{
            "id": "prober",
            "name": "Prober",
            "run": [_ECHO, "-n", "leak={{parent_only}}|input={{from_parent}}"],
        }],
    })
    _yaml(tmp_path / "parent.yaml", {
        "name": "Parent",
        "nodes": [
            {
                "id": "parent_only",
                "name": "Parent Only",
                "run": [_ECHO, "-n", "PARENT_VALUE"],
            },
            {
                "id": "sub_ref",
                "name": "Sub Ref",
                "workflow": {"file": "sub.yaml", "with": {"from_parent": "explicit-input"}},
                "depends_on": ["parent_only"],
            },
        ],
    })

    raw = yaml.safe_load((tmp_path / "parent.yaml").read_text())
    parent_config = Graph(**raw)
    executor = DispatchExecutor(workdir=str(tmp_path))
    with pytest.raises(ValueError, match="parent_only is not an ancestor or available input"):
        build_workflow_graph(parent_config, executor, _base_dir=tmp_path)


def test_depth_limit_enforced(tmp_path):
    """Chain of 12 nested subgraphs exceeds default max_subgraph_depth=10.

    Each yaml's only node references the next yaml; recursion bottoms
    out when build_workflow_graph hits _depth > max_subgraph_depth.
    """
    chain_len = 12
    for i in range(chain_len):
        next_ref = f"link_{i+1}.yaml" if i + 1 < chain_len else None
        node = {"id": f"n{i}", "name": f"N{i}"}
        if next_ref:
            node["workflow"] = next_ref
        else:
            node["run"] = [_ECHO, "-n", "leaf"]
        _yaml(tmp_path / f"link_{i}.yaml", {
            "name": f"L{i}",
            "nodes": [node],
        })

    raw = yaml.safe_load((tmp_path / "link_0.yaml").read_text())
    config = Graph(**raw)
    executor = DispatchExecutor(workdir=str(tmp_path))

    # Compile-time depth check: build_workflow_graph eagerly compiles
    # subgraphs (via make_subgraph_node), so the depth error fires at
    # compile time, not at invocation.
    with pytest.raises(SubgraphDepthError):
        build_workflow_graph(config, executor, _base_dir=tmp_path)


@pytest.mark.asyncio
async def test_absurd_paper_carve_compiles_with_subgraph(tmp_path):
    """examples/absurd-paper/workflow.yaml uses a workflow with inputs for the
    `paper` node. Asserts the carved workflow compiles, the `paper` node
    is present, and its workflow file resolves to the subgraph file.
    """
    example_dir = Path(__file__).resolve().parents[2] / "examples" / "absurd-paper"
    raw = yaml.safe_load(
        (example_dir / "workflow.yaml").read_text()
    )
    config = Graph(**raw)

    paper = next(n for n in config.nodes if n.id == "paper")
    assert paper.workflow is not None
    assert paper.workflow.file == "subgraphs/compose_and_validate.yaml"
    # Five upstream sections project into the subgraph context.
    assert set(paper.workflow.inputs) == {
        "abstract", "intro", "methods", "results", "discussion"
    }
    # render_pdf wires to the `paper` subgraph parent and is the terminal node.
    render_pdf = next(n for n in config.nodes if n.id == "render_pdf")
    assert render_pdf.depends_on == ["paper"]

    # Compile against the example dir as base (so config-ref resolves).
    executor = DispatchExecutor(workdir=str(example_dir))
    graph = build_workflow_graph(
        config, executor, _base_dir=example_dir,
    )
    assert graph is not None
