"""Refresh item values through the CLI, SQLite checkpoints and Git worktrees."""
from collections import Counter
import json
from pathlib import Path
import subprocess

from click.testing import CliRunner
from langgraph.checkpoint.sqlite import SqliteSaver
import pytest
import yaml

from sqrlly.cli.main import cli


def workflow(directory, *, producer=False, worker=None):
    nodes = [{"id": "plan", "run": ["cat", "items.json"]}] if producer else []
    script = worker or 'echo "$1:$2" >> "$3"; printf %s "$2"'
    branches = {
        "id": "rows", "for_each": "plan" if producer else {"file": "items.json"},
        "run": ["sh", "-ec", script, "worker", "{{item.id}}", "{{item.value}}",
                str(directory.parent / "calls")],
    }
    if producer:
        branches["depends_on"] = ["plan"]
    nodes.extend([branches, {"id": "summary", "depends_on": ["rows"],
                             "run": ["sh", "-ec", 'echo "$1" >> summaries', "summary", "{{rows}}"]}])
    path = directory / "workflow.yaml"
    path.write_text(yaml.safe_dump({"nodes": nodes}))
    return ["run", str(path), "--workdir", str(directory)]


@pytest.fixture
def work(tmp_path):
    directory = tmp_path / "work"
    directory.mkdir()
    return directory


def write_items(directory, *rows):
    (directory / "items.json").write_text(json.dumps(rows))


def row(key, value):
    return {"id": key, "value": value}


def invoke(args, *flags, success=True):
    result = CliRunner().invoke(cli, [*args, *flags])
    assert (result.exit_code == 0) == success, (result.output, result.exception)
    return result.output


def calls(directory):
    return Counter((directory.parent / "calls").read_text().splitlines())


def summaries(directory):
    return [json.loads(line) for line in (directory / "summaries").read_text().splitlines()]


def test_file_refresh_compares_to_producing_inputs_after_plain_resume(work):
    args = workflow(work)
    a, b, c = row("a", 1), row("b", 2), row("c", 3)
    write_items(work, a, b)
    invoke(args)
    a["value"] = 11
    write_items(work, b, c, a)
    invoke(args, "--resume")
    assert summaries(work) == [["1", "2"], ["2", "3", "1"]]
    invoke(args, "--refresh-items")
    assert summaries(work)[-1] == ["2", "3", "11"]
    write_items(work, {"value": 2, "id": "b"}, c, a)
    invoke(args, "--refresh-items")
    assert len(summaries(work)) == 3
    write_items(work, a, c, b)
    invoke(args, "--refresh-items")
    assert summaries(work)[-1] == ["11", "3", "2"]
    assert calls(work) == {"a:1": 1, "b:2": 1, "c:3": 1, "a:11": 1}


def test_refresh_uses_producer_output_from_this_invocation(work):
    args = workflow(work, producer=True)
    write_items(work, row("a", 1), row("b", 2))
    invoke(args)
    write_items(work, row("b", 22), row("a", 1))
    invoke(args, "--refresh-items")
    assert summaries(work) == [["1", "2"]]
    invoke(args, "--refresh-items", "--resume-from", "plan")
    assert summaries(work)[-1] == ["22", "1"]
    assert calls(work) == {"a:1": 1, "b:2": 1, "b:22": 1}


def test_refresh_then_back_route_runs_every_item_in_the_next_wave(work):
    args = workflow(work, producer=True)
    path = Path(args[1])
    config = yaml.safe_load(path.read_text())
    config["nodes"].extend([
        {"id": "advance", "depends_on": ["summary"], "run": ["sh", "-ec",
         "if test -e repeat; then mv repeat repeated; echo again; else echo done; fi"]},
        {"id": "gate", "depends_on": ["advance"],
         "route": [{"when": "'again' in advance", "then": "plan", "max_attempts": 4}]},
    ])
    path.write_text(yaml.safe_dump(config))
    write_items(work, row("a", 1), row("b", 2))
    invoke(args)
    write_items(work, row("a", 1), row("b", 22))
    (work / "repeat").touch()
    invoke(args, "--refresh-items", "--resume-from", "plan")
    assert calls(work) == {"a:1": 2, "b:2": 1, "b:22": 2}
    assert summaries(work) == [["1", "2"], ["1", "22"], ["1", "22"]]


def test_legacy_checkpoint_refreshes_each_unknown_input_once(work):
    args = workflow(work)
    write_items(work, row("a", 1), row("b", 2))
    invoke(args)
    with SqliteSaver.from_conn_string(str(work / ".sqrlly-checkpoint.db")) as saver:
        latest = next(saver.list(None, limit=1))
        checkpoint = latest.checkpoint
        del checkpoint["channel_values"]["fan_inputs"]
        del checkpoint["channel_versions"]["fan_inputs"]
        saver.put(latest.parent_config, checkpoint, latest.metadata, {})
    invoke(args, "--resume")
    assert calls(work) == {"a:1": 1, "b:2": 1}
    invoke(args, "--refresh-items")
    invoke(args, "--refresh-items")
    assert calls(work) == {"a:1": 2, "b:2": 2}
    assert summaries(work) == [["1", "2"], ["1", "2"]]


def init_git(work):
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "-c", "user.name=Test", "-c", "user.email=test@example.org",
                    "commit", "--allow-empty", "-qm", "initial"], check=True)


