"""Exercise approval with the installed CLI and persisted checkpoints."""

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def _cli(workdir, command, *flags):
    executable = Path(sys.executable).with_name("sqrlly.exe" if sys.platform == "win32" else "sqrlly")
    return subprocess.run(
        [str(executable), command, str(workdir / "workflow.yaml"), *flags],
        cwd=workdir, capture_output=True, text=True, timeout=20,
    )


def _run(workdir, *flags):
    return _cli(workdir, "run", "--quiet", "--log", "run.jsonl", *flags)


def _request(result, node):
    assert result.returncode == 3, result.stdout + result.stderr
    assert f"waiting for approval: {node}\n" in result.stdout
    fields = dict(line.split(": ", 1) for line in result.stdout.splitlines() if ": " in line)
    return node, fields["run id"], fields["request id"]


def _events(workdir):
    return [json.loads(line) for line in (workdir / "run.jsonl").read_text().splitlines()]


def test_pending_approval_matches_node_run_request_and_definition(tmp_path):
    write_effect = [sys.executable, "-c",
                    "import sys; open('effects', 'a').write(sys.argv[1] + '\\n')"]
    nodes = [
        {"id": "draft", "run": [*write_effect, "draft"]},
        {"id": "review", "depends_on": ["draft"], "eval": {"human": "Review draft files"}},
        {"id": "release", "depends_on": ["review"], "eval": {"human": "Review release"}},
        {"id": "publish", "depends_on": ["release"], "run": [*write_effect, "publish"]},
    ]
    workflow = tmp_path / "workflow.yaml"
    authored = yaml.safe_dump({"nodes": nodes})
    workflow.write_text(authored)
    trace = _run(tmp_path, "--dry-run")
    assert trace.returncode == 0 and "4 nodes traced" in trace.stdout
    cold = _run(tmp_path, "--entry", "publish")
    assert cold.returncode == 2 and "--entry cannot be used" in cold.stderr
    effects = tmp_path / "effects"
    assert not effects.exists()

    first = _run(tmp_path)
    review = _request(first, "review")
    assert "Review draft files" in first.stdout
    assert _request(_run(tmp_path, "--resume"), "review") == review
    assert effects.read_text() == "draft\n"
    replay = _run(tmp_path, "--resume", "--rerun-all")
    assert replay.returncode == 1 and "Pending approval" in replay.stderr
    wrong_run = _run(tmp_path, "--resume", "--approval", "review", "stale", review[2], "approve")
    assert wrong_run.returncode == 2 and "UUID" in wrong_run.stderr
    invalid = _run(tmp_path, "--resume", "--approval", *review, "skip")
    assert invalid.returncode == 2 and "skip" in invalid.stderr

    nodes[-1]["run"][-1] = "changed"
    workflow.write_text(yaml.safe_dump({"nodes": nodes}))
    changed = _run(tmp_path, "--resume", "--approval", *review, "approve")
    assert changed.returncode == 1 and "Workflow definition changed" in changed.stderr
    workflow.write_text(authored)
    release = _request(_run(tmp_path, "--resume", "--approval", *review, "approve"), "release")
    assert release[1] == review[1]
    assert release[2] != review[2]
    stale = _run(tmp_path, "--resume", "--approval", "release", review[1], review[2], "approve")
    assert stale.returncode == 1 and "does not match" in stale.stderr
    assert effects.read_text() == "draft\n"

    done = _run(tmp_path, "--resume", "--approval", *release, "approve")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Completed: 4 nodes" in done.stdout
    assert effects.read_text() == "draft\npublish\n"
    events = _events(tmp_path)
    waiting = next(event for event in events if event.get("status") == "pending_approval")
    assert waiting["pending_approval"] == {
        "node": "review", "run_id": review[1], "request_id": review[2], "message": "Review draft files",
    }
    assert [event["result"]["decision"] for event in events if event["event"] == "gate_evaluated"] == [
        "approve", "approve",
    ]


