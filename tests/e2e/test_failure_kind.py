"""End-to-end: every failure path a node can take emits the right structured
`kind` on its `node_failed` JSONL event, and `workflow_end` carries the
failed-id→kind map.

Real subprocesses (no mocks): a script that exits non-zero (`node_error`), a
node blocked by a failed dependency (`upstream_failed`), an exhausted command
eval retry (`gate_failure`), and a node that overruns its
timeout (`timeout`). The overload/backend_error kinds are LLM-backend paths
covered at the unit level (tests/unit/runtime/test_prompt.py); here we pin the
four script-reachable kinds through the full graph → event boundary.
"""
from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.logging import JsonlLogger
from sqrlly.runtime.runner import run_workflow
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph

_PYTHON = sys.executable


def _script(workdir: Path, name: str, body: str) -> str:
    p = workdir / name
    p.write_text(body)
    return str(p)


def _build(workdir: Path) -> Graph:
    fail = _script(workdir, "fail.py", "import sys; sys.exit(1)\n")
    ok = _script(workdir, "ok.py", "print('ok')\n")
    slow = _script(workdir, "slow.py", "import time; time.sleep(3)\n")
    validator = _script(
        workdir, "val.py",
        "import sys; sys.exit(1)\n",
    )
    return Graph(
        name="failure-kinds",
        nodes=[
            {"id": "bad", "name": "bad",
             "run": [_PYTHON, fail]},
            {"id": "blocked", "name": "blocked", "depends_on": ["bad"],
             "run": [_PYTHON, ok]},
            {"id": "gate_fail", "name": "gate_fail",
             "run": [_PYTHON, ok],
             "eval": {"run": [sys.executable, validator]}},
            {"id": "slow", "name": "slow", "timeout": 1.0,
             "run": [_PYTHON, slow]},
        ],
    )


@pytest.mark.asyncio
async def test_node_failed_events_carry_the_right_kind(tmp_path):
    config = _build(tmp_path)
    buf = StringIO()
    logger = JsonlLogger(buf)
    compiled = build_workflow_graph(
        config, DispatchExecutor(workdir=str(tmp_path)), checkpointer=None,
    )
    state = make_initial_state(workdir=str(tmp_path), dry_run=False)
    result = await run_workflow(compiled, state, config, logger=logger)

    events = [json.loads(l) for l in buf.getvalue().strip().split("\n") if l]
    kinds = {
        e["node"]: e["kind"]
        for e in events if e.get("event") == "node_failed"
    }
    assert kinds.get("bad") == "node_error"            # script exit 1
    assert kinds.get("blocked") == "upstream_failed"   # dep 'bad' failed
    assert kinds.get("gate_fail") == "gate_failure"
    assert kinds.get("slow") == "timeout"              # overran node timeout

    # Every failed node has a kind; none defaulted silently to the fallback
    # for a site we claim to classify.
    assert all("kind" in e for e in events if e.get("event") == "node_failed")
    assert set(result["failed_nodes"]) >= {"bad", "blocked", "gate_fail", "slow"}


@pytest.mark.asyncio
async def test_infra_abort_settles_as_node_failed_not_traceback(tmp_path):
    """A worktree-setup infra abort (a setup command exiting non-zero) settles
    the graph as a `node_failed` with `kind: infra` — the run RETURNS with the
    node in `failed_nodes` — instead of escaping as a raw traceback (which would
    leave `workflow_end` at 0/0). Proves the catch-and-settle (no reraise)
    constraint that makes the halt distinguishable from a clean run."""
    from sqrlly.runtime.foreman import ForemanExecutor
    from helpers import init_git_repo

    init_git_repo(tmp_path, files={"f": "x"})
    ok = _script(tmp_path, "ok.py", "print('ok')\n")
    config = Graph(
        name="infra-kind",
        nodes=[{"id": "build", "name": "build", "worktree": "isolated",
                "run": [_PYTHON, ok]}],
        settings={"worktree_setup": ["sh -c 'exit 7'"]},
    )
    buf = StringIO()
    logger = JsonlLogger(buf)
    foreman = ForemanExecutor(
        DispatchExecutor(workdir=str(tmp_path)), str(tmp_path),
        settings=config.settings,
    )
    compiled = build_workflow_graph(config, foreman, checkpointer=None)
    state = make_initial_state(workdir=str(tmp_path), dry_run=False)
    result = await run_workflow(compiled, state, config, logger=logger)

    # The run SETTLED (returned a result) — not a traceback.
    assert "build" in result["failed_nodes"]
    events = [json.loads(l) for l in buf.getvalue().strip().split("\n") if l]
    assert any(
        e.get("event") == "node_failed" and e["node"] == "build"
        and e["kind"] == "infra"
        for e in events
    ), events
    await foreman.close()


@pytest.mark.asyncio
async def test_subgraph_fanout_child_infra_abort_settles(tmp_path):
    """Each workflow branch reports its own isolated member setup failure."""
    import yaml
    from sqrlly.runtime.foreman import ForemanExecutor
    from helpers import init_git_repo

    init_git_repo(tmp_path, files={"f": "x"})
    ok = _script(tmp_path, "ok.py", "print('ok')\n")
    (tmp_path / "member.yaml").write_text(yaml.safe_dump({
        "name": "member",
        "nodes": [{"id": "step", "name": "step", "worktree": "isolated",
                   "run": [_PYTHON, ok]}],
    }))
    manifest = json.dumps({"items": [{"id": "alpha"}, {"id": "beta"}]})
    config = Graph(
        name="sg-fanout-infra",
        nodes=[{
            "id": "cfan", "name": "cfan",
            "run": ["/bin/sh", "-c", f"printf '%s' '{manifest}'"],
            "fan_out": {
                "template": {
                    "workflow": {"file": "member.yaml", "with": {"x": "{{id}}"}},
                },
            },
        }],
        settings={"worktree_setup": ["sh -c 'exit 1'"]},
    )
    buf = StringIO()
    logger = JsonlLogger(buf)
    foreman = ForemanExecutor(
        DispatchExecutor(workdir=str(tmp_path)), str(tmp_path),
        settings=config.settings,
    )
    compiled = build_workflow_graph(
        config, foreman, _base_dir=tmp_path, checkpointer=None, logger=logger,
    )
    state = make_initial_state(workdir=str(tmp_path), dry_run=False)
    result = await run_workflow(compiled, state, config, logger=logger)

    events = [json.loads(line) for line in buf.getvalue().splitlines()]
    kinds = {e["node"]: e["kind"] for e in events if e["event"] == "node_failed"}
    for branch in ("cfan::alpha", "cfan::beta"):
        assert branch in result["failed_nodes"]
        assert kinds[branch] == "upstream_failed"
        assert kinds[f"{branch}::step"] == "infra"
    await foreman.close()
