"""Nested workflow names distinguish identical inner node ids in the same run."""

from pathlib import Path

from helpers import init_git_repo
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor


async def test_nested_qualified_nodes_acquire_distinct_worktrees(tmp_path):
    init_git_repo(tmp_path)
    root = ForemanExecutor(DispatchExecutor(str(tmp_path)), str(tmp_path))
    try:
        first = root.scoped("outer").scoped("left")
        second = root.scoped("outer").scoped("right")
        a = await first.acquire_branch_worktree("inner")
        b = await second.acquire_branch_worktree("inner")
        assert Path(a).is_dir() and Path(b).is_dir() and a != b
        assert await first.acquire_branch_worktree("inner") == a
    finally:
        await root.close()