def _write_git_approval(workdir):
    for args in [["init", "-q"], ["config", "user.name", "Approval Test"],
                 ["config", "user.email", "approval@example.com"], ["commit", "--allow-empty", "-qm", "seed"]]:
        subprocess.run(["git", *args], cwd=workdir, check=True, capture_output=True)
    nodes = [
        {"id": "draft", "worktree": "isolated", "eval": {"human": "Inspect draft.txt"},
         "outputs": {"files": ["draft.txt"]},
         "run": [sys.executable, "-c", "from pathlib import Path; Path('draft.txt').write_text('reviewed')"]},
        {"id": "publish", "depends_on": ["draft"], "run": [sys.executable, "-c",
         "from pathlib import Path; Path('published').write_text(Path('draft.txt').read_text())"]},
    ]
    config = workdir / "workflow.yaml"
    config.write_text(yaml.safe_dump({"nodes": nodes}))
    return config


@pytest.mark.parametrize(("decision", "edited"), [("approve", False), ("reject", False), ("approve", True)])
def test_approval_controls_isolated_worktree_promotion(tmp_path, decision, edited):
    _write_git_approval(tmp_path)
    paused = _run(tmp_path)
    request = _request(paused, "draft")
    tree = Path(next(line.removeprefix("worktree: ") for line in paused.stdout.splitlines()
                     if line.startswith("worktree: ")))
    assert (tree / "draft.txt").read_text() == "reviewed"
    assert not (tmp_path / "draft.txt").exists()
    if edited:
        (tree / "draft.txt").write_text("changed after review")
    resumed = _run(tmp_path, "--resume", "--approval", *request, decision)
    end = _events(tmp_path)[-1]
    if edited:
        assert resumed.returncode == 1 and "Approval worktree changed" in resumed.stderr
        assert (tree / "draft.txt").read_text() == "changed after review"
        assert not (tmp_path / "draft.txt").exists()
        assert not (tmp_path / "published").exists()
    elif decision == "approve":
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        assert (tmp_path / "published").read_text() == "reviewed"
        assert end["status"] == "succeeded"
    else:
        assert resumed.returncode == 1 and "Approval rejected" in resumed.stderr
        assert (tree / "draft.txt").read_text() == "reviewed"
        assert not (tmp_path / "draft.txt").exists()
        assert not (tmp_path / "published").exists()
        assert end["failed_kinds"] == {"draft": "gate_failure"}


def test_resume_cannot_skip_an_approval_added_after_checkpointing(tmp_path):
    review = {"id": "review", "run": [sys.executable, "-c", "print('draft')"]}
    publish = {"id": "publish", "depends_on": ["review"], "run": [
        sys.executable, "-c", "from pathlib import Path; Path('published').touch()",
    ]}
    config = tmp_path / "workflow.yaml"
    config.write_text(yaml.safe_dump({"nodes": [review, publish]}))
    assert _run(tmp_path).returncode == 0
    (tmp_path / "published").unlink()
    review["eval"] = {"human": "Review the draft"}
    config.write_text(yaml.safe_dump({"nodes": [review, publish]}))

    replay = _run(tmp_path, "--resume-from", "publish")
    assert replay.returncode == 1 and "Workflow definition changed" in replay.stderr
    assert not (tmp_path / "published").exists()


