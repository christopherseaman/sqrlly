"""Route back edges and cold entry through the CLI and SQLite checkpointer."""

import sys

import yaml
from click.testing import CliRunner

from sqrlly.cli.main import cli


def workflow(tmp_path, nodes):
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump({"nodes": nodes}))
    return ["run", str(path), "--workdir", str(tmp_path)]


def test_entry_bypasses_router_and_resumes_selected_branch(tmp_path):
    args = workflow(tmp_path, [
        {"id": "router", "run": ["touch", "router-ran"], "route": [{"then": "other"}]},
        {"id": "other", "run": ["touch", "other-ran"], "route": [{"then": "selected"}]},
        {"id": "selected", "run": ["sh", "-c", "echo selected >> calls"], "route": [{"then": "finish"}]},
        {"id": "finish", "run": ["sh", "-c", "test -f ready && echo finish >> calls"]},
    ])
    first = CliRunner().invoke(cli, [*args, "--entry", "selected"])
    assert first.exit_code == 1, first.output
    assert (tmp_path / "calls").read_text().splitlines() == ["selected"]

    (tmp_path / "ready").touch()
    second = CliRunner().invoke(cli, [*args, "--resume"])
    assert second.exit_code == 0, second.output
    assert (tmp_path / "calls").read_text().splitlines() == ["selected", "finish"]
    assert not (tmp_path / "router-ran").exists()
    assert not (tmp_path / "other-ran").exists()


def test_join_waits_for_current_round_after_upstream_route(tmp_path):
    start = "from pathlib import Path; p=Path('round'); n=int(p.read_text())+1 if p.exists() else 1; p.write_text(str(n)); print(n)"
    args = workflow(tmp_path, [
        {"id": "fixed", "run": ["echo", "fixed"]},
        {"id": "start", "depends_on": ["fixed"], "run": [sys.executable, "-c", start],
         "route": [{"then": ["short", "long1"]}]},
        {"id": "short", "run": ["cat", "round"]},
        {"id": "long1", "run": ["cat", "round"]},
        {"id": "long2", "depends_on": ["long1"], "run": ["cat", "round"],
         "eval": {"run": ["true"]}},
        {"id": "join", "depends_on": ["short", "long2", "fixed"],
         "run": ["sh", "-c", 'echo "$1-$2" >> pairs', "sh", "{{short}}", "{{long2}}"],
         "route": [{"when": "attempt < 2", "then": "start", "max_attempts": 2}]},
    ])
    run = CliRunner().invoke(cli, args)
    assert run.exit_code == 0, run.output
    assert (tmp_path / "pairs").read_text().splitlines() == ["1-1", "2-2"]


def test_nested_upstream_routes_keep_each_join_round_separate(tmp_path):
    args = workflow(tmp_path, [
        {"id": "outer", "run": ["sh", "-c", "echo outer >> outer_calls"]},
        {"id": "inner", "depends_on": ["outer"], "run": ["sh", "-c", "echo inner >> inner_calls"]},
        {"id": "left", "depends_on": ["inner"], "run": ["wc", "-l", "inner_calls"]},
        {"id": "right1", "depends_on": ["inner"], "run": ["true"]},
        {"id": "right2", "depends_on": ["right1"], "run": ["wc", "-l", "inner_calls"]},
        {"id": "join", "depends_on": ["left", "right2"],
         "run": ["sh", "-c", 'test "$1" = "$2" && echo joined >> joins', "sh", "{{left}}", "{{right2}}"],
         "route": [{"when": "attempt in (1, 3)", "then": "inner", "max_attempts": 4}]},
        {"id": "finish", "depends_on": ["join"], "run": ["true"],
         "route": [{"when": "attempt == 1", "then": "outer", "max_attempts": 2}]},
    ])
    run = CliRunner().invoke(cli, args)
    assert run.exit_code == 0, run.output
    assert (tmp_path / "outer_calls").read_text().splitlines() == ["outer"] * 2
    assert (tmp_path / "inner_calls").read_text().splitlines() == ["inner"] * 4
    assert (tmp_path / "joins").read_text().splitlines() == ["joined"] * 4
