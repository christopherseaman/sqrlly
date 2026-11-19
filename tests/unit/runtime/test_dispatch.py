import json
import os
import sys

import pytest

from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.schema.models import Node, Settings


@pytest.mark.asyncio
async def test_run_argv_and_env(tmp_path):
    args = ["{{ value }}", "$(false)", "*.py", ""]
    code = "import json,os,sys;print(json.dumps([sys.argv[1:],os.environ['VALUE'],os.getcwd()]))"
    node = Node(id="run", run={"cmd": [sys.executable, "-c", code, *args],
                                "env": {"VALUE": "{{ value }}"}})
    result = await DispatchExecutor(str(tmp_path)).execute(node, {"value": "two words"})
    assert result.success
    assert json.loads(result.output) == [["two words", "$(false)", "*.py", ""], "two words", str(tmp_path)]


@pytest.mark.asyncio
async def test_run_failure_and_join(tmp_path):
    dispatch = DispatchExecutor(str(tmp_path))
    join = await dispatch.execute(Node(id="join"), {})
    assert join.success and join.output == ""
    missing = await dispatch.execute(Node(id="missing", run=["missing-sqrlly-cmd"]), {})
    assert not missing.success and "missing-sqrlly-cmd" in missing.error
    fail = await dispatch.execute(Node(id="fail", run=[sys.executable, "-c",
        "import sys; print('out'); sys.exit('bad')"]), {})
    assert not fail.success and fail.output == "out\n" and "bad" in fail.error


@pytest.mark.asyncio
async def test_prompt_directory_defaults_and_argv(tmp_path, monkeypatch):
    from sqrlly.schema.models import load_workflow
    workflow = tmp_path / "flow.yaml"
    workflow.write_text("settings:\n  model: top\n  defaults: {agent: {model: writer}}"
                        "\nnodes:\n  - {id: write, agent: body.txt}\n")
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts/body.txt").write_text("Hello {{ person }}")
    graph = load_workflow(workflow)
    program = tmp_path / "codex"
    program.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps([sys.argv[1:],sys.stdin.read()]))")
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    child = tmp_path / "child"
    child.mkdir()
    result = await DispatchExecutor(str(child), graph.settings).execute(graph.nodes[0], {"person": "Ada"})
    assert result.success, result.error
    args, prompt = json.loads(result.output)
    assert args == ["exec", "--skip-git-repo-check", "--model", "writer", "--sandbox", "workspace-write", "-"]
    assert prompt == "Hello Ada"
    assert (result.model, result.provider) == ("writer", "openai")


@pytest.mark.asyncio
async def test_explicit_workflow_dir_context_wins(tmp_path):
    result = await DispatchExecutor(str(tmp_path)).execute(
        Node(id="using", run=["echo", "{{workflow_dir}}"]), {"workflow_dir": "upstream"}
    )
    assert result.output == "upstream\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("retries,expected", [(0, False), (1, True)])
async def test_run_retry_in_same_directory(tmp_path, retries, expected):
    script = tmp_path / "run.py"
    script.write_text("from pathlib import Path\nimport sys\n"
                      "counter = Path('attempts')\n"
                      "attempt = int(counter.read_text()) + 1 if counter.exists() else 1\n"
                      "counter.write_text(str(attempt))\n"
                      "print(attempt)\nsys.exit(attempt == 1)")
    dispatch = DispatchExecutor(str(tmp_path), Settings(defaults={"run": {"backend_max_retries": retries}}))
    result = await dispatch.execute(Node(id="run", run=[sys.executable, str(script)]), {})
    assert result.success is expected
    assert int(result.output) == int((tmp_path / "attempts").read_text()) == 1 + retries


class TestDispatchConcurrencyCap:
    """max_parallel_jobs is enforced by DispatchExecutor itself, so the cap
    applies even when there is no ForemanExecutor — i.e. off-git, where the
    foreman is disabled. Regression for the fan-out saturation blocker: a
    non-git fan-out previously dispatched every child at once."""

    @pytest.mark.asyncio
    async def test_cap_throttles_without_foreman(self, tmp_path):
        import asyncio
        import shutil
        import time

        sleep_bin = shutil.which("sleep") or "/bin/sleep"
        sleep_s = 0.3
        dispatch = DispatchExecutor(
            workdir=str(tmp_path), settings=Settings(max_parallel_jobs=2),
        )

        def _node(i):
            return Node(
                id=f"p{i}", name=f"p{i}",
                run=[sleep_bin, str(sleep_s)],
            )

        start = time.perf_counter()
        results = await asyncio.gather(*[dispatch.execute(_node(i), {}) for i in range(4)])
        elapsed = time.perf_counter() - start

        assert all(r.success for r in results), [r.error for r in results]
        # cap=2 over 4 jobs → two serialized waves ≈ 2×sleep. Uncapped (the
        # bug) runs all four at once ≈ 1×sleep. 1.5× cleanly separates them.
        assert elapsed >= sleep_s * 1.5, (
            f"elapsed {elapsed:.3f}s < {sleep_s * 1.5:.3f}s — cap not enforced"
        )

    @pytest.mark.asyncio
    async def test_high_cap_runs_parallel(self, tmp_path):
        """Control: a cap >= job count runs fully parallel (~1 sleep)."""
        import asyncio
        import shutil
        import time

        sleep_bin = shutil.which("sleep") or "/bin/sleep"
        sleep_s = 0.3
        dispatch = DispatchExecutor(
            workdir=str(tmp_path), settings=Settings(max_parallel_jobs=8),
        )

        def _node(i):
            return Node(
                id=f"p{i}", name=f"p{i}",
                run=[sleep_bin, str(sleep_s)],
            )

        start = time.perf_counter()
        await asyncio.gather(*[dispatch.execute(_node(i), {}) for i in range(4)])
        elapsed = time.perf_counter() - start
        assert elapsed < sleep_s * 2, f"elapsed {elapsed:.3f}s too long for cap=8"
