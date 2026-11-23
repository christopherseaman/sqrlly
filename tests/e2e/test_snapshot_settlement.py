"""Snapshot settlement through the installed CLI and real linked Git worktrees."""
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def git(base, *args):
    return subprocess.check_output(["git", "-C", base, *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    base = tmp_path / "repo"
    base.mkdir()
    git(base, "init", "-q")
    git(base, "config", "user.email", "test@example.com")
    git(base, "config", "user.name", "test")
    (base / "input").write_text("clean")
    git(base, "add", "input")
    git(base, "commit", "-qm", "seed")
    (base / "input").write_text("dirty")
    return base


def invoke(base, nodes, *args, **settings):
    config = base / "workflow.yaml"
    config.write_text(yaml.safe_dump({"name": "snapshots", "settings": settings, "nodes": nodes}))
    argv = [str(Path(sys.executable).with_name("sqrlly")), "run", str(config), "-w", str(base), *args]
    return subprocess.run(argv, cwd=base, text=True, capture_output=True)


def test_settled_commit_reaches_next_node(repo):
    nodes = [
        {"id": "prepare", "run": ["sh", "-c", "echo upstream > source"]},
        {"id": "edit", "depends_on": ["prepare"], "worktree": "isolated", "run": [
            "sh", "-ec", 'test "$(cat input)" = dirty; cat source > result; git add result; git commit -qm result']},
        {"id": "read", "depends_on": ["edit"], "run": ["sh", "-ec", "cat result > seen"]},
        {"id": "private", "depends_on": ["read"], "worktree": "isolated", "promote": False,
         "run": ["sh", "-c", "echo kept > hidden"]},
    ]
    result = invoke(repo, nodes)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "seen").read_text() == "upstream\n"
    assert git(repo, "diff", "--cached", "--name-only") == ""
    assert len(git(repo, "worktree", "list", "--porcelain").split("worktree ")) == 2
    ref = next(line.split()[-1] for line in result.stdout.splitlines() if line.startswith("Retained snapshot:"))
    assert git(repo, "show", f"{ref}:hidden") == "kept"
    assert not (repo / "hidden").exists()


def test_moved_base_retains_failed_tree(repo):
    nodes = [{"id": "edit", "worktree": "isolated", "run": [
        "sh", "-ec", 'echo ours > input; echo external > "$1/input"', "worker", str(repo)]}]
    result = invoke(repo, nodes)
    assert result.returncode == 1
    assert "conflict" in (result.stdout + result.stderr).lower()
    assert (repo / "input").read_text() == "external\n"
    tree, = (repo / ".sqrlly").glob("wt-*")
    assert (tree / "input").read_text() == "ours\n"


@pytest.mark.parametrize("mode", ["ordinary", "repeated", "changed-tree"])
def test_nested_resume_retries_conflicted_promotion_without_dispatch(repo, tmp_path, mode):
    counts = tmp_path / "counts"
    child = [
        {"id": "edit", "worktree": "isolated", "run": [
            "sh", "-ec", 'echo called >> "$1"; printf ours > input; '
            'printf external > "$2/input"; printf worker-output',
            "worker", str(counts), str(repo)]},
        {"id": "read", "depends_on": ["edit"], "run": [
            "sh", "-ec", 'printf "%s:%s" "$1" "$(cat input)" > seen',
            "reader", "{{edit}}"]},
    ]
    (repo / "child.yaml").write_text(yaml.safe_dump({"nodes": child}))
    nodes = [{"id": "nested", "workflow": {"file": "child.yaml"}}]

    scope = "nested"
    if mode != "ordinary":
        (repo / "items.json").write_text('[{"id": "only"}]')
        nodes[0]["for_each"] = {"file": "items.json"}
        scope = "nested::only"

    log = tmp_path / "conflict.jsonl"
    result = invoke(repo, nodes, "--log", str(log))
    assert result.returncode == 1, result.stdout + result.stderr
    events = [json.loads(line) for line in log.read_text().splitlines()]
    failure, = [event for event in events
                if event["event"] == "node_failed" and event["node"] == f"{scope}::edit"]
    assert "Promote conflict (1 path(s)): input <-" in failure["error"]
    assert (repo / "input").read_text() == "external"
    tree, = (repo / ".sqrlly").glob("wt-*")
    assert (tree / "input").read_text() == "ours"
    assert counts.read_text() == "called\n"
    assert not (repo / "seen").exists()

    (repo / "input").write_text("dirty")
    if mode == "changed-tree":
        (tree / "input").write_text("unverified")
    resumed_log = tmp_path / "resumed.jsonl"
    result = invoke(repo, nodes, "--resume", "--log", str(resumed_log))
    if mode == "changed-tree":
        assert result.returncode == 1
        assert "settled receipt is unavailable or mismatched" in resumed_log.read_text()
        assert (repo / "input").read_text() == "dirty"
        assert (tree / "input").read_text() == "unverified"
        assert counts.read_text() == "called\n"
        assert not (repo / "seen").exists()
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "input").read_text() == "ours"
    assert (repo / "seen").read_text() == "worker-output:ours"
    assert counts.read_text() == "called\n"
    assert not list((repo / ".sqrlly").glob("wt-*"))


