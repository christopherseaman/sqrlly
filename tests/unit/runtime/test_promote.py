import subprocess

import pytest
from helpers import init_git_repo
from sqrlly.runtime.promote import PromoteConflictError, PromotionPlan, plan_promotions
from sqrlly.runtime.snapshots import SnapshotRun


def _footprints():
    # writer_a and writer_b both touch shared.txt; each also touches a unique file.
    return {
        "writer_a": {"shared.txt": "added", "a.txt": "added"},
        "writer_b": {"shared.txt": "added", "b.txt": "added"},
    }


def test_plan_disjoint_has_no_conflict():
    fp = {"writer_a": {"a.txt": "added"}, "writer_b": {"b.txt": "added"}}
    plan = plan_promotions(fp, "warn")
    assert plan.conflicts == {}
    assert plan.allowed == fp


def test_plan_warn_keeps_full_footprints_and_reports_conflict():
    plan = plan_promotions(_footprints(), "warn")
    assert plan.conflicts == {"shared.txt": ["writer_a", "writer_b"]}
    assert plan.allowed["writer_a"] == {"shared.txt": "added", "a.txt": "added"}
    assert plan.allowed["writer_b"] == {"shared.txt": "added", "b.txt": "added"}


def test_plan_overwrite_matches_warn_allowance():
    assert (plan_promotions(_footprints(), "overwrite").allowed
            == plan_promotions(_footprints(), "warn").allowed)


def test_plan_skip_first_writer_keeps_conflicting_path():
    plan = plan_promotions(_footprints(), "skip")
    # writer_a (first in order) keeps shared.txt; writer_b drops it but
    # keeps its non-conflicting b.txt.
    assert plan.allowed["writer_a"] == {"shared.txt": "added", "a.txt": "added"}
    assert plan.allowed["writer_b"] == {"b.txt": "added"}
    assert plan.conflicts == {"shared.txt": ["writer_a", "writer_b"]}


def test_plan_fail_raises_before_any_decision():
    with pytest.raises(PromoteConflictError) as ei:
        plan_promotions(_footprints(), "fail")
    assert "shared.txt" in str(ei.value)
    assert ei.value.conflicts == {"shared.txt": ["writer_a", "writer_b"]}


def test_plan_fail_no_conflict_returns_plan():
    fp = {"writer_a": {"a.txt": "added"}, "writer_b": {"b.txt": "added"}}
    plan = plan_promotions(fp, "fail")
    assert isinstance(plan, PromotionPlan)
    assert plan.conflicts == {}


def test_plan_skip_three_node_conflict_only_first_keeps():
    fp = {
        "a": {"shared.txt": "modified", "ua.txt": "added"},
        "b": {"shared.txt": "modified", "ub.txt": "added"},
        "c": {"shared.txt": "modified", "uc.txt": "added"},
    }
    plan = plan_promotions(fp, "skip")
    assert plan.allowed["a"] == {"shared.txt": "modified", "ua.txt": "added"}
    assert plan.allowed["b"] == {"ub.txt": "added"}
    assert plan.allowed["c"] == {"uc.txt": "added"}
    assert plan.conflicts == {"shared.txt": ["a", "b", "c"]}


def test_plan_delete_vs_edit_is_a_conflict():
    fp = {"a": {"shared.txt": "deleted"}, "b": {"shared.txt": "modified"}}
    assert plan_promotions(fp, "warn").conflicts == {"shared.txt": ["a", "b"]}
    with pytest.raises(PromoteConflictError):
        plan_promotions(fp, "fail")


def test_plan_empty_footprints_no_conflict_any_mode():
    for mode in ("fail", "warn", "overwrite", "skip"):
        plan = plan_promotions({}, mode)
        assert plan.allowed == {}
        assert plan.conflicts == {}


