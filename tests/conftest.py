import shutil
from pathlib import Path

import pytest

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def pytest_collection_modifyitems(config, items):
    """Hard pre-req checks — fail collection loudly instead of letting tests error
    cryptically at runtime."""
    cli_tests = [
        i for i in items if "tests/cli/" in str(i.fspath).replace("\\", "/")
    ]
    missing = [c for c in ("codex", "claude") if shutil.which(c) is None]
    if cli_tests and missing:
        pytest.exit(
            f"CLI transport tests collected but not on PATH: {', '.join(missing)}.\n"
            "Install and authenticate the Codex and Claude CLIs, or run with "
            "`--ignore=tests/cli`.",
            returncode=4,
        )

    node_tests = [
        i
        for i in items
        if "TestGateJSValidator" in i.nodeid or i.nodeid.endswith("::test_js_validator_returns_score")
    ]
    if node_tests and shutil.which("node") is None:
        pytest.exit(
            "JS gate tests collected but `node` not on PATH.\n"
            "Install Node.js (https://nodejs.org) or deselect the JS gate tests.",
            returncode=4,
        )


@pytest.fixture
def minimal_config_dict():
    return {
        "name": "Test Workflow",
        "nodes": [
            {
                "id": "node-1",
                "name": "First Node",
                "agent": "nodes/node-1.md",
            }
        ],
    }


@pytest.fixture
def multi_phase_config_dict():
    return {
        "name": "Multi Node",
        "nodes": [
            {
                "id": "node-1",
                "name": "First",
                "agent": "nodes/node-1.md",
            },
            {
                "id": "node-2",
                "name": "Second",
                "agent": "nodes/node-2.md",
                "depends_on": ["node-1"],
            },
        ],
    }


@pytest.fixture
def parallel_config_dict():
    """Diamond dependency: A -> (B, C) -> D"""
    return {
        "name": "Parallel Workflow",
        "nodes": [
            {"id": "a", "name": "A", "agent": "a.md"},
            {"id": "b", "name": "B", "agent": "b.md", "depends_on": ["a"]},
            {"id": "c", "name": "C", "agent": "c.md", "depends_on": ["a"]},
            {
                "id": "d",
                "name": "D",
                "agent": "d.md",
                "depends_on": ["b", "c"],
            },
        ],
    }


@pytest.fixture
def kitchen_sink_workflow_path():
    """Full-feature workflow used as a CLI / schema fixture: prompt nodes,
    fan-out, evaluations, subgraph composition, and route nodes all in one.

    The absurd-paper workflow is the canonical example of every Stage 4-5b
    feature combined; CLI tests use it to verify validate / graph / dry-run
    over a non-trivial graph.
    """
    return EXAMPLES_DIR / "absurd-paper" / "workflow.yaml"
