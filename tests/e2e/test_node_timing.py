import json
import sys

import yaml
from click.testing import CliRunner

from sqrlly.cli.main import cli


def test_two_commands_log_start_before_work_and_completion(tmp_path):
    log = tmp_path / "run.jsonl"
    verify_start = (
        "import json, pathlib, sys, time\n"
        "events = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]\n"
        "assert [(e['event'], e['node']) for e in events if e.get('node') == sys.argv[2]] == "
        "[('node_started', sys.argv[2])]\n"
        "time.sleep(0.04)\n"
    )
    config = tmp_path / "workflow.yaml"
    config.write_text(yaml.safe_dump({"nodes": [
        {"id": "prepare", "run": [sys.executable, "-c", verify_start, str(log), "prepare"]},
        {"id": "finish", "depends_on": ["prepare"],
         "run": [sys.executable, "-c", verify_start, str(log), "finish"]},
    ]}))
    result = CliRunner().invoke(cli, ["run", str(config), "--workdir", str(tmp_path), "--log", str(log)])
    assert result.exit_code == 0, result.output

    records = [json.loads(line) for line in log.read_text().splitlines()]
    node_records = [record for record in records if "node" in record]
    assert [(record["node"], record["event"]) for record in node_records] == [
        ("prepare", "node_started"), ("prepare", "node_completed"),
        ("finish", "node_started"), ("finish", "node_completed"),
    ]
    assert all(record["duration_s"] >= 0.04 for record in node_records[1::2])
    assert all("_started_at" not in record for record in records)


def test_failed_dispatch_gets_duration_but_skipped_nodes_get_no_start(tmp_path):
    log = tmp_path / "run.jsonl"
    config = tmp_path / "workflow.yaml"
    command = [sys.executable, "-c", "print('ran')"]
    config.write_text(yaml.safe_dump({"nodes": [
        {"id": "prepare", "run": command, "route": [{"when": "False", "then": "unused"}]},
        {"id": "unused", "run": command},
        {"id": "broken", "depends_on": ["prepare"], "run": [sys.executable, "-c", "raise SystemExit(7)"]},
        {"id": "blocked", "depends_on": ["broken"], "run": command},
    ]}))
    args = ["run", str(config), "--workdir", str(tmp_path), "--log", str(log)]
    runner = CliRunner()
    for options in ([], ["--resume"]):
        result = runner.invoke(cli, [*args, *options])
        assert result.exit_code == 1, result.output

    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert [record["node"] for record in records if record["event"] == "node_started"] == [
        "prepare", "broken", "broken",
    ]
    failures = [record for record in records if record["event"] == "node_failed"]
    assert [record["node"] for record in failures] == ["broken", "blocked", "broken", "blocked"]
    assert all(record["duration_s"] > 0 for record in failures[::2])
    assert all("duration_s" not in record for record in failures[1::2])


def test_fan_out_subgraph_events_use_full_node_ids(tmp_path):
    log = tmp_path / "run.jsonl"
    config = tmp_path / "workflow.yaml"
    (tmp_path / "items.json").write_text(json.dumps([{"id": "one"}, {"id": "two"}]))
    (tmp_path / "child.yaml").write_text(yaml.safe_dump({"nodes": [
        {"id": "work", "run": [sys.executable, "-c", "print('result')"]},
    ]}))
    config.write_text(yaml.safe_dump({"nodes": [
        {"id": "repeat", "for_each": {"file": "items.json"}, "workflow": "child.yaml"},
    ]}))
    result = CliRunner().invoke(cli, ["run", str(config), "--workdir", str(tmp_path), "--log", str(log)])
    assert result.exit_code == 0, result.output

    records = [json.loads(line) for line in log.read_text().splitlines()]
    expected = {"repeat", "repeat::one", "repeat::one::work", "repeat::two", "repeat::two::work"}
    assert {record["node"] for record in records if "node" in record} == expected
    for node in expected:
        start, finish = [record for record in records if record.get("node") == node]
        assert (start["event"], finish["event"]) == ("node_started", "node_completed")
        assert finish["duration_s"] > 0