def test_snapshot_promotes_committed_and_uncommitted_without_touching_base_index(tmp_path):
    def git(root, *args):
        return subprocess.check_output(["git", "-C", root, *args])

    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "t@t")
    git(tmp_path, "config", "user.name", "t")
    file = tmp_path / "dirty"
    file.write_text("staged")
    git(tmp_path, "add", "dirty")
    file.write_text("unstaged")
    (tmp_path / "untracked").write_text("yes")
    (tmp_path / ".sqrlly").mkdir()
    (tmp_path / ".sqrlly" / "private").write_text("private")
    run = SnapshotRun(str(tmp_path), "workflow", "first")
    snapshot = run.capture_in("node")
    wt = tmp_path / ".sqrlly" / "node"
    git(tmp_path, "worktree", "add", "--detach", str(wt), snapshot)
    assert (wt / "dirty").read_text() == "unstaged"
    assert (wt / "untracked").read_text() == "yes"
    assert not (wt / ".sqrlly").exists()
    (wt / "dirty").rename(wt / "new\nname")
    (wt / "untracked").unlink()
    (wt / "link").symlink_to("new\nname")
    git(wt, "add", "-A")
    git(wt, "commit", "-qm", "node commit")
    (wt / "new\nname").chmod(0o755)
    out = run.capture_out("node", str(wt))
    assert git(tmp_path, "show", out + ":new\nname") == b"unstaged"
    run.promote([("node", None)], "fail")
    assert (tmp_path / "link").is_symlink()
    assert (tmp_path / "link").read_text() == "unstaged"
    assert (tmp_path / "new\nname").stat().st_mode & 0o111
    assert not file.exists() and not (tmp_path / "untracked").exists()
    assert git(tmp_path, "show", ":dirty") == b"staged"
    (tmp_path / "new\nname").write_text("later")
    run.promote([("node", None)], "fail")
    assert (tmp_path / "new\nname").read_text() == "later"
    run.close(True)


def test_snapshot_fails_before_writes_and_prunes_only_inactive_prior_runs(tmp_path):
    init_git_repo(tmp_path, files={"a.txt": "orig"})
    old = SnapshotRun(str(tmp_path), "flow", "old")
    old.capture_in("x")
    old.close(False)
    active = SnapshotRun(str(tmp_path), "flow", "active")
    active.capture_in("x")
    run = SnapshotRun(str(tmp_path), "flow", "new")
    sha = run.capture_in("x")
    wt = tmp_path / ".sqrlly" / "x"
    wt.parent.mkdir()
    subprocess.run(["git", "-C", tmp_path, "worktree", "add", "--detach", wt, sha], check=True)
    (wt / "a.txt").write_text("node")
    (wt / "b.txt").write_text("new")
    run.capture_out("x", str(wt))
    (tmp_path / "a.txt").write_text("human")
    with pytest.raises(PromoteConflictError) as err:
        run.promote([("x", None)], "fail")
    assert err.value.conflicts == {"a.txt": ["base", "x"]}
    assert not (tmp_path / "b.txt").exists()
    run.promote([("x", None)], "skip")
    assert (tmp_path / "a.txt").read_text() == "human"
    assert (tmp_path / "b.txt").read_text() == "new"
    run.close(True)
    refs = subprocess.check_output(["git", "-C", tmp_path, "for-each-ref", "--format=%(refname)"]).decode().splitlines()
    assert old.ref("x", "in") not in refs
    assert active.ref("x", "in") in refs and run.ref("x", "out") in refs
    active.close(True)


@pytest.mark.parametrize("mode", ["warn", "overwrite"])
def test_snapshot_nested_filter_respects_outputs_and_conflict_mode(tmp_path, mode):
    (tmp_path / "sub").mkdir()
    init_git_repo(tmp_path, files={"sub/a.txt": "original", "outside.txt": "base"})
    run = SnapshotRun(str(tmp_path), "nested", "new")
    sha = run.capture_in("node")
    wt = tmp_path / ".sqrlly" / "wt"
    wt.parent.mkdir()
    subprocess.run(["git", "-C", tmp_path, "worktree", "add", "--detach", wt, sha], check=True)
    (wt / "sub" / "a.txt").write_text("node")
    (wt / "outside.txt").write_text("node")
    (wt / "sub" / "ignored.txt").write_text("ignored")
    (wt / "sub" / "other.md").write_text("other")
    run.capture_out("node", wt)
    (tmp_path / "sub" / "a.txt").write_text("human")
    plan = run.promote([("node", ["**/*.txt"])], mode, excludes=["ignored.txt"], prefix="sub")
    assert plan.conflicts == {"sub/a.txt": ["base", "node"]}
    assert (tmp_path / "sub" / "a.txt").read_text() == "node"
    assert (tmp_path / "outside.txt").read_text() == "base"
    assert not (tmp_path / "sub" / "other.md").exists()
    assert not (tmp_path / "sub" / "ignored.txt").exists()
    run.close(True)


