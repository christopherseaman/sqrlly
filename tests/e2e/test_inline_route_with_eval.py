"""Command measurements, ordered cases and checkpointed attempts through the public CLI."""

import sys

import pytest
import yaml
from click.testing import CliRunner

from helpers import init_git_repo
from sqrlly.cli.main import cli
from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.executor.prompt import prepend_eval_preamble
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph


@pytest.mark.parametrize("context", ["last", "all", "none"])
@pytest.mark.asyncio
async def test_exhausted_retry_escalates_with_context(tmp_path, context):
    predicate = "eval.exit_code != 0"
    config = Graph(nodes=[
        {"id": "draft", "run": ["echo", "draft"],
         "eval": {"run": [sys.executable, "-c", "print('review {{attempt}}'); raise SystemExit(1)"]},
         "route": [{"when": predicate, "then": "retry", "max_attempts": 3},
                   {"when": predicate, "then": "escalate", "context": context}]},
        {"id": "escalate", "run": ["echo", "-n", "{{_route_eval_preamble}}"]},
    ])
    graph = build_workflow_graph(config, DispatchExecutor(str(tmp_path)))
    state = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
    assert [r["attempt"] for r in state["evaluations"]["draft"]] == [1, 2, 3]
    feedback = state["node_outputs"]["escalate"]
    prompt = prepend_eval_preamble("Diagnose draft", {"_route_eval_preamble": feedback})
    if context == "none":
        assert prompt == "Diagnose draft"
    else:
        assert "Attempt 3" in prompt and "review 3" in prompt
        assert ("review 1" in prompt) == (context == "all")


def test_exhausted_case_names_limit_and_resume_continues_attempts(tmp_path):
    workflow = tmp_path / "workflow.yaml"
    trace = tmp_path / "attempts.txt"
    workflow.write_text(yaml.safe_dump({"nodes": [{
        "id": "draft", "run": ["echo", "draft"],
        "eval": {"run": [sys.executable, "-c",
                         "import sys; print(sys.argv[1], file=open(sys.argv[2], 'a')); sys.exit(1)",
                         "{{attempt}}", str(trace)]},
        "route": [{"when": "eval.exit_code != 0", "then": "retry", "max_attempts": 3}],
    }]}))
    args = ["run", str(workflow), "--workdir", str(tmp_path)]
    first = CliRunner().invoke(cli, args)
    assert first.exit_code != 0
    assert "max_attempts" in first.output and "attempt 3" in first.output
    assert trace.read_text().splitlines() == ["1", "2", "3"]
    resumed = CliRunner().invoke(cli, [*args, "--resume"])
    assert resumed.exit_code != 0
    assert trace.read_text().splitlines() == ["1", "2", "3", "4"]


def test_command_eval_reads_file_only_in_node_worktree(tmp_path):
    init_git_repo(tmp_path)
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(yaml.safe_dump({"nodes": [{
        "id": "build", "worktree": "isolated", "promote": False,
        "run": [sys.executable, "-c", "from pathlib import Path; Path('private.txt').write_text('built')"],
        "eval": {"run": [sys.executable, "-c",
                         "from pathlib import Path; assert Path('private.txt').read_text() == 'built'"]},
    }]}))
    result = CliRunner().invoke(cli, ["run", str(workflow), "--workdir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "private.txt").exists()
