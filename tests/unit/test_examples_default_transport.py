"""Bundled examples must default to the Codex CLI.

A default ``sqrlly run <example>`` has to work on the headline
``uv tool install sqrlly`` / ``pipx install sqrlly`` install with only
``codex`` on PATH. So every LLM preset SELECTED by a default run — the
``default`` preset, or any preset named via ``preset:`` on a node / fan-out
template / final node — must be ``transport: cli, provider: openai``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from sqrlly.schema.models import Graph, LlmPreset

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"
EXAMPLE_YAMLS = sorted(EXAMPLES_DIR.rglob("*.yaml"))


def _selected_presets(path: Path) -> tuple[set[str], dict]:
    """Preset names a default run selects: the ``default`` preset plus every
    preset referenced via ``preset:`` anywhere in the file (node ``params``,
    fan-out templates, final nodes). Intersecting referenced names with the
    declared presets drops stray regex matches."""
    raw_text = path.read_text()
    graph = Graph(**yaml.safe_load(raw_text))
    presets = graph.settings.presets or {}
    selected = {name for name, p in presets.items() if getattr(p, "default", False)}
    referenced = set(re.findall(r"\bpreset:\s*['\"]?([A-Za-z0-9_]+)", raw_text))
    selected |= referenced & set(presets)
    return selected, presets


@pytest.mark.parametrize(
    "path", EXAMPLE_YAMLS, ids=lambda p: str(p.relative_to(EXAMPLES_DIR))
)
def test_example_default_llm_run_uses_codex_cli(path):
    selected, presets = _selected_presets(path)
    llm_selected = [
        (name, presets[name])
        for name in selected
        if isinstance(presets.get(name), LlmPreset)
    ]
    assert all(
        preset.transport == "cli" and preset.provider == "openai"
        for _, preset in llm_selected
    ), (
        f"{path.relative_to(EXAMPLES_DIR)} has a non-Codex selected LLM "
        f"preset: {[(name, p.transport, p.provider) for name, p in llm_selected]!r}"
    )


def test_guard_covers_every_example_yaml():
    """The parametrization must actually discover the example tree — an empty
    sweep would make the guard above vacuously pass."""
    assert len(EXAMPLE_YAMLS) >= 8, EXAMPLE_YAMLS
    names = {p.name for p in EXAMPLE_YAMLS}
    assert {"workflow.yaml", "smoke_test.yaml"} <= names
