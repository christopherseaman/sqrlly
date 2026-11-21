import json
import sys

import pytest
import yaml
from click.testing import CliRunner

from sqrlly.cli.main import cli


def test_var_reaches_template(tmp_path):
    workflow = tmp_path / "workflow.yaml"
    log = tmp_path / "run.jsonl"
    workflow.write_text(yaml.safe_dump({"nodes": [
        {"id": "prepare", "run": {
            "cmd": [sys.executable, "-c", "import json, sys; from pathlib import Path; "
                    "Path('args.json').write_text(json.dumps(sys.argv[1:]))",
                    "{{vars.who}}", "{{vars.blank}}", "{{vars.items | default('fallback')}}",
                    "{{vars['keys'] | default('fallback')}}"],
        }, "eval": {"run": {
            "cmd": [sys.executable, "-c", "import os; print(os.environ['WHO'])"],
            "env": {"WHO": "{{vars.who}}"},
        }}, "route": [{"when": "vars.who != 'A B=C' or vars['_flag'] != 'go'", "then": "fail"}]},
        {"id": "deliver", "depends_on": ["prepare"], "workflow": {
            "file": "child.yaml", "with": {"label": "{{vars.who}}"},
        }},
    ]}))
    (tmp_path / "child.yaml").write_text(yaml.safe_dump({"nodes": [{
        "id": "write", "run": [sys.executable, "-c", "import sys; from pathlib import Path; "
                                "assert Path('ready').is_file(); Path('result').write_text(sys.argv[1])",
                                "{{workflow_inputs.label}}"],
    }]}))
    args = ["run", str(workflow), "--workdir", str(tmp_path), "--log", str(log)]
    runner = CliRunner()
    first = runner.invoke(cli, [*args, "--var", "who=A B=C", "--var", "blank=", "--var", "_flag=go"])
    assert first.exit_code == 1, first.output
    assert json.loads((tmp_path / "args.json").read_text()) == ["A B=C", "", "fallback", "fallback"]
    assert not (tmp_path / "result").exists()

    rejected = runner.invoke(cli, [*args, "--resume-from", "deliver", "--var", "who=Changed"])
    assert rejected.exit_code == 2 and "cannot be used with --resume" in rejected.output

    (tmp_path / "ready").touch()
    resumed = runner.invoke(cli, [*args, "--resume"])
    assert resumed.exit_code == 0, resumed.output
    assert (tmp_path / "result").read_text() == "A B=C"
    events = [json.loads(line) for line in log.read_text().splitlines()]
    evaluated = [event["result"]["text"].strip() for event in events if event["event"] == "gate_evaluated"]
    assert evaluated == ["A B=C"]
    invocations = {event["invocation_id"] for event in events if event["event"] == "workflow_start"}
    assert len(invocations) == 2
    assert {event["invocation_id"] for event in events} == invocations
    assert [event["status"] for event in events if event["event"] == "workflow_end"] == ["failed", "succeeded"]
    assert any(event.get("node") == "deliver::write" and event["event"] == "node_completed" for event in events)


@pytest.mark.parametrize("options", [
    ["--var", "novalue"], ["--var", "=bad"], ["--var", "a.b=bad"],
    ["--var", "a=one", "--var", "a=two"],
])
def test_malformed_var_fails_before_writing_checkpoint(tmp_path, options):
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(f"nodes: [{{id: no_op, run: ['{sys.executable}', '-V']}}]\n")
    result = CliRunner().invoke(cli, ["run", str(workflow), "--workdir", str(tmp_path), *options])
    assert result.exit_code == 2 and "--var" in result.output
    assert not (tmp_path / ".sqrlly-checkpoint.db").exists()


def test_resume_missing_checkpoint_logs_failed_invocation(tmp_path):
    workflow = tmp_path / "workflow.yaml"
    log = tmp_path / "run.jsonl"
    workflow.write_text(f"nodes: [{{id: no_op, run: ['{sys.executable}', '-V']}}]\n")
    result = CliRunner().invoke(cli, ["run", str(workflow), "--workdir", str(tmp_path),
                                      "--log", str(log), "--resume"])
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert result.exit_code == 1 and "No saved state" in result.output
    start, end = events
    assert [start["event"], end["event"]] == ["workflow_start", "workflow_end"]
    assert start["invocation_id"] == end["invocation_id"]
    assert (end["status"], end["failed"], end["failed_kinds"]) == ("failed", 0, {})