def test_fanout_resume_promotes_in_manifest_order_once(repo, tmp_path):
    (repo / "items.json").write_text(json.dumps([{"id": "z"}, {"id": "a"}]))
    gate = tmp_path / "allow"
    counts = tmp_path / "counts"
    script = 'echo "$1" >> "$2"; test "$1" != a || test -f "$3"; echo "$1" > "file-$1"; echo "$1" > common'
    nodes = [
        {"id": "fan", "for_each": {"file": "items.json"}, "run": [
            "sh", "-ec", script, "worker", "{{item.id}}", str(counts), str(gate)]},
        {"id": "join", "depends_on": ["fan"], "run": ["sh", "-ec", "cat common file-z file-a > joined"]},
    ]
    def run(*args):
        return invoke(repo, nodes, *args, on_promote_conflict="overwrite")
    result = run()
    assert result.returncode == 1, result.stdout + result.stderr
    assert not (repo / "common").exists()
    assert not (repo / "joined").exists()
    gate.touch()
    result = run("--resume")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "joined").read_text() == "a\nz\na\n"
    assert sorted(counts.read_text().splitlines()) == ["a", "a", "z"]
    (repo / "file-z").write_text("later edit")
    result = run("--resume")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (repo / "file-z").read_text() == "later edit"
    assert sorted(counts.read_text().splitlines()) == ["a", "a", "z"]


