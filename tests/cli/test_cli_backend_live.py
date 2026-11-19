"""Full E2E: CLI transport generates jokes -> deterministic gate
validates JSON schema -> CLI transport selects best joke. Exercises
the entire pipeline with real ``codex exec`` subprocess execution.

A second case sends one prompt through the ``claude -p`` provider.

Lives under ``tests/cli/`` because it requires ``codex`` and ``claude`` on PATH;
the collection-time pre-flight in ``tests/conftest.py`` and the
``--ignore=tests/cli`` exclusion both apply.
"""
from __future__ import annotations

import json
import subprocess

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.backends.factory import create_backend
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state

from helpers import make_config


@pytest.mark.cli
class TestJokeWorkflowCLIIntegration:
    @pytest.mark.asyncio
    async def test_generate_gate_select(self, tmp_path):
        generate_prompt = tmp_path / "generate.md"
        generate_prompt.write_text(
            "Generate exactly 3 short jokes. Respond with ONLY valid JSON, "
            "no markdown, no code fences:\n"
            '{"jokes": ["joke 1", "joke 2", "joke 3"]}'
        )
        select_prompt = tmp_path / "select.md"
        select_prompt.write_text(
            "Here are some jokes:\n\n{{generate}}\n\n"
            "Pick the funniest one. Respond with ONLY the joke text."
        )

        validator = tmp_path / "validate.py"
        validator.write_text(
            "import json, sys\n"
            "raw = sys.stdin.read().strip()\n"
            "try:\n"
            "    data = json.loads(raw)\n"
            "    jokes = data.get('jokes', [])\n"
            "    if isinstance(jokes, list) and len(jokes) == 3 "
            "and all(isinstance(j, str) and j for j in jokes):\n"
            "        print('1.0')\n"
            "    else:\n"
            "        print('0.0')\n"
            "except Exception:\n"
            "    print('0.0')\n"
        )

        config = make_config(
            [
                {
                    "id": "generate",
                    "name": "Generate Jokes",
                    "llm": "generate.md",
                    "evaluation": {
                        "validator": str(validator),
                        "threshold": 1.0,
                        "blocking": True,
                        "max_retries": 2,
                    },
                },
                {
                    "id": "select",
                    "name": "Select Best",
                    "llm": "select.md",
                    "depends_on": ["generate"],
                },
            ],
            prompts=".",
        )
        executor = DispatchExecutor(workdir=str(tmp_path), settings=config.settings)
        try:
            graph = build_workflow_graph(config, executor)
            result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

            assert "generate" in result["completed_nodes"]
            assert "select" in result["completed_nodes"]
            assert result["evaluations"]["generate"][-1]["result"]["score"] == 1.0

            gen_output = result["node_outputs"]["generate"]
            data = json.loads(gen_output)
            assert len(data["jokes"]) == 3

            assert len(result["node_outputs"]["select"]) > 0
        finally:
            await executor.close()


@pytest.mark.cli
class TestClaudeProviderLive:
    @pytest.mark.asyncio
    async def test_anthropic_llm_round_trip(self, tmp_path):
        """An Anthropic llm node dispatches through a real ``claude -p`` subprocess."""
        backend = create_backend("anthropic")
        try:
            result = await backend.send_prompt(
                "Reply with exactly the single word: pong", "haiku", str(tmp_path)
            )
            assert result.output.strip().strip(".").lower() == "pong"
        finally:
            await backend.close()


@pytest.mark.cli
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_llm_cannot_read_local_file(tmp_path, provider):
    from secrets import token_hex

    token = token_hex(16)
    (tmp_path / "probe.txt").write_text(token)
    backend = create_backend(provider)
    result = await backend.send_prompt(
        "Use a file tool to read probe.txt. Reply with only its contents, "
        "or only UNAVAILABLE if you cannot access the file.",
        None, str(tmp_path), timeout=90,
    )
    assert token not in result.output
    assert result.output.strip().upper().rstrip(".") == "UNAVAILABLE"


@pytest.mark.cli
@pytest.mark.asyncio
async def test_codex_llm_disables_configured_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "config.toml").write_text('[mcp_servers."test.server"]\ncommand = "false"\n')
    backend = create_backend()
    args = await backend._disabled_mcp_args(str(tmp_path))
    configured = subprocess.check_output(
        ["codex", "mcp", "list", "--json", *args], cwd=tmp_path, text=True,
    )
    assert [(server["name"], server["enabled"]) for server in json.loads(configured)] == [
        ("test.server", False)
    ]


@pytest.mark.cli
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_schema_field_reaches_route_and_template(tmp_path, provider):
    from sqrlly.schema.models import load_workflow

    (tmp_path / "field.json").write_text('{"type":"integer"}')
    (tmp_path / "count.json").write_text(json.dumps({
        "type": "object", "additionalProperties": False,
        "properties": {"count": {"$ref": "field.json"}}, "required": ["count"],
    }))
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text("""
name: Structured count
nodes:
  - id: count_items
    llm:
      text: Return a count of exactly seven.
      provider: PROVIDER
    timeout: 90
    outputs: {schema: count.json}
  - id: choose
    depends_on: [count_items]
    run: [echo, '{{count_items.count}}']
    route:
      cases:
        - when: count_items.count == 7
          goto: show
      else: wrong
  - id: show
    run: [echo, correct]
  - id: wrong
    run: ['false']
""".replace("PROVIDER", provider))
    config = load_workflow(workflow)
    executor = DispatchExecutor(workdir=str(tmp_path), settings=config.settings)
    graph = build_workflow_graph(config, executor)
    state = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
    assert state["node_structured_outputs"]["count_items"] == {"count": 7}
    assert state["node_outputs"]["choose"].strip() == "7"
    assert state["node_outputs"]["show"].strip() == "correct"
    assert "wrong" not in state["completed_nodes"]


@pytest.mark.cli
@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["openai", "anthropic"])
async def test_provider_returns_array_schema(tmp_path, provider):
    schema = tmp_path / "array.json"
    schema.write_text(json.dumps({"type": "array", "items": {"type": "integer"}}))
    backend = create_backend(provider, output_schema=schema)
    result = await backend.send_prompt("Return the single-item array [7].", None, str(tmp_path), timeout=90)
    assert result.structured_output == [7]
    assert json.loads(result.output) == [7]
