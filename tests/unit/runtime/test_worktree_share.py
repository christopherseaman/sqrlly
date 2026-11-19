"""Shared dependencies and setup operate inside the node worktree."""

import os
import subprocess

import pytest

from helpers import init_git_repo
from sqrlly.runtime.worktree_share import ensure_setup, materialize_shares


@pytest.fixture
def trees(tmp_path):
    base = tmp_path / "repository"
    init_git_repo(base)
    dest = tmp_path / "worktree"
    subprocess.run(["git", "-C", base, "worktree", "add", "--detach", "-q", dest, "HEAD"], check=True)
    return base, dest


def test_shares_use_relative_links_and_preserve_git_excludes(trees):
    base, dest = trees
    dep = base / "node_modules"
    dep.mkdir()
    (dep / "library").write_text("shared")
    exclude = base / ".git" / "info" / "exclude"
    before = exclude.read_bytes()
    for _ in range(2):
        materialize_shares(str(base), str(dest), ["node_modules"])
        link = dest / "node_modules"
        assert link.is_symlink() and not os.path.isabs(os.readlink(link))
        assert (link / "library").read_text() == "shared"
    assert exclude.read_bytes() == before


def test_missing_share_fails(trees):
    base, dest = trees
    with pytest.raises(RuntimeError, match="worktree_share"):
        materialize_shares(str(base), str(dest), ["missing"])


def test_share_cannot_read_outside_base(trees):
    base, dest = trees
    outside = base.parent / "outside"
    outside.mkdir()
    with pytest.raises((ValueError, RuntimeError)):
        materialize_shares(str(base), str(dest), ["../outside"])
    assert not outside.is_symlink()


async def test_setup_sentinel_prevents_reexecution(trees):
    base, dest = trees
    for _ in range(2):
        await ensure_setup(base=str(base), dest=str(dest), commands=["sh -c 'echo ok >> marker'"])
    assert (dest / "marker").read_text() == "ok\n"
    assert (dest / ".sqrlly" / "setup-ok").is_file()


async def test_setup_failure_leaves_no_sentinel(trees):
    base, dest = trees
    with pytest.raises(RuntimeError, match="setup failed"):
        await ensure_setup(base=str(base), dest=str(dest), commands=["false"], retries=0)
    assert not (dest / ".sqrlly" / "setup-ok").exists()
