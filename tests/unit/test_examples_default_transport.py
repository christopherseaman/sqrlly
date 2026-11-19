"""The shipped prompt examples run with the default Codex provider."""
from pathlib import Path

import pytest
import yaml

EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples"


@pytest.mark.parametrize("path", sorted(EXAMPLES_DIR.rglob("*.yaml")), ids=lambda path: str(path.relative_to(EXAMPLES_DIR)))
def test_example_default_llm_run_uses_codex_cli(path):
    workflow = yaml.safe_load(path.read_text())
    defaults = workflow.get("settings", {}).get("defaults", {})
    for node in workflow["nodes"]:
        fan_out = node.get("fan_out", {})
        for step in [node, fan_out.get("template", {}), *fan_out.get("final_nodes", [])]:
            for kind in ("llm", "agent"):
                if kind not in step:
                    continue
                options = step[kind] if isinstance(step[kind], dict) else {}
                provider = options.get("provider", defaults.get(kind, {}).get("provider", "openai"))
                assert provider == "openai", (path, node["id"], provider)