def test_git_refresh_preserves_completed_sibling_across_failure(work):
    init_git(work)
    worker = ('echo "$1:$2" >> "$3"; test "$2" != broken; '
              'cat edition > "$1"; echo "$2" >> "$1"; printf %s "$2"')
    args = workflow(work, worker=worker)
    (work / "edition").write_text("first\n")
    write_items(work, row("a", 1), row("b", "broken"), row("c", 3))
    invoke(args, success=False)
    (work / "edition").write_text("second\n")
    write_items(work, row("a", 11), row("b", 2), row("c", 3))
    invoke(args, "--refresh-items")
    assert (work / "a").read_text() == "second\n11\n"
    assert (work / "b").read_text() == "second\n2\n"
    assert (work / "c").read_text() == "first\n3\n"
    assert summaries(work) == [["11", "2", "3"]]
    assert calls(work) == {"a:1": 1, "b:broken": 1, "c:3": 1, "a:11": 1, "b:2": 1}


@pytest.mark.parametrize("isolated", [False, True])
def test_refresh_reuses_a_repaired_worker_result_after_plain_resume(work, isolated):
    if isolated:
        init_git(work)
    args = workflow(work, worker='echo "$1:$2" >> "$3"; test "$2" != broken; printf %s "$2"')
    write_items(work, row("a", 1), row("b", "broken"))
    invoke(args, success=False)
    write_items(work, row("a", 1), row("b", 2))
    invoke(args, "--resume")
    invoke(args, "--refresh-items")
    assert calls(work) == {"a:1": 1, "b:broken": 1, "b:2": 1}
    assert summaries(work) == [["1", "2"]]


@pytest.mark.parametrize("subgraph", [False, True])
def test_refreshed_raw_output_replaces_structured_value_and_preserves_null(work, subgraph):
    args = workflow(work)
    path = Path(args[1])
    config = yaml.safe_load(path.read_text())
    rows = config["nodes"][0]
    (work / "value.json").write_text('{"type":["integer","null"]}')
    rows["outputs"] = {"schema": "value.json"}
    path.write_text(yaml.safe_dump(config))
    write_items(work, row("a", "1"), row("b", "null"))
    invoke(args)
    assert summaries(work) == [[1, None]]
    del rows["outputs"]
    if subgraph:
        del rows["run"]
        (work / "child.yaml").write_text(yaml.safe_dump({"nodes": [
            {"id": "emit", "run": ["printf", "%s", "{{value}}"]},
        ]}))
        rows["workflow"] = {"file": "child.yaml", "with": {"value": "{{item.value}}"}}
    path.write_text(yaml.safe_dump(config))
    write_items(work, row("a", "banana"), row("b", "null"))
    invoke(args, "--refresh-items")
    assert summaries(work)[-1] == ["banana", None]


@pytest.mark.parametrize("resume", ["--resume", "--refresh-items"])
def test_nested_git_recovery_stops_before_repeating_settled_side_effect(work, resume):
    init_git(work)
    args = workflow(work)
    path = Path(args[1])
    config = yaml.safe_load(path.read_text())
    rows = config["nodes"][0]
    del rows["run"]
    rows["workflow"] = {"file": "child.yaml"}
    calls_path = work.parent / "visited"
    ready = work.parent / "ready"
    (work / "child.yaml").write_text(yaml.safe_dump({"nodes": [
        {"id": "visited", "worktree": "isolated",
         "run": ["sh", "-ec", 'echo visited >> "$1"', "worker", str(calls_path)]},
        {"id": "finish", "depends_on": ["visited"], "run": ["test", "-e", str(ready)]},
    ]}))
    path.write_text(yaml.safe_dump(config))
    write_items(work, row("b", 1))
    invoke(args, success=False)
    ready.touch()
    log = work.parent / "recovered.jsonl"
    invoke(args, resume, "--log", str(log), success=False)
    failed = [event for line in log.read_text().splitlines() if
              (event := json.loads(line)).get("event") == "node_failed"]
    assert any(event["node"] == "rows::b::visited" and "checkpoint completion is missing" in event["error"]
               for event in failed)
    assert calls_path.read_text().splitlines() == ["visited"]
    assert not (work / "summaries").exists()


def test_refresh_preserves_prior_manifest_after_rejected_drift(work):
    args = workflow(work)
    write_items(work, row("a", 1), row("b", 2))
    invoke(args)
    write_items(work, row("b", 22))
    assert "DRIFTED" in invoke(args, "--refresh-items", success=False)
    assert calls(work) == {"a:1": 1, "b:2": 1}
    write_items(work, row("b", 22), row("a", 1))
    invoke(args, "--refresh-items")
    assert calls(work) == {"a:1": 1, "b:2": 1, "b:22": 1}
    assert summaries(work)[-1] == ["22", "1"]


@pytest.mark.parametrize("flags", [["--dry-run"], ["--entry", "rows"], ["--rerun-all"], ["--var", "source=x"]])
def test_refresh_rejects_conflicting_modes(work, flags):
    args = workflow(work)
    write_items(work)
    assert "--refresh-items cannot be combined" in invoke(args, "--refresh-items", *flags, success=False)
    assert not (work / ".sqrlly-checkpoint.db").exists()