@pytest.mark.parametrize("standalone", [False, True])
def test_newer_git_run_keeps_the_pending_approval_snapshot(tmp_path, standalone):
    config = _write_git_approval(tmp_path)
    if standalone:
        nodes = yaml.safe_load(config.read_text())["nodes"]
        check = nodes[0].pop("eval")
        nodes[1]["depends_on"] = ["review"]
        nodes.insert(1, {"id": "review", "depends_on": ["draft"], "eval": check})
        config.write_text(yaml.safe_dump({"nodes": nodes}))
    authored = config.read_text()
    request = _request(_run(tmp_path), "review" if standalone else "draft")
    git_refs = ["git", "for-each-ref", "--format=%(refname)", "refs/sqrlly/"]
    retained = subprocess.check_output(git_refs, cwd=tmp_path, text=True)
    assert retained
    config.write_text(yaml.safe_dump({"nodes": [
        {"id": "different", "run": [sys.executable, "-c", "print('newer run')"]},
    ]}))
    newer = _run(tmp_path)
    assert newer.returncode == 0, newer.stdout + newer.stderr
    assert subprocess.check_output(git_refs, cwd=tmp_path, text=True) == retained
    config.write_text(authored)

    approved = _run(tmp_path, "--resume", "--approval", *request, "approve")
    assert approved.returncode == 0, approved.stdout + approved.stderr
    assert (tmp_path / "published").read_text() == "reviewed"


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="requires SIGKILL support")
def test_resume_finishes_approval_after_promotion_removes_the_worktree(tmp_path, monkeypatch):
    workdir = tmp_path / "work"
    workdir.mkdir()
    _write_git_approval(workdir)
    request = _request(_run(workdir), "draft")

    git_path = shlex.quote(shutil.which("git"))
    launcher = tmp_path / "git"
    launcher.write_text(f"""#!/bin/sh
{git_path} "$@"
status=$?
if [ "$status" -eq 0 ] && [ "$3" = worktree ] && [ "$4" = remove ]; then
    kill -KILL "$PPID"
fi
exit "$status"
""")
    launcher.chmod(0o700)
    with monkeypatch.context() as fault:
        fault.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
        interrupted = _run(workdir, "--resume", "--approval", *request, "approve")
    assert interrupted.returncode == -signal.SIGKILL
    assert (workdir / "draft.txt").read_text() == "reviewed"
    assert not (workdir / "published").exists()
    report = _cli(workdir, "status", "--json")
    assert report.returncode == 0, report.stderr
    pending = json.loads(report.stdout)["pending_approval"]
    assert pending["request_id"] == request[2]
    assert pending["decision"] == "approve"

    opposite = _run(workdir, "--resume", "--approval", *request, "reject")
    assert opposite.returncode == 1 and "approve decision is already saved" in opposite.stderr
    assert not (workdir / "published").exists()

    resumed = _run(workdir, "--resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert (workdir / "published").read_text() == "reviewed"


def test_status_and_approval_can_address_an_earlier_pending_run(tmp_path):
    nodes = [
        {"id": "review", "eval": {"human": "Review the batch"}},
        {"id": "publish", "depends_on": ["review"], "run": [
            sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
            "{{ vars.batch }}",
        ]},
    ]
    (tmp_path / "workflow.yaml").write_text(yaml.safe_dump({"nodes": nodes}))
    first = _request(_run(tmp_path, "--var", "batch=first"), "review")
    second = _request(_run(tmp_path, "--var", "batch=second"), "review")
    latest = json.loads(_cli(tmp_path, "status", "--json").stdout)
    assert latest["run_id"] == second[1]
    assert latest["pending_approval"]["request_id"] == second[2]
    result = _cli(tmp_path, "status", "--json", "--run-id", first[1])
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["pending_approval"]["request_id"] == first[2]

    result = _run(tmp_path, "--resume", "--approval", *first, "approve")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "first").exists()
    assert not (tmp_path / "second").exists()
    assert _request(_run(tmp_path, "--resume"), "review") == second


@pytest.mark.parametrize(("extra", "error"), [
    ({"id": "other"}, "single dependency chain"),
    ({"id": "other", "depends_on": ["review"], "route": [{"else": "fail"}]}, "without routes"),
    ({"id": "child", "workflow": "approval.yaml"}, "top-level workflow"),
])
def test_human_approval_rejects_unsupported_topologies(tmp_path, extra, error):
    gate = {"id": "review", "eval": {"human": "Check the draft"}}
    (tmp_path / "approval.yaml").write_text(yaml.safe_dump({"nodes": [gate]}))
    nodes = [extra] if "workflow" in extra else [gate, extra]
    (tmp_path / "workflow.yaml").write_text(yaml.safe_dump({"nodes": nodes}))
    result = _cli(tmp_path, "validate")
    assert result.returncode == 1 and error in result.stderr
