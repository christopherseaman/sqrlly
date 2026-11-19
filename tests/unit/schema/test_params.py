"""Unit tests for schema/params.py: per-mode dataclasses + URL resolver.

Function-level tests cover known-good and known-bad pairs:
    - Each per-mode model parses its expected keys
    - Each per-mode model rejects mode-mismatched keys
    - params_for_url returns the right model type per extension
    - coerce_params surfaces ValidationError on typos
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sqrlly.schema.models import Graph
from sqrlly.schema.params import (
    PromptParams,
    SubgraphParams,
    SubprocessParams,
    coerce_params,
    params_for_url,
)


class TestPromptParams:
    def test_parses_known_keys(self):
        p = PromptParams(preset="smart", timeout=30.0)
        assert p.preset == "smart"
        assert p.timeout == 30.0

    def test_defaults_to_none(self):
        p = PromptParams()
        assert p.preset is None
        assert p.timeout is None

    def test_rejects_args_key(self):
        with pytest.raises(ValidationError):
            PromptParams(args=["x"])

    def test_rejects_typo(self):
        with pytest.raises(ValidationError):
            PromptParams(preset_name="smart")  # should be `preset`

    def test_rejects_removed_model_key(self):
        """`model:` on PromptParams was removed in the preset rework;
        the migrator rewrites it as `preset:` references. Test pins
        the rejection so the key doesn't sneak back."""
        with pytest.raises(ValidationError):
            PromptParams(model="opus")


class TestSubgraphParams:
    def test_parses_inputs_outputs(self):
        p = SubgraphParams(
            inputs={"topic": "{{paper}}"},
            outputs={"summary": "{{step2}}"},
        )
        assert p.inputs == {"topic": "{{paper}}"}
        assert p.outputs == {"summary": "{{step2}}"}

    def test_rejects_args_key(self):
        with pytest.raises(ValidationError):
            SubgraphParams(args=["x"])

    def test_rejects_model_key(self):
        with pytest.raises(ValidationError):
            SubgraphParams(model="opus")


class TestSubprocessParams:
    """script + direct-exec collapsed to one model (Stage 5b cleanup)."""

    def test_parses_args_env(self):
        p = SubprocessParams(args=["--flag", "value"], env={"X": "y"})
        assert p.args == ["--flag", "value"]
        assert p.env == {"X": "y"}

    def test_rejects_model_key(self):
        with pytest.raises(ValidationError):
            SubprocessParams(model="opus")

    def test_rejects_inputs_key(self):
        with pytest.raises(ValidationError):
            SubprocessParams(inputs={"x": "y"})


class TestParamsForURL:
    @pytest.mark.parametrize("ext", [".md", ".txt", ".prompt"])
    def test_prompt_extensions(self, ext):
        assert params_for_url(f"file:///x/y{ext}") is PromptParams

    @pytest.mark.parametrize("ext", [".yaml", ".yml"])
    def test_subgraph_extensions(self, ext):
        assert params_for_url(f"file:///x/y{ext}") is SubgraphParams

    @pytest.mark.parametrize("ext", [".py", ".js", ".mjs", ".ts", ".sh"])
    def test_script_extensions(self, ext):
        assert params_for_url(f"file:///x/y{ext}") is SubprocessParams

    def test_no_extension_falls_through_to_subprocess(self):
        assert params_for_url("file:///bin/echo") is SubprocessParams

    def test_unknown_extension_falls_through_to_subprocess(self):
        assert params_for_url("file:///x/y.unknown") is SubprocessParams

    def test_https_scheme_routes_by_extension(self):
        assert params_for_url("https://x.com/y.md") is PromptParams
        assert params_for_url("https://x.com/y.yaml") is SubgraphParams
        assert params_for_url("https://x.com/y.py") is SubprocessParams

    def test_case_insensitive_extension(self):
        assert params_for_url("file:///x/Y.MD") is PromptParams
        assert params_for_url("file:///x/Y.YAML") is SubgraphParams

    def test_explicit_mode_overrides_extension(self):
        assert params_for_url("file:///x/y.py", mode="prompt") is PromptParams

    def test_unknown_mode_raises_value_error(self):
        with pytest.raises(ValueError) as ei:
            params_for_url("file:///x/y.md", mode="bogus")
        assert "bogus" in str(ei.value)


class TestCoerceParams:
    def test_coerce_prompt_url_with_preset(self):
        p = coerce_params("file:///x.md", {"preset": "smart"})
        assert isinstance(p, PromptParams)
        assert p.preset == "smart"

    def test_coerce_subgraph_url_with_inputs(self):
        p = coerce_params("file:///sub.yaml", {"inputs": {"x": "y"}})
        assert isinstance(p, SubgraphParams)
        assert p.inputs == {"x": "y"}

    def test_coerce_rejects_mode_mismatch(self):
        # `args:` on a prompt URL is a typo / mode confusion
        with pytest.raises(ValidationError):
            coerce_params("file:///x.md", {"args": ["x"]})

    def test_coerce_empty_dict(self):
        p = coerce_params("file:///x.md", {})
        assert isinstance(p, PromptParams)


@pytest.mark.parametrize(("url", "params", "key"), [
    ("prompt.md", {"args": ["wrong"]}, "args"),
    ("prompt.md", {"agent": "claude"}, "agent"),
    ("script.sh", {"model": "opus"}, "model"),
    ("child.yaml", {"input": {"x": "y"}}, "input"),
    ("?rev=2", {"timout": 2}, "timout"),
])
def test_graph_rejects_invalid_params_at_load(minimal_config_dict, url, params, key):
    minimal_config_dict["nodes"][0]["execute"] = {"url": url, "params": params}
    with pytest.raises(ValidationError, match=key):
        Graph.model_validate(minimal_config_dict)


@pytest.mark.parametrize("ref", ["", "?rev=2", "#section"])
def test_pathless_ref_uses_prompt_base(minimal_config_dict, ref):
    execute = {"url": ref, "params": {"timeout": 2}}
    minimal_config_dict["settings"] = {"base_url": "https://example.org/template.md"}
    minimal_config_dict["nodes"][0]["execute"] = execute
    graph = Graph.model_validate(minimal_config_dict)
    assert graph.nodes[0].execute.params == {"timeout": 2}
    execute["params"] = {"args": ["wrong"]}
    with pytest.raises(ValidationError, match="args"):
        Graph.model_validate(minimal_config_dict)
    execute["mode"] = "exec"
    assert Graph.model_validate(minimal_config_dict).nodes[0].execute.mode == "exec"


@pytest.mark.parametrize("location", ["template", "final_nodes"])
def test_fan_out_checks_params_against_base(minimal_config_dict, location):
    step = {"execute": {"url": "?rev=2", "params": {"args": []}}}
    fan_out = {"template": step} if location == "template" else {
        "final_nodes": [{"id": "final", "name": "Final", **step}]
    }
    minimal_config_dict["settings"] = {"base_url": "/prompts/template.md"}
    minimal_config_dict["nodes"][0]["fan_out"] = fan_out
    with pytest.raises(ValidationError, match="args"):
        Graph.model_validate(minimal_config_dict)


def test_absolute_double_slash_ignores_prompt_base(minimal_config_dict):
    node = minimal_config_dict["nodes"][0]
    node["execute"] = {"url": "//hostname", "params": {"args": ["ok"]}}
    minimal_config_dict["settings"] = {"base_url": "/template.md"}
    graph = Graph.model_validate(minimal_config_dict)
    assert graph.nodes[0].execute.params == {"args": ["ok"]}