def test_snapshot_preserves_symlink_parent_and_unrelated_path(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    init_git_repo(base, files={"safe.txt": "orig"})
    run = SnapshotRun(str(base), "workflow", "run")
    sha = run.capture_in("x")
    wt = tmp_path / "wt"
    subprocess.run(["git", "-C", base, "worktree", "add", "--detach", wt, sha], check=True)
    (wt / "parent").mkdir()
    (wt / "parent" / "file").write_text("node")
    (wt / "safe.txt").write_text("node")
    run.capture_out("x", wt)
    external = tmp_path / "external"
    external.mkdir()
    (base / "parent").symlink_to(external)
    with pytest.raises(PromoteConflictError):
        run.promote([("x", None)], "overwrite")
    assert (base / "safe.txt").read_text() == "orig"
    assert not (external / "file").exists()
    run.close(False)


def test_snapshot_recovers_interrupted_git_apply_without_losing_later_edits(tmp_path):
    init_git_repo(tmp_path, files={"one": "base", "two": "base", "three": "base"})
    run = SnapshotRun(str(tmp_path), "workflow", "run")
    sha = run.capture_in("node")
    wt = tmp_path / ".sqrlly" / "tree"
    wt.parent.mkdir()
    subprocess.run(["git", "-C", tmp_path, "worktree", "add", "--detach", wt, sha], check=True)
    for path in ("one", "two", "three"):
        (wt / path).write_text("node")
    out = run.capture_out("node", wt)
    # Recreate a crash after the first path was written, before marking the out ref settled.
    subprocess.run(["git", "-C", tmp_path, "update-ref", out + "-pending", out], check=True)
    (tmp_path / "one").write_text("node")
    (tmp_path / "two").write_text("external")
    with pytest.raises(PromoteConflictError) as err:
        run.promote([("node", None)], "fail")
    assert err.value.conflicts == {"two": ["base", "node"]}
    assert (tmp_path / "three").read_text() == "base"
    (tmp_path / "two").write_text("base")
    run.promote([("node", None)], "fail")
    assert [(tmp_path / p).read_text() for p in ("one", "two", "three")] == ["node"] * 3
    run.close(True)


def test_snapshot_preserves_fifo_at_destination(tmp_path):
    import os
    import stat

    init_git_repo(tmp_path, files={"keep": "base"})
    run = SnapshotRun(str(tmp_path), "workflow", "run")
    sha = run.capture_in("node")
    wt = tmp_path / ".sqrlly" / "tree"
    wt.parent.mkdir()
    subprocess.run(["git", "-C", tmp_path, "worktree", "add", "--detach", wt, sha], check=True)
    (wt / "pipe").write_text("regular")
    (wt / "keep").write_text("node")
    run.capture_out("node", wt)
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(PromoteConflictError):
        run.promote([("node", None)], "overwrite")
    assert stat.S_ISFIFO((tmp_path / "pipe").lstat().st_mode)
    assert (tmp_path / "keep").read_text() == "base"
    run.close(False)


@pytest.mark.parametrize("obstruction", ["file", "symlink", "directory"])
def test_skip_structural_conflict_promotes_independent_path(tmp_path, obstruction):
    base = tmp_path / "base"
    base.mkdir()
    init_git_repo(base, files={"independent": "original"})
    run = SnapshotRun(str(base), "workflow", "run")
    sha = run.capture_in("node")
    wt = tmp_path / "tree"
    subprocess.run(["git", "-C", base, "worktree", "add", "--detach", wt, sha], check=True)
    (wt / "independent").write_text("node")
    if obstruction == "directory":
        (wt / "slot").write_text("replacement")
        (base / "slot").mkdir()
        (base / "slot" / "external").write_text("human")
        blocked = "slot"
    else:
        (wt / "slot").mkdir()
        (wt / "slot" / "result").write_text("node")
        blocked = "slot/result"
        if obstruction == "file":
            (base / "slot").write_text("human")
        else:
            (base / "slot").symlink_to(tmp_path)
    run.capture_out("node", wt)
    plan = run.promote([("node", None)], "skip")
    assert blocked in plan.conflicts
    assert plan.allowed == {"node": {"independent": "modified"}}
    assert (base / "independent").read_text() == "node"
    if obstruction == "directory":
        assert (base / "slot" / "external").read_text() == "human"
    elif obstruction == "file":
        assert (base / "slot").read_text() == "human"
    else:
        assert (base / "slot").is_symlink()
        assert not (tmp_path / "result").exists()
    run.close(True)
