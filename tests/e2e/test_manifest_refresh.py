"""Refresh item values through the CLI, SQLite checkpoints and Git worktrees."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

from click.testing import CliRunner
from langgraph.checkpoint.sqlite import SqliteSaver
import pytest
import yaml

from sqrlly.cli.main import cli
from sqrlly.runtime import foreman
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.snapshots import SnapshotRun


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


def nested_git_workflow(work, *, routed=False, repo=None):
    init_git(repo if repo is not None else work)
    args = workflow(work)
    path = Path(args[1])
    config = yaml.safe_load(path.read_text())
    rows = config["nodes"][0]
    del rows["run"]
    rows["workflow"] = {"file": "child.yaml", "with": {"key": "{{item.id}}", "value": "{{item.value}}"}}
    ready = work.parent / "ready"
    schema = work / "value.json"
    schema.write_text('{"type": ["null", "object"]}')
    node = {
        "id": "visited", "worktree": "isolated",
        "run": ["sh", "-ec",
                'printf "%s:%s\\n" "$1" "$2" >> "$3"; printf %s "$2" > "artifact-$1"; '
                'if [ "$1" = b ]; then printf null; else printf \'{"value":"%s"}\' "$2"; fi',
                "worker", "{{key}}", "{{value}}", str(work.parent / "calls")],
        "outputs": {"files": ["artifact-*"], "schema": schema.name},
    }
    if routed:
        node["route"] = [
            {"when": "attempt == 1", "then": "retry"},
            {"then": "finish"},
        ]
        node["run"][2] += '; grep -c "^$1:" "$3" > "$4-$1"'
        node["run"].append(str(work.parent / "attempt"))
    (work / "child.yaml").write_text(yaml.safe_dump({"nodes": [
        node,
        {"id": "finish", "depends_on": ["visited"],
         "run": ["sh", "-ec",
                 'if [ "$1" = b ]; then test -e "$5"; fi; printf "%s:%s:%s" "$1" "$2" "$3"; '
                 'test "$4" = None || test "$4" = "$2"',
                 "finish", "{{key}}", "{{value}}", "{{visited}}",
                 "{{visited_structured.value if visited_structured is not none else 'None'}}", str(ready)]},
    ]}))
    path.write_text(yaml.safe_dump(config))
    write_items(work, row("b", 1), row("a", 2))
    return args, ready


@pytest.mark.parametrize("resume", ["--resume", "--refresh-items"])
def test_nested_git_recovery_reuses_settled_result(work, resume):
    args, ready = nested_git_workflow(work)
    invoke(args, success=False)
    assert (work / "artifact-b").read_text() == "1"
    assert (work / "artifact-a").read_text() == "2"
    assert not list((work / ".sqrlly").glob("wt-*"))
    assert calls(work) == {"b:1": 1, "a:2": 1}
    assert not (work / "summaries").exists()

    ready.touch()
    invoke(args, resume)
    assert calls(work) == {"b:1": 1, "a:2": 1}
    assert summaries(work) == [['b:1:null', 'a:2:{"value":"2"}']]
    assert not list((work / ".sqrlly").glob("wt-*"))


@pytest.mark.parametrize("change_alias", [False, True], ids=["same-alias", "changed-alias"])
def test_nested_git_recovery_checks_template_aliases(work, change_alias):
    args, ready = nested_git_workflow(work)
    path = Path(args[1])
    config = yaml.safe_load(path.read_text())
    bindings = config["nodes"][0]["workflow"]["with"]
    bindings["left"] = [1]
    bindings["right"] = bindings["left"]
    path.write_text(yaml.safe_dump(config))
    child = work / "child.yaml"
    child_config = yaml.safe_load(child.read_text())
    visited, finish = child_config["nodes"]
    visited["run"][5] = "{{ value ~ ':' ~ (left is sameas right) }}"
    finish["run"][2] = finish["run"][2].replace('test "$4" = "$2"', 'test "$4" = "$2:True"')
    child.write_text(yaml.safe_dump(child_config))

    invoke(args, success=False)
    expected_calls = {"b:1:True": 1, "a:2:True": 1}
    assert calls(work) == expected_calls
    assert (work / "artifact-b").read_text() == "1:True"
    assert (work / "artifact-a").read_text() == "2:True"
    assert not list((work / ".sqrlly").glob("wt-*"))
    assert not (work / "summaries").exists()

    if change_alias:
        bindings["right"] = [1]
        path.write_text(yaml.safe_dump(config))
    ready.touch()
    log = work.parent / "alias-resume.jsonl"
    invoke(args, "--resume", "--log", str(log), success=not change_alias)
    assert calls(work) == expected_calls
    assert (work / "artifact-b").read_text() == "1:True"
    assert (work / "artifact-a").read_text() == "2:True"
    assert not list((work / ".sqrlly").glob("wt-*"))
    if change_alias:
        assert "settled receipt is unavailable or mismatched" in log.read_text()
        assert not (work / "summaries").exists()
    else:
        assert summaries(work) == [['b:1:null', 'a:2:{"value":"2:True"}']]


def test_nested_git_recovery_preserves_structured_key_identity(work):
    args, ready = nested_git_workflow(work)
    child = work / "child.yaml"
    config = yaml.safe_load(child.read_text())
    visited, finish = config["nodes"]
    (work / "value.json").write_text('{"type": "array"}')
    visited["run"][2] = (
        'printf "%s:%s\\n" "$1" "$2" >> "$3"; printf %s "$2" > "artifact-$1"; '
        """printf '%s' '[{"same long key":1},{"same long key":1}]'"""
    )
    config["nodes"].insert(1, {
        "id": "observe", "depends_on": ["visited"], "worktree": "isolated",
        "run": ["sh", "-ec", 'printf "observe-%s:%s\\n" "$1" "$2" >> "$3"; '
                'printf %s "$2" | tee "observed-$1"',
                "observe", "{{key}}",
                "{{ (visited_structured[0]|list|first) is sameas "
                "(visited_structured[1]|list|first) }}", str(work.parent / "calls")],
    })
    finish["depends_on"] = ["observe"]
    finish["run"] = ["sh", "-ec", 'if [ "$1" = b ]; then test -e "$3"; fi; printf "%s:%s" "$1" "$2"',
                     "finish", "{{key}}", "{{observe}}", str(ready)]
    child.write_text(yaml.safe_dump(config))

    invoke(args, success=False)
    expected_calls = {"b:1": 1, "a:2": 1, "observe-b:True": 1, "observe-a:True": 1}
    assert calls(work) == expected_calls
    assert not (work / "summaries").exists()
    assert not list((work / ".sqrlly").glob("wt-*"))

    ready.touch()
    invoke(args, "--resume")
    assert calls(work) == expected_calls
    assert summaries(work) == [["b:True", "a:True"]]
    assert [(work / f"artifact-{key}").read_text() for key in ("b", "a")] == ["1", "2"]
    assert [(work / f"observed-{key}").read_text() for key in ("b", "a")] == ["True", "True"]
    assert not list((work / ".sqrlly").glob("wt-*"))


def test_nested_git_recovery_preserves_escaped_surrogate(work):
    args, ready = nested_git_workflow(work)
    workflow_path = Path(args[1])
    workflow_config = yaml.safe_load(workflow_path.read_text())
    workflow_config["nodes"][1]["run"][2] = 'printf "%s\\n" "$1" >> summaries'
    workflow_path.write_text(yaml.safe_dump(workflow_config))
    child = work / "child.yaml"
    config = yaml.safe_load(child.read_text())
    visited, finish = config["nodes"]
    (work / "value.json").write_text(
        '{"type":"object","required":["value"],"properties":{"value":{"type":"string"}}}'
    )
    stdout = r'{"value":"\ud800"}'
    visited["run"][2] = (
        'printf "%s:%s\\n" "$1" "$2" >> "$3"; printf %s "$2" > "artifact-$1"; '
        f"""printf '%s' '{stdout}'"""
    )
    finish["run"] = [
        "sh", "-ec", 'if [ "$1" = b ]; then test -e "$4"; fi; printf "%s:%s:%s" "$1" "$2" "$3"',
        "finish", "{{key}}", "{{visited}}", "{{visited_structured.value|length}}", str(ready),
    ]
    child.write_text(yaml.safe_dump(config))

    invoke(args, success=False)
    expected_calls = {"b:1": 1, "a:2": 1}
    assert calls(work) == expected_calls
    assert [(work / f"artifact-{key}").read_text() for key in ("b", "a")] == ["1", "2"]
    assert not list((work / ".sqrlly").glob("wt-*"))
    assert not (work / "summaries").exists()

    ready.touch()
    invoke(args, "--resume")
    assert calls(work) == expected_calls
    assert [(work / f"artifact-{key}").read_text() for key in ("b", "a")] == ["1", "2"]
    assert not list((work / ".sqrlly").glob("wt-*"))
    assert summaries(work) == [[r'b:{"value":"\ud800"}:1', r'a:{"value":"\ud800"}:1']]


@pytest.mark.parametrize("interrupt", [False, True], ids=["fresh", "recovered"])
def test_nested_git_retained_dispatch_preserves_stdout_identity(work, monkeypatch, interrupt):
    args, _ = nested_git_workflow(work)
    child = work / "child.yaml"
    producers = [
        {"id": producer, "worktree": "isolated", "promote": False,
         "run": ["sh", "-ec", 'printf "%s:%s\n" "$1" "$2" >> "$3"; '
                 'printf %s "same long stdout" | tee "artifact-$1-$2"',
                 "producer", producer, "{{key}}", str(work.parent / "calls")]}
        for producer in ("left", "right")
    ]
    finish = {
        "id": "finish", "depends_on": ["left", "right"], "worktree": "off",
        "run": ["sh", "-ec", 'printf "%s:%s:%s:%s" "$1" "$2" "$3" "$4"', "finish",
                "{{key}}", "{{left is sameas right}}", "{{left}}", "{{right}}"],
    }
    child.write_text(yaml.safe_dump({"nodes": [*producers, finish]}))
    expected_calls = {"left:b": 1, "right:b": 1, "left:a": 1, "right:a": 1}

    if interrupt:
        def lose_settlement(executor, node, settings, ids):
            assert node.id in {"left", "right"}
            raise OSError("lost settlement after dispatch capture")

        with monkeypatch.context() as patch:
            patch.setattr(ForemanExecutor, "settle", lose_settlement)
            log = work.parent / "interrupted.jsonl"
            invoke(args, "--log", str(log), success=False)
        assert "lost settlement after dispatch capture" in log.read_text()
        assert calls(work) == expected_calls
        assert not (work / "summaries").exists()
        assert len(list((work / ".sqrlly").glob("wt-*"))) == 4
        invoke(args, "--resume")
    else:
        invoke(args)

    assert calls(work) == expected_calls
    assert summaries(work) == [[
        "b:False:same long stdout:same long stdout",
        "a:False:same long stdout:same long stdout",
    ]]
    assert not list(work.glob("artifact-*"))
    assert not list((work / ".sqrlly").glob("wt-*"))


@pytest.mark.parametrize("keep_receipt", [True, False], ids=["verified", "missing"])
def test_nested_git_guards_edited_unpromoted_output(work, keep_receipt):
    args, ready = nested_git_workflow(work)
    child = work / "child.yaml"
    config = yaml.safe_load(child.read_text())
    visited = config["nodes"][0]
    visited["promote"] = False
    visited["run"][2] = visited["run"][2].replace(
        'printf %s "$2" > "artifact-$1";',
        'if [ "$1" != b ]; then printf %s "$2" > "artifact-$1"; fi;',
    )
    child.write_text(yaml.safe_dump(config))

    log = work.parent / "initial.jsonl"
    invoke(args, "--log", str(log), success=False)
    assert "Output contract violated: missing files: artifact-*" in log.read_text()
    expected_calls = {"b:1": 3, "a:2": 1}
    assert calls(work) == expected_calls
    assert not (work / "summaries").exists()
    tree, = (work / ".sqrlly").glob("wt-*")
    assert not list(tree.glob("artifact-*"))

    (tree / "artifact-b").write_text("1")
    ready.touch()
    receipt, = (work / ".git" / "sqrlly-locks").glob("*.receipts/*.json")
    evidence = receipt.read_bytes()
    if not keep_receipt:
        receipt.unlink()

    log = work.parent / "guarded.jsonl"
    invoke(args, "--resume", "--log", str(log), success=False)
    assert "settled receipt is unavailable or mismatched" in log.read_text()
    assert calls(work) == expected_calls
    assert not (work / "summaries").exists()
    assert (tree / "artifact-b").read_text() == "1"
    assert not list(work.glob("artifact-*"))
    assert list((work / ".sqrlly").glob("wt-*")) == [tree]
    if keep_receipt:
        assert receipt.read_bytes() == evidence
    else:
        assert not receipt.exists()


@pytest.mark.parametrize("recovery", [
    "normal", "changed-dispatch", "changed-tree", "ignored-file", "ignored-file-subdir",
    "ignored-file-from-setup", "interrupted-retry",
])
def test_nested_git_resumes_after_output_validation_exhaustion(work, monkeypatch, recovery):
    repo = work
    if recovery == "ignored-file-subdir":
        work = repo / "project"
        work.mkdir()
    prefix = work.relative_to(repo)
    args, ready = nested_git_workflow(work, repo=repo)
    from_setup = recovery == "ignored-file-from-setup"
    ignored = recovery.startswith("ignored-file")
    if ignored:
        (repo / ".gitignore").write_text(f"/{prefix / 'artifact-b'}\n")
    child = work / "child.yaml"
    config = yaml.safe_load(child.read_text())
    if from_setup:
        config["settings"] = {"worktree_setup": [shlex.join([
            "sh", "-ec", 'if [ -e "$1" ]; then printf "setup output" > artifact-b; fi',
            "setup", str(ready),
        ])]}
    command = config["nodes"][0]["run"]
    command[2] = command[2].replace(
        'printf %s "$2" > "artifact-$1";',
        'if [ "$1" != b ] || [ -e "$4" ]; then printf %s "$2" > "artifact-$1"; fi;',
    )
    command.append(str(ready))
    child.write_text(yaml.safe_dump(config))

    log = work.parent / "initial.jsonl"
    invoke(args, "--log", str(log), success=False)
    assert "Output contract violated: missing files: artifact-*" in log.read_text()
    assert calls(work) == {"b:1": 3, "a:2": 1}
    assert (work / "artifact-a").read_text() == "2"
    assert not (work / "artifact-b").exists()
    assert not (work / "summaries").exists()
    tree, = (work / ".sqrlly").glob("wt-*")
    assert tree.is_dir()

    receipt, = [path for path in (repo / ".git" / "sqrlly-locks").glob("*.receipts/*.json")
                if b'"rejected_dispatch"' in path.read_bytes()]
    evidence = receipt.read_bytes()
    ready.touch()
    if recovery == "changed-dispatch":
        dispatch, = (work / ".git" / "worktrees").glob("*/sqrlly-dispatch.json")
        raw = json.loads(dispatch.read_text())
        raw["output"] = "{}"
        dispatch.write_text(json.dumps(raw))
    retained = tree / prefix / "artifact-b"
    edited = recovery == "changed-tree" or (ignored and not from_setup)
    if from_setup:
        assert not retained.exists()
        subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.org",
                        "commit", "--allow-empty", "-qm", "advance setup fingerprint"], cwd=repo, check=True)
    if edited:
        assert not retained.exists()
        retained.write_text("manual edit")
        if ignored:
            subprocess.run(["git", "check-ignore", "-q", str(prefix / "artifact-b")], cwd=tree, check=True)
    if recovery == "interrupted-retry":
        record = ForemanExecutor.record_result_for_settlement

        def interrupt(executor, node, identity, result, executions):
            if node.id == "visited" and result.success and result.output == "null":
                assert executions == 2
                raise OSError("lost validated result before recording settlement evidence")
            return record(executor, node, identity, result, executions)

        with monkeypatch.context() as patch:
            patch.setattr(ForemanExecutor, "record_result_for_settlement", interrupt)
            log = work.parent / "interrupted.jsonl"
            invoke(args, "--resume", "--log", str(log), success=False)
        assert "lost validated result before recording settlement evidence" in log.read_text()
        assert (tree / "artifact-b").read_text() == "1"
    if recovery != "normal":
        log = work.parent / "guarded.jsonl"
        invoke(args, "--resume", "--log", str(log), success=False)
        assert "settled receipt is unavailable or mismatched" in log.read_text()
        assert calls(work) == {"b:1": 4 if recovery == "interrupted-retry" else 3, "a:2": 1}
        assert (work / "artifact-a").read_text() == "2"
        assert not (work / "artifact-b").exists()
        assert not (work / "summaries").exists()
        assert list((work / ".sqrlly").glob("wt-*")) == [tree]
        if edited:
            assert retained.read_text() == "manual edit"
            assert receipt.read_bytes() == evidence
        if from_setup:
            assert retained.read_text() == "setup output"
            assert receipt.read_bytes() == evidence
        return

    invoke(args, "--resume")
    assert calls(work) == {"b:1": 4, "a:2": 1}
    assert (work / "artifact-b").read_text() == "1"
    assert summaries(work) == [['b:1:null', 'a:2:{"value":"2"}']]
    assert not list((work / ".sqrlly").glob("wt-*"))


def test_nested_git_yaml_date_executes_without_reusable_receipt(work):
    args, ready = nested_git_workflow(work)
    path = Path(args[1])
    config = yaml.safe_load(path.read_text())
    bindings = config["nodes"][0]["workflow"]["with"]
    bindings["value"] = yaml.safe_load("2026-11-22")
    path.write_text(yaml.safe_dump(config))
    assert "value: 2026-11-22" in path.read_text()

    invoke(args, success=False)
    expected_calls = {"b:2026-11-22": 1, "a:2026-11-22": 1}
    assert calls(work) == expected_calls
    assert (work / "artifact-b").read_text() == "2026-11-22"
    assert (work / "artifact-a").read_text() == "2026-11-22"
    assert not list((work / ".git" / "sqrlly-locks").glob("*.receipts/*.json"))
    assert not list((work / ".sqrlly").glob("wt-*"))

    ready.touch()
    log = work.parent / "resume.jsonl"
    invoke(args, "--resume", "--log", str(log), success=False)
    assert "checkpoint completion is missing" in log.read_text()
    assert calls(work) == expected_calls
    assert not (work / "summaries").exists()


def test_nested_git_nonfinite_output_promotes_without_reusable_receipt(work):
    args, ready = nested_git_workflow(work)
    (work / "value.json").write_text('{"type": ["number", "object"]}')
    child = work / "child.yaml"
    config = yaml.safe_load(child.read_text())
    visited, finish = config["nodes"]
    visited["run"][2] = visited["run"][2].replace("printf null", "printf 1e999")
    finish["run"][2] = finish["run"][2].replace('test "$4" = None', 'test "$4" = inf')
    finish["run"][7] = "{{visited_structured if key == 'b' else visited_structured.value}}"
    child.write_text(yaml.safe_dump(config))

    invoke(args, success=False)
    expected_calls = {"b:1": 1, "a:2": 1}
    assert calls(work) == expected_calls
    assert (work / "artifact-b").read_text() == "1"
    assert (work / "artifact-a").read_text() == "2"
    assert not list((work / ".sqrlly").glob("wt-*"))
    receipt, = (work / ".git" / "sqrlly-locks").glob("*.receipts/*.json")
    assert json.loads(receipt.read_bytes().split(b"\n", 1)[1])["result"]["structured_output"] == {"value": "2"}
    assert not (work / "summaries").exists()

    ready.touch()
    log = work.parent / "resume.jsonl"
    invoke(args, "--resume", "--log", str(log), success=False)
    assert "settled receipt is unavailable or mismatched" in log.read_text()
    assert calls(work) == expected_calls
    assert not (work / "summaries").exists()


@pytest.mark.parametrize("damage", [
    "missing", "legacy", "partial", "wrong-output", "wrong-scope", "wrong-structured-output", "routed",
])
def test_nested_git_recovery_guards_unverified_result(work, damage):
    args, ready = nested_git_workflow(work, routed=damage == "routed")
    invoke(args, success=False)
    assert (work / "artifact-b").read_text() == "1"
    receipts = list((work / ".git" / "sqrlly-locks").glob("*.receipts/*.json"))
    if damage == "routed":
        assert receipts == []
    else:
        receipt, = [path for path in receipts if b'"output": "null"' in path.read_bytes()]
        if damage == "missing":
            receipt.unlink()
        elif damage == "legacy":
            receipt.write_text('{"success": true, "output": "null"}')
        elif damage == "partial":
            receipt.write_bytes(receipt.read_bytes()[:18])
        else:
            record = json.loads(receipt.read_bytes().split(b"\n", 1)[1])
            if damage == "wrong-output":
                record["after"] = record["before"]
            elif damage == "wrong-structured-output":
                record["result"]["structured_output"] = {"value": "different from stdout"}
            else:
                record["scope"] += "-other"
            payload = json.dumps(record).encode()
            receipt.write_bytes(hashlib.sha256(payload).hexdigest().encode() + b"\n" + payload)
    ready.touch()
    log = work.parent / "recovered.jsonl"
    invoke(args, "--resume", "--log", str(log), success=False)
    failed = [event for line in log.read_text().splitlines() if
              (event := json.loads(line)).get("event") == "node_failed"]
    assert any(event["node"] == "rows::b::visited" and "checkpoint completion is missing" in event["error"]
               for event in failed)
    expected_calls = 2 if damage == "routed" else 1
    assert calls(work) == {"b:1": expected_calls, "a:2": expected_calls}
    if damage == "routed":
        assert (work.parent / "attempt-b").read_text() == "2\n"
        refs = subprocess.check_output(
            ["git", "-C", str(work), "for-each-ref", "--format=%(refname)", "refs/sqrlly"],
            text=True,
        ).splitlines()
        assert sum(ref.endswith("/out") for ref in refs) == 2
    assert not (work / "summaries").exists()


@pytest.mark.parametrize("remove_tree", [False, True])
def test_ordinary_nested_resume_after_promotion(work, monkeypatch, remove_tree):
    args, ready = nested_git_workflow(work)
    ready.touch()
    path = Path(args[1])
    config = yaml.safe_load(path.read_text())
    reference = config["nodes"][0]
    del reference["for_each"]
    reference["workflow"]["with"] = {"key": "b", "value": "1"}
    config["nodes"] = [reference]
    path.write_text(yaml.safe_dump(config))
    child_path = work / "child.yaml"
    child = yaml.safe_load(child_path.read_text())
    child["nodes"][1]["run"][2] += '; printf "%s:%s:%s" "$1" "$2" "$3" > seen'
    child_path.write_text(yaml.safe_dump(child))
    promote = SnapshotRun.promote
    settle = ForemanExecutor.settle

    def interrupt_promotion(snapshots, specs, *args, **kwargs):
        promote(snapshots, specs, *args, **kwargs)
        (key, _), = specs
        output = snapshots.ref(key, "out")
        revisions = subprocess.check_output(
            ["git", "rev-parse", output, output + "-settled"], cwd=work, text=True,
        ).splitlines()
        assert len(revisions) == 2 and revisions[0] == revisions[1]
        raise RuntimeError("lost acknowledgment after promotion")

    def interrupt_settlement(executor, node, settings, ids):
        settle(executor, node, settings, ids)
        raise RuntimeError("lost acknowledgment after cleanup")

    with monkeypatch.context() as patch:
        if remove_tree:
            patch.setattr(ForemanExecutor, "settle", interrupt_settlement)
        else:
            patch.setattr(SnapshotRun, "promote", interrupt_promotion)
        invoke(args, success=False)
    assert calls(work) == {"b:1": 1}
    assert (work / "artifact-b").read_text() == "1"
    trees = list((work / ".sqrlly").glob("wt-*"))
    assert len(trees) == int(not remove_tree)
    assert not (work / "seen").exists()
    assert not list((work / ".git" / "sqrlly-locks").glob("*.receipts/*.json"))

    log = work.parent / "ordinary-resume.jsonl"
    invoke(args, "--resume", "--log", str(log), success=not remove_tree)
    assert calls(work) == {"b:1": 1}
    assert (work / "artifact-b").read_text() == "1"
    assert not list((work / ".sqrlly").glob("wt-*"))
    events = [json.loads(line) for line in log.read_text().splitlines()]
    if remove_tree:
        assert any(event["event"] == "node_failed" and event["node"] == "rows::visited"
                   and "checkpoint completion is missing" in event["error"] for event in events)
        assert not (work / "seen").exists()
    else:
        assert (work / "seen").read_text() == "b:1:null"


@pytest.mark.parametrize("damage", [
    "none", "changed-input", "changed-tree", "missing", "legacy", "cleaned", "deleted-ignored-output",
])
@pytest.mark.parametrize("boundary", ["wrapper-outputs", "downstream-eval"])
@pytest.mark.parametrize("output_retry", [False, True], ids=["first-output", "retried-output"])
def test_nested_retained_result_after_promotion(work, monkeypatch, damage, boundary, output_retry):
    args, ready = nested_git_workflow(work)
    ready.touch()
    eval_calls = work.parent / "eval-calls"
    child_path = work / "child.yaml"
    child = yaml.safe_load(child_path.read_text())
    if damage == "deleted-ignored-output":
        (work / ".gitignore").write_text("proof-*\n")
        child["nodes"][0]["outputs"]["files"].append("proof-*")
        child["nodes"][0]["run"][2] = (
            'printf %s "$2" > "proof-$1"; ' + child["nodes"][0]["run"][2]
        )
    if output_retry:
        command = child["nodes"][0]["run"]
        command[2] = command[2].replace(
            'if [ "$1" = b ];', 'if [ "$1" = b ] && [ -z "$4" ]; then printf invalid; exit 0; fi; if [ "$1" = b ];',
        )
        command.append("{{ _output_retry_reason | default('') }}")
    if boundary == "wrapper-outputs":
        path = Path(args[1])
        config = yaml.safe_load(path.read_text())
        config["nodes"][0]["outputs"] = {"files": ["artifact-*"]}
        path.write_text(yaml.safe_dump(config))
    else:
        child["nodes"][1]["eval"] = {"run": [
            "sh", "-ec", 'printf "%s\n" "$1" >> "$2"', "eval", "{{key}}", str(eval_calls),
        ]}
    child_path.write_text(yaml.safe_dump(child))
    promote = SnapshotRun.promote
    settle = ForemanExecutor.settle

    def interrupt_promotion(snapshots, specs, *args, **kwargs):
        plan = promote(snapshots, specs, *args, **kwargs)
        (key, _), = specs
        if json.loads(key)[0] == "rows::b":
            output = snapshots.ref(key, "out")
            revisions = subprocess.check_output(
                ["git", "rev-parse", output, output + "-settled"], cwd=work, text=True,
            ).splitlines()
            assert len(revisions) == 2 and revisions[0] == revisions[1]
            raise RuntimeError("lost acknowledgment after promotion")
        return plan

    def interrupt_cleanup(executor, node, settings, ids):
        retained = settle(executor, node, settings, ids)
        if node.id == "visited" and json.loads(executor._key(node.id))[0] == "rows::b":
            raise RuntimeError("lost acknowledgment after cleanup")
        return retained

    with monkeypatch.context() as patch:
        if damage == "cleaned":
            patch.setattr(ForemanExecutor, "settle", interrupt_cleanup)
        else:
            patch.setattr(SnapshotRun, "promote", interrupt_promotion)
        invoke(args, "--log", str(work.parent / "initial-wrapper.jsonl"), success=False)
    expected_calls = {"b:1": 2 if output_retry else 1, "a:2": 1}
    assert calls(work) == expected_calls
    assert (work / "artifact-b").read_text() == "1"
    assert (work / "artifact-a").read_text() == "2"
    trees = list((work / ".sqrlly").glob("wt-*"))
    assert len(trees) == int(damage != "cleaned")
    if trees:
        tree, = trees
        retained_file = tree / "artifact-b"
        assert retained_file.read_text() == "1"
        admin = Path(subprocess.check_output(
            ["git", "rev-parse", "--absolute-git-dir"], cwd=tree, text=True,
        ).strip())
        assert json.loads((admin / "sqrlly-dispatch.json").read_bytes())["success"] is True
        sidecar = admin / "sqrlly-retained-dispatch.json"
        # The archived baseline control predates the retained dispatch receipt.
        baseline_control = (
            boundary == "downstream-eval" and damage == "none" and not output_retry
            and not hasattr(ForemanExecutor, "_has_retained_dispatch")
        )
        if not baseline_control:
            result = json.loads(sidecar.read_bytes().split(b"\n", 1)[1])
            assert result["executions"] == expected_calls["b:1"]
            assert result["result"]["output"] == "null"
            assert result["result"]["structured_output"] is None
    assert not (work / "summaries").exists()
    assert not list((work / ".git" / "sqrlly-locks").glob("*.receipts/*.json"))
    if boundary == "downstream-eval":
        assert eval_calls.read_text().splitlines() == ["a"]

    if damage == "changed-input":
        write_items(work, row("b", 3), row("a", 2))
    elif damage == "changed-tree":
        retained_file.write_text("manual edit")
    elif damage == "deleted-ignored-output":
        proof, = tree.glob("proof-*")
        assert proof.name == "proof-b"
        assert proof.read_text() == "1"
        assert subprocess.check_output(
            ["git", "check-ignore", proof.name], cwd=tree, text=True,
        ).strip() == proof.name
        proof.unlink()
    elif damage == "missing":
        sidecar.unlink()
    elif damage == "legacy":
        legacy = {key: result[key] for key in ("before", "after", "identity", "dispatch")}
        legacy["scope"] = (admin / "sqrlly-owner").read_text()
        payload = json.dumps(legacy).encode()
        sidecar.write_bytes(hashlib.sha256(payload).hexdigest().encode() + b"\n" + payload)

    log = work.parent / "wrapper-resume.jsonl"
    invoke(args, "--resume", "--log", str(log), success=damage == "none")
    assert calls(work) == expected_calls
    assert (work / "artifact-b").read_text() == "1"
    assert (work / "artifact-a").read_text() == "2"
    if boundary == "downstream-eval":
        assert eval_calls.read_text().splitlines() == (["a", "b"] if damage == "none" else ["a"])
    if damage == "none":
        assert summaries(work) == [['b:1:null', 'a:2:{"value":"2"}']]
        assert not list((work / ".sqrlly").glob("wt-*"))
    else:
        assert "settled receipt is unavailable or mismatched" in log.read_text()
        assert not (work / "summaries").exists()
        if damage == "cleaned":
            assert not list((work / ".sqrlly").glob("wt-*"))
        else:
            assert retained_file.read_text() == ("manual edit" if damage == "changed-tree" else "1")
        if damage == "deleted-ignored-output":
            assert tree.is_dir()
            assert not list(tree.glob("proof-*"))
            assert sidecar.exists()


@pytest.mark.parametrize("boundary", ["before-promotion", "legacy-dispatch", "after-promotion"])
def test_nested_git_lost_settlement_acknowledgment(work, monkeypatch, boundary):
    after_promotion = boundary == "after-promotion"
    args, ready = nested_git_workflow(work)
    ready.touch()
    settle = ForemanExecutor.settle

    def interrupt(executor, node, settings, ids):
        directory = executor.get_worktree(node.id)
        if node.id == "visited" and directory and (Path(directory) / "artifact-b").exists():
            if after_promotion:
                settle(executor, node, settings, ids)
            raise RuntimeError("lost settlement acknowledgment")
        return settle(executor, node, settings, ids)

    with monkeypatch.context() as patch:
        patch.setattr(ForemanExecutor, "settle", interrupt)
        invoke(args, success=False)
    assert calls(work) == {"b:1": 1, "a:2": 1}
    assert (work / "artifact-b").exists() == after_promotion
    assert bool(list((work / ".sqrlly").glob("wt-*"))) != after_promotion
    assert not (work / "summaries").exists()

    if boundary == "legacy-dispatch":
        receipt, = [path for path in (work / ".git" / "sqrlly-locks").glob("*.receipts/*.json")
                    if b'"output": "null"' in path.read_bytes()]
        receipt.unlink()

    log = work.parent / "recovered.jsonl"
    invoke(args, "--resume", "--log", str(log), success=after_promotion)
    assert calls(work) == {"b:1": 1, "a:2": 1}
    if after_promotion:
        assert summaries(work) == [['b:1:null', 'a:2:{"value":"2"}']]
        assert (work / "artifact-b").read_text() == "1"
    else:
        assert "settled receipt is unavailable or mismatched" in log.read_text()
        assert not (work / "artifact-b").exists()
        assert not (work / "summaries").exists()


@pytest.mark.parametrize(
    "evidence", ["matching", "changed-input", "changed-tree", "ignored-file", "legacy", "wrong-output-commit"],
)
@pytest.mark.parametrize("downstream_eval", [False, True], ids=["plain", "downstream-eval"])
@pytest.mark.parametrize("output_retry", [False, True], ids=["first-output", "retried-output"])
def test_nested_git_resume_retained_nonpromoting_dispatch(
    work, monkeypatch, evidence, downstream_eval, output_retry,
):
    args, ready = nested_git_workflow(work)
    if evidence == "ignored-file":
        (work / ".gitignore").write_text("/artifact-manual\n")
    ready.touch()
    child_path = work / "child.yaml"
    child = yaml.safe_load(child_path.read_text())
    child["nodes"][0]["promote"] = False
    if output_retry:
        command = child["nodes"][0]["run"]
        command[2] = command[2].replace(
            'printf %s "$2" > "artifact-$1";',
            'if [ "$1" = b ] && [ -z "$4" ]; then printf null; exit 0; fi; '
            'printf %s "$2" > "artifact-$1";',
        )
        command.append("{{ _output_retry_reason | default('') }}")
    expected_calls = {"b:1": 2 if output_retry else 1, "a:2": 1}
    eval_calls = work.parent / "eval-calls"
    if downstream_eval:
        child["nodes"][1]["eval"] = {"run": [
            "sh", "-ec", 'printf "%s\n" "$1" >> "$2"', "eval", "{{key}}", str(eval_calls),
        ]}
    child_path.write_text(yaml.safe_dump(child))
    interrupted = []
    write_receipt = foreman._write_receipt

    def interrupt(path, payload):
        write_receipt(path, payload)
        record = json.loads(payload)
        if (path.name == "sqrlly-retained-dispatch.json" and record["scope"].endswith("/in")
                and calls(work).get("b:1") == expected_calls["b:1"]
                and json.loads(path.with_name("sqrlly-dispatch.json").read_bytes())["output"] == "null"):
            interrupted.append(record["scope"].removesuffix("/in") + "/out")
            raise OSError("lost dispatch acknowledgment after evidence publication")

    log = work.parent / "interrupted.jsonl"
    with monkeypatch.context() as patch:
        patch.setattr(foreman, "_write_receipt", interrupt)
        invoke(args, "--log", str(log), success=False)
    output_ref, = interrupted
    assert "lost dispatch acknowledgment after evidence publication" in log.read_text()
    assert subprocess.check_output(
        ["git", "show", f"{output_ref}:artifact-b"], cwd=work, text=True,
    ) == "1"
    tree, = (work / ".sqrlly").glob("wt-*")
    assert (tree / "artifact-b").read_text() == "1"
    dispatch, = (work / ".git" / "worktrees").glob("*/sqrlly-dispatch.json")
    assert json.loads(dispatch.read_text())["success"] is True
    assert calls(work) == expected_calls
    assert not (work / "artifact-b").exists()
    assert not (work / "artifact-a").exists()
    assert not (work / "summaries").exists()

    if downstream_eval:
        assert eval_calls.read_text().splitlines() == ["a"]

    if evidence == "changed-input":
        path = Path(args[1])
        config = yaml.safe_load(path.read_text())
        config["nodes"][0]["workflow"]["with"]["value"] = "9"
        path.write_text(yaml.safe_dump(config))
    elif evidence == "changed-tree":
        (tree / "artifact-b").write_text("different")
    elif evidence == "ignored-file":
        (tree / "artifact-manual").write_text("manual edit")
        subprocess.run(["git", "check-ignore", "-q", "artifact-manual"], cwd=tree, check=True)
    elif evidence in {"legacy", "wrong-output-commit"}:
        sidecar = dispatch.with_name("sqrlly-retained-dispatch.json")
        record = json.loads(sidecar.read_bytes().split(b"\n", 1)[1])
        assert record["after"] == subprocess.check_output(
            ["git", "rev-parse", output_ref], cwd=work, text=True,
        ).strip()
        if evidence == "legacy":
            del record["after"]
        else:
            record["after"] = record["before"]
        payload = json.dumps(record).encode()
        sidecar.write_bytes(hashlib.sha256(payload).hexdigest().encode() + b"\n" + payload)

    resumed = evidence == "matching"
    log = work.parent / "recovered.jsonl"
    invoke(args, "--resume", "--log", str(log), success=resumed)
    assert calls(work) == expected_calls
    assert not (work / "artifact-b").exists()
    assert not (work / "artifact-a").exists()
    if downstream_eval:
        assert eval_calls.read_text().splitlines() == (["a", "b"] if resumed else ["a"])
    if resumed:
        assert not tree.exists()
        assert summaries(work) == [['b:1:null', 'a:2:{"value":"2"}']]
        assert subprocess.check_output(
            ["git", "show", f"{output_ref}:artifact-b"], cwd=work, text=True,
        ) == "1"
    else:
        assert tree.is_dir()
        assert "settled receipt is unavailable or mismatched" in log.read_text()
        assert not (work / "summaries").exists()
        if evidence == "ignored-file":
            assert (tree / "artifact-manual").read_text() == "manual edit"


def test_nested_git_resume_guards_stale_output_ref(work, monkeypatch):
    args, ready = nested_git_workflow(work)
    ready.touch()
    child_path = work / "child.yaml"
    child = yaml.safe_load(child_path.read_text())
    node = child["nodes"][0]
    node["promote"] = False
    command = node["run"]
    command[2] = command[2].replace(
        'printf %s "$2" > "artifact-$1";',
        'if [ "$1" = b ] && [ -z "$4" ]; then '
        'printf A > artifact-b; printf \'"invalid"\'; exit 0; fi; '
        'printf %s "$2" > "artifact-$1";',
    )
    command.append("{{ _output_retry_reason | default('') }}")
    child_path.write_text(yaml.safe_dump(child))

    capture_out = SnapshotRun.capture_out
    interrupted = []

    def interrupt(snapshots, node_id, worktree, **kwargs):
        if node_id == '["rows::b","visited"]' and calls(work)["b:1"] == 2:
            output_ref = snapshots.ref(node_id, "out")
            interrupted.append(output_ref)
            assert (Path(worktree) / "artifact-b").read_text() == "1"
            raise OSError("interrupted before replacement output capture")
        return capture_out(snapshots, node_id, worktree, **kwargs)

    log = work.parent / "interrupted.jsonl"
    with monkeypatch.context() as patch:
        patch.setattr(SnapshotRun, "capture_out", interrupt)
        invoke(args, "--log", str(log), success=False)
    output_ref, = interrupted
    assert "interrupted before replacement output capture" in log.read_text()
    tree, = (work / ".sqrlly").glob("wt-*")
    artifact = tree / "artifact-b"
    assert artifact.read_text() == "1"
    assert subprocess.check_output(
        ["git", "show", f"{output_ref}:artifact-b"], cwd=work, text=True,
    ) == "A"
    dispatch, = (work / ".git" / "worktrees").glob("*/sqrlly-dispatch.json")
    assert json.loads(dispatch.read_bytes())["output"] == "null"
    artifact.write_text("A")

    log = work.parent / "resumed.jsonl"
    invoke(args, "--resume", "--log", str(log), success=False)
    assert "settled receipt is unavailable or mismatched" in log.read_text()
    assert calls(work) == {"b:1": 2, "a:2": 1}
    assert artifact.read_text() == "A"
    assert not (work / "artifact-b").exists()
    assert not (work / "artifact-a").exists()
    assert not (work / "summaries").exists()


def test_nested_git_resume_after_input_snapshot_before_worktree(work, monkeypatch):
    args, ready = nested_git_workflow(work)
    ready.touch()
    capture_in = SnapshotRun.capture_in
    interrupted = []

    def interrupt(snapshots, node_id, **kwargs):
        commit = capture_in(snapshots, node_id, **kwargs)
        if node_id == '["rows::b","visited"]':
            assert snapshots.revision(node_id, "in") == commit
            assert not snapshots.revision(node_id, "out")
            interrupted.append(snapshots.ref(node_id, "in"))
            raise OSError("lost checkout before worktree creation")
        return commit

    log = work.parent / "interrupted.jsonl"
    with monkeypatch.context() as patch:
        patch.setattr(SnapshotRun, "capture_in", interrupt)
        invoke(args, "--log", str(log), success=False)
    ref, = interrupted
    assert "lost checkout before worktree creation" in log.read_text()
    subprocess.run(["git", "show-ref", "--verify", ref], cwd=work, check=True, capture_output=True)
    output_ref = ref.removesuffix("/in") + "/out"
    refs = subprocess.check_output(["git", "for-each-ref", "--format=%(refname)", output_ref],
                                   cwd=work, text=True)
    assert output_ref not in refs.splitlines()
    assert calls(work) == {"a:2": 1}
    assert (work / "artifact-a").read_text() == "2"
    assert not (work / "artifact-b").exists()
    assert not list((work / ".sqrlly").glob("wt-*"))
    assert not (work / "summaries").exists()

    invoke(args, "--resume")
    assert calls(work) == {"b:1": 1, "a:2": 1}
    assert (work / "artifact-b").read_text() == "1"
    assert summaries(work) == [['b:1:null', 'a:2:{"value":"2"}']]
    assert not list((work / ".sqrlly").glob("wt-*"))


@pytest.mark.parametrize("disable_isolation", [False, True])
def test_nested_git_interrupted_dispatch_keeps_guard(work, monkeypatch, disable_isolation):
    args, ready = nested_git_workflow(work)
    execute = DispatchExecutor.execute

    async def interrupt(executor, node, context, *args, **kwargs):
        result = await execute(executor, node, context, *args, **kwargs)
        if node.id == "visited" and context["key"] == "b":
            assert result.success
            raise OSError("lost dispatch result")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(DispatchExecutor, "execute", interrupt)
        invoke(args, success=False)
    ready.touch()
    assert calls(work) == {"b:1": 1, "a:2": 1}
    assert not (work / "artifact-b").exists()
    record, = (work / ".git" / "worktrees").glob("*/sqrlly-dispatch.json")
    assert record.read_text() == "null"
    if disable_isolation:
        child = work / "child.yaml"
        config = yaml.safe_load(child.read_text())
        config["nodes"][0]["worktree"] = "off"
        child.write_text(yaml.safe_dump(config))
    log = work.parent / "recovered.jsonl"
    invoke(args, "--resume", "--log", str(log), success=False)
    assert calls(work) == {"b:1": 1, "a:2": 1}
    assert "settled receipt is unavailable or mismatched" in log.read_text()
    assert record.read_text() == "null"
    assert not (work / "artifact-b").exists()
    assert not (work / "summaries").exists()


def test_nested_git_fresh_run_prunes_old_receipts(work):
    args, ready = nested_git_workflow(work)
    invoke(args, success=False)
    locks = work / ".git" / "sqrlly-locks"
    old, = locks.glob("*.receipts")
    assert len(list(old.glob("*.json"))) == 2
    ready.touch()
    invoke(args)
    assert not old.exists()
    current, = locks.glob("*.receipts")
    assert len(list(current.glob("*.json"))) == 2
    assert calls(work) == {"b:1": 2, "a:2": 2}
    assert summaries(work) == [['b:1:null', 'a:2:{"value":"2"}']]



def test_nested_git_changed_item_requires_explicit_refresh(work):
    args, ready = nested_git_workflow(work)
    invoke(args, success=False)
    assert (work / "artifact-b").read_text() == "1"
    ready.touch()
    write_items(work, row("b", 3), row("a", 2))
    invoke(args, "--resume", success=False)
    assert calls(work) == {"b:1": 1, "a:2": 1}
    assert (work / "artifact-b").read_text() == "1"
    invoke(args, "--refresh-items")
    assert calls(work) == {"b:1": 1, "a:2": 1, "b:3": 1}
    assert (work / "artifact-b").read_text() == "3"
    assert summaries(work) == [['b:3:null', 'a:2:{"value":"2"}']]


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