def test_concurrent_workflow_runs_keep_both_snapshots(repo, tmp_path):
    import os
    import time

    ready, release = tmp_path / "ready", tmp_path / "release"
    worker = (
        'echo "$RUN" > artifact; '
        'if test "$RUN" = first; then touch "$1"; '
        'while test ! -e "$2"; do sleep 0.05; done; fi'
    )
    config = repo / "workflow.yaml"
    config.write_text(yaml.safe_dump({"nodes": [{
        "id": "write", "worktree": "isolated", "promote": False,
        "run": ["sh", "-ec", worker, "worker", str(ready), str(release)],
    }]}))
    cli = [str(Path(sys.executable).with_name("sqrlly")), "run", str(config), "-w", str(repo)]
    first = subprocess.Popen(cli, env=os.environ | {"RUN": "first"}, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists()
        busy = subprocess.run([*cli, "--resume"], text=True, capture_output=True)
        assert busy.returncode != 0 and "already active" in busy.stderr
        second = subprocess.run(cli, env=os.environ | {"RUN": "second"}, capture_output=True, text=True)
        assert second.returncode == 0, second.stderr
    finally:
        release.touch()
        stdout, stderr = first.communicate(timeout=20)
    assert first.returncode == 0, stdout + stderr

    def snapshots():
        refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/sqrlly")
        return {git(repo, "show", f"{ref}:artifact") for ref in refs.splitlines() if ref.endswith("/out")}

    assert snapshots() == {"first", "second"}
    third = subprocess.run(cli, env=os.environ | {"RUN": "third"}, capture_output=True, text=True)
    assert third.returncode == 0, third.stderr
    assert snapshots() == {"third"}


def test_eval_runs_in_isolated_tree_before_promotion(repo, tmp_path):
    approval = tmp_path / "approved"
    judge = repo / "judge.py"
    judge.write_text(
        "from pathlib import Path\n"
        f"assert not (Path({str(repo)!r}) / 'candidate').exists()\n"
        "assert Path('candidate').read_text() == 'ready'\n"
        f"raise SystemExit(0 if Path({str(approval)!r}).exists() else 1)\n"
    )
    nodes = [{"id": "generate", "worktree": "isolated", "run": ["sh", "-c", "printf ready > candidate"],
              "eval": {"run": [sys.executable, "judge.py"]},
              "route": [{"when": "eval.exit_code != 0", "then": "fail"}]}]
    failed = invoke(repo, nodes)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert not (repo / "candidate").exists()
    tree, = (repo / ".sqrlly").glob("wt-*")
    assert (tree / "candidate").read_text() == "ready"
    approval.touch()
    success = invoke(repo, nodes, "--resume")
    assert success.returncode == 0, success.stdout + success.stderr
    assert (repo / "candidate").read_text() == "ready"
    assert not tree.exists()


def test_cleanup_preserves_ignored_nested_repository(repo):
    (repo / ".gitignore").write_text("scratch/\n")
    nested_path = repo / "manual-path"
    script = (
        'mkdir scratch; git worktree add -q --detach "$PWD/scratch/manual" HEAD; '
        'echo mine > scratch/manual/user-work; printf "%s" "$PWD/scratch/manual" > "$1"'
    )
    result = invoke(repo, [{"id": "edit", "worktree": "isolated",
                            "run": ["sh", "-ec", script, "worker", str(nested_path)]}])
    inner = Path(nested_path.read_text())
    assert result.returncode == 1 and str(inner) in result.stderr
    assert (inner / "user-work").read_text() == "mine\n"
    assert f"worktree {inner}" in git(repo, "worktree", "list", "--porcelain")


def test_resume_after_checkout_hook_failure(repo):
    hook = repo / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\nexit 19\n")
    hook.chmod(0o755)
    nodes = [{"id": "write", "worktree": "isolated", "run": ["sh", "-ec", "echo done > result"]}]
    failed = invoke(repo, nodes)
    assert failed.returncode == 1 and not (repo / "result").exists()
    hook.unlink()
    resumed = invoke(repo, nodes, "--resume")
    assert resumed.returncode == 0, resumed.stderr
    assert (repo / "result").read_text() == "done\n"


def test_child_settings_and_retained_refs_in_workflow_fanout(repo):
    (repo / "deps").mkdir()
    (repo / "deps/input").write_text("nested share")
    child = {
        "settings": {"worktree_setup": ["sh -c 'echo setup > setup-output'"],
                     "worktree_share": ["deps"], "promote_exclude": ["setup-output"]},
        "nodes": [{"id": "edit", "worktree": "isolated", "promote": False,
                   "run": ["sh", "-ec", "test -L deps; cat deps/input setup-output > result"]}],
    }
    (repo / "child.yaml").write_text(yaml.safe_dump(child))
    (repo / "items.json").write_text('["x"]')
    result = invoke(repo, [{"id": "branches", "for_each": {"file": "items.json"}, "workflow": "child.yaml"}])
    assert result.returncode == 0, result.stdout + result.stderr
    ref = next(line.split()[-1] for line in result.stdout.splitlines() if line.startswith("Retained snapshot:"))
    assert git(repo, "show", f"{ref}:result") == "nested sharesetup"
    assert "deps" not in git(repo, "ls-tree", "--name-only", ref).splitlines()


def test_interruption_preserves_ambiguous_dispatch(repo, tmp_path):
    import time

    started, release = tmp_path / "started", tmp_path / "release"
    script = (
        'echo "$1" >> "$2"; if test "$1" = b; then '
        'while test ! -f "$3"; do sleep 0.05; done; fi; echo "$1" >> "$1"'
    )
    (repo / "items.json").write_text('["a", "b"]')
    nodes = [{"id": "branches", "for_each": {"file": "items.json"},
        "run": ["sh", "-ec", script, "worker", "{{item}}", str(started), str(release)]}]
    config = repo / "workflow.yaml"
    config.write_text(yaml.safe_dump({"nodes": nodes, "name": "snapshots", "settings": {}}))
    cli = [str(Path(sys.executable).with_name("sqrlly")), "run", str(config), "-w", str(repo)]
    proc = subprocess.Popen(cli, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            refs = git(repo, "for-each-ref", "--format=%(refname)", "refs/sqrlly")
            if started.exists() and "b" in started.read_text() and "/out" in refs:
                break
            time.sleep(.05)
        else:
            pytest.fail("Both branches must start and one must produce an out ref")
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        release.touch()
    resumed = invoke(repo, nodes, "--resume")
    assert resumed.returncode == 1 and "Interrupted dispatch" in resumed.stderr
    assert sorted(started.read_text().splitlines()) == ["a", "b"]
    assert not (repo / "a").exists()
    assert len(list((repo / ".sqrlly").glob("wt-*/a"))) == 1


def test_same_named_files_have_independent_refs_and_checkpoints(repo):
    refs = []
    for name in ("first", "second"):
        config = repo / f"{name}.yaml"
        config.write_text(yaml.safe_dump({"name": "same", "nodes": [{
            "id": "edit", "worktree": "isolated", "promote": False,
            "run": ["sh", "-c", f"echo {name} > result"],
        }]}))
        result = subprocess.run([str(Path(sys.executable).with_name("sqrlly")), "run", str(config), "-w", str(repo)],
                                text=True, capture_output=True)
        assert result.returncode == 0, result.stderr
        refs.append(next(line.split()[-1] for line in result.stdout.splitlines() if line.startswith("Retained snapshot:")))
    assert [git(repo, "show", f"{ref}:result") for ref in refs] == ["first", "second"]


def test_output_checks_finish_before_filtered_promotion(repo, tmp_path):
    allow = tmp_path / "allow"
    count = tmp_path / "attempts"
    script = ('echo attempt >> "$1"; mkdir -p out; echo kept > out/value.txt; '
              'echo hidden > private; if test -e "$2"; then echo ready > out/status; fi')
    nodes = [{"id": "build", "worktree": "isolated",
              "outputs": {"files": ["out/*.txt", "out/status"]},
              "run": ["sh", "-ec", script, "worker", str(count), str(allow)]}]
    failed = invoke(repo, nodes)
    assert failed.returncode == 1, failed.stdout + failed.stderr
    assert count.read_text().splitlines() == ["attempt"] * 3
    assert not (repo / "out").exists()
    tree, = (repo / ".sqrlly").glob("wt-*")
    assert (tree / "out/value.txt").read_text() == "kept\n"
    allow.touch()
    resumed = invoke(repo, nodes, "--resume")
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert (repo / "out/value.txt").read_text() == "kept\n"
    assert (repo / "out/status").read_text() == "ready\n"
    assert not (repo / "private").exists()
    assert not tree.exists()
