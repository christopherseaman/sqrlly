"""End-to-end resume-from-checkpoint test using a real ``AsyncSqliteSaver``.

Two-phase scenario:
  - Phase 1: linear chain a -> b -> c. ``b`` fails (env-injected),
    so ``c`` is gated and never fires. Phase ends with
    ``completed_nodes == ["a"]`` and ``failed_nodes == ["b", "c"]``
    (``c`` is marked failed because dep ``b`` failed).
  - Phase 2: clear the failure marker, resume from the same
    checkpoint. The previously-failed ``b`` now succeeds; ``c`` runs
    for the first time.

A side-channel runs-counter file per node lets us assert exactly how
many times each body executed across both phases. This is the
property no existing resume test pins down.

Skip-completed resume (default ``--resume`` behavior since 0.6):
``a`` completed cleanly in phase 1 and is not downstream of any
failure, so it is frozen in phase 2 — runs counter stays at 1.
``b`` was dirty (failed); re-runs. ``c`` is downstream of the
dirty ``b``; runs for the first time. Use ``--rerun-all`` to restore
pre-0.6 full-replay behavior (``a`` re-executes).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.runner import run_workflow
from sqrlly.schema.models import Graph


_PYTHON = sys.executable

# Worker increments a per-node run-counter file, then optionally
# fails based on a marker file. No env-var indirection — keeps the
# test process clean.
_WORKER_SRC = """
import sys
from pathlib import Path
qid = sys.argv[1]
counter = Path(f"runs_{qid}.txt")
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
fail_marker = Path("fail.txt")
if fail_marker.exists() and fail_marker.read_text().strip() == qid:
    print(f"failing:{qid}")
    sys.exit(1)
print(f"ok:{qid}")
"""


def _worker_path(workdir: Path) -> Path:
    p = workdir / "worker.py"
    p.write_text(_WORKER_SRC)
    return p


def _build_chain(workdir: Path) -> Graph:
    """Linear chain: a -> b -> c, each invoking worker.py with its id."""
    worker = _worker_path(workdir)
    return Graph(
        name="resume-chain",
        nodes=[
            {
                "id": "a", "name": "a",
                "run": [_PYTHON, str(worker), "a"],
            },
            {
                "id": "b", "name": "b", "depends_on": ["a"],
                "run": [_PYTHON, str(worker), "b"],
            },
            {
                "id": "c", "name": "c", "depends_on": ["b"],
                "run": [_PYTHON, str(worker), "c"],
            },
        ],
    )


def _read_runs(workdir: Path, qid: str) -> int:
    p = workdir / f"runs_{qid}.txt"
    return int(p.read_text()) if p.exists() else 0


async def _run_phase(
    workdir: Path,
    config: Graph,
    db_path: str,
    thread_id: str,
    *,
    resume: bool,
    resume_from: tuple = (),
    rerun_all: bool = False,
) -> dict:
    """Mirror cli/main.py's execute path with a real AsyncSqliteSaver."""
    async with AsyncSqliteSaver.from_conn_string(db_path) as cp:
        await cp.setup()
        from sqrlly.cli.main import _seed_state
        state = await _seed_state(
            cp, config, str(workdir), resume=resume, resume_from=resume_from,
            rerun_all=rerun_all, entry=None, thread_id=thread_id,
        )
        compiled = build_workflow_graph(
            config,
            DispatchExecutor(workdir=str(workdir)),
            checkpointer=cp,
        )
        return await run_workflow(
            compiled, state, config, thread_id=thread_id,
        )


class TestResumeFromCheckpoint:
    @pytest.mark.asyncio
    async def test_resume_after_mid_chain_failure(self, tmp_path):
        """a -> b(fail) -> c, then resume with b unblocked.

        Skip-completed resume: ``a`` completed cleanly and is NOT
        downstream of the failure, so it is frozen — runs counter
        stays at 1 across both phases.
        """
        config = _build_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "resume-test"

        # Phase 1: fail b.
        (tmp_path / "fail.txt").write_text("b")
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert result_1["completed_nodes"] == {"a"}
        assert "b" in result_1["failed_nodes"]
        # ``c`` is marked failed because its dep failed; the body
        # never ran.
        assert "c" in result_1["failed_nodes"]
        assert _read_runs(tmp_path, "a") == 1
        assert _read_runs(tmp_path, "b") == 1
        assert _read_runs(tmp_path, "c") == 0

        # Phase 2: clear failure, resume.
        (tmp_path / "fail.txt").unlink()
        result_2 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=True,
        )
        assert "b" in result_2["completed_nodes"]
        assert "c" in result_2["completed_nodes"]
        assert result_2["failed_nodes"] == set()

        # Skip-completed resume: a completed cleanly and is NOT downstream of
        # the failure, so it is frozen — runs ONCE total.
        assert _read_runs(tmp_path, "a") == 1
        # b failed in phase 1, is dirty, re-runs in phase 2 — counter == 2.
        assert _read_runs(tmp_path, "b") == 2
        # c is downstream of the failed b (dirty), ran for the first time.
        assert _read_runs(tmp_path, "c") == 1

        # State preservation: phase 1's completed ``a`` is in
        # completed_nodes after phase 2. The set-union reducer merges
        # the skip-completed state write (a was frozen, not re-executed).
        assert result_2["completed_nodes"] == {"a", "b", "c"}

    @pytest.mark.asyncio
    async def test_rerun_all_restores_full_replay(self, tmp_path):
        """--rerun-all reproduces pre-0.6 behavior: a re-executes."""
        config = _build_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "rerun-all"
        (tmp_path / "fail.txt").write_text("b")
        await _run_phase(tmp_path, config, db_path, thread_id, resume=False)
        (tmp_path / "fail.txt").unlink()
        await _run_phase(
            tmp_path, config, db_path, thread_id, resume=True, rerun_all=True,
        )
        assert _read_runs(tmp_path, "a") == 2  # full replay

    @pytest.mark.asyncio
    async def test_resume_from_reruns_node_and_downstream(self, tmp_path):
        """All clean; --resume-from b => a frozen, b & c re-run."""
        config = _build_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "resume-from"
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert result_1["completed_nodes"] == {"a", "b", "c"}
        await _run_phase(
            tmp_path, config, db_path, thread_id,
            resume=True, resume_from=("b",),
        )
        assert _read_runs(tmp_path, "a") == 1   # frozen
        assert _read_runs(tmp_path, "b") == 2   # rerun target
        assert _read_runs(tmp_path, "c") == 2   # downstream of b


_ECHO = shutil.which("echo") or "/bin/echo"

# The eval runs in the workdir and exits 1 while the marker exists.
_VALIDATOR_SRC = (
    "import sys; from pathlib import Path; sys.stdin.read(); "
    "sys.exit(Path('eval_fail.txt').exists())"
)


def _build_gated_chain(workdir: Path) -> Graph:
    """Re-evaluate the dirty node on resume, even when it completed previously."""
    worker = _worker_path(workdir)
    validator = workdir / "validator.py"
    validator.write_text(_VALIDATOR_SRC)
    return Graph(
        name="resume-gated",
        nodes=[
            {
                "id": "up", "name": "up",
                "run": [_PYTHON, str(worker), "up"],
            },
            {
                "id": "g", "name": "g", "depends_on": ["up"],
                "run": [_PYTHON, str(worker), "g"],
                "eval": {"run": [sys.executable, str(validator)]},
                "route": [{"when": "eval.exit_code != 0", "then": "fail"}],
            },
        ],
    )


def _build_fanout_chain(workdir: Path) -> Graph:
    """The join runs after the repeated command settles."""
    worker = _worker_path(workdir)
    manifest = json.dumps({"items": [{"id": "x"}]})
    return Graph(
        name="resume-fanout",
        nodes=[
            {
                "id": "up", "name": "up",
                "run": [_PYTHON, str(worker), "up"],
            },
            {"id": "manifest",
             "run": [_ECHO, "-n", manifest],
             "depends_on": ["up"]},
            {"id": "fan",
             "for_each": "manifest",
             "depends_on": ["manifest"],
             "run": [_ECHO, "-n", "child-{{item.id}}"]},
            {"id": "agg",
             "name": "agg",
             "run": [_PYTHON, str(worker), "agg"],
             "depends_on": ["fan"]},
        ],
    )


class TestResumeDirtyGuards:
    """Pins that within-run guards (Decision node, fan-out final barrier) treat
    dirty nodes as not-done when completed_nodes is reseeded to skip-only."""

    @pytest.mark.asyncio
    async def test_gated_node_reruns_eval_on_resume_from(self, tmp_path):
        """Changing the marker changes the route on a resumed execution."""
        config = _build_gated_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "gated-resume"

        # The successful first eval completes g.
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert result_1["completed_nodes"] == {"up", "g"}
        assert _read_runs(tmp_path, "g") == 1

        # Flip the validator to fail before phase 2.
        (tmp_path / "eval_fail.txt").write_text("fail")

        # Phase 2: --resume-from up → up and g are dirty.
        # A dirty node uses the new measurement when choosing its route.
        result_2 = await _run_phase(
            tmp_path, config, db_path, thread_id,
            resume=True, resume_from=("up",),
        )
        assert "g" in result_2["failed_nodes"]
        assert "g" not in result_2["completed_nodes"]
        # Body re-ran (counter incremented on this run).
        assert _read_runs(tmp_path, "g") == 2

    @pytest.mark.asyncio
    async def test_join_reruns_on_resume_from_ancestor(self, tmp_path):
        """The downstream join runs again after resuming from the producer."""
        config = _build_fanout_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "fanout-resume"

        # Phase 1: full run.
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "fan" in result_1["completed_nodes"]
        assert "agg" in result_1["completed_nodes"]
        assert _read_runs(tmp_path, "agg") == 1

        # Phase 2: --resume-from up → fan and agg are dirty.
        result_2 = await _run_phase(
            tmp_path, config, db_path, thread_id,
            resume=True, resume_from=("up",),
        )
        assert "agg" in result_2["completed_nodes"]
        # Aggregation body re-ran.
        assert _read_runs(tmp_path, "agg") == 2


def _build_failable_fanout(workdir: Path) -> Graph:
    """up -> cfan (fans over alpha/beta/gamma) ; each child runs worker.py
    keyed by the per-item id, so a per-child run-counter (runs_<id>.txt) and
    the fail.txt marker (holding a bare item id) control exactly one child's
    failure. Branch counters record each child execution."""
    worker = _worker_path(workdir)
    manifest = json.dumps(
        {"items": [{"id": "alpha"}, {"id": "beta"}, {"id": "gamma"}]}
    )
    return Graph(
        name="resume-failable-fanout",
        nodes=[
            {
                "id": "up", "name": "up",
                "run": [_PYTHON, str(worker), "up"],
            },
            {"id": "manifest",
             "run": [_ECHO, "-n", manifest],
             "depends_on": ["up"]},
            {"id": "cfan",
             "for_each": "manifest",
             "depends_on": ["manifest"],
             "run": [_PYTHON, str(worker), "{{item.id}}"]},
        ],
    )


class TestResumeFailedChild:
    @pytest.mark.asyncio
    async def test_only_failed_child_reruns_on_resume(self, tmp_path):
        """Fan-out over alpha/beta/gamma; beta fails in phase 1. On resume
        (bare --resume, no resume-from) ONLY beta re-runs; alpha and gamma
        are frozen — their run counters stay at 1. The parent re-fans (dirty
        via the failed child), beta succeeds, the run completes clean."""
        config = _build_failable_fanout(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "failed-child-resume"

        # Phase 1: beta fails.
        (tmp_path / "fail.txt").write_text("beta")
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "cfan" in result_1["failed_nodes"]
        assert "cfan::alpha" in result_1["completed_nodes"]
        assert "cfan::gamma" in result_1["completed_nodes"]
        assert "cfan::beta" in result_1["failed_nodes"]
        assert _read_runs(tmp_path, "alpha") == 1
        assert _read_runs(tmp_path, "beta") == 1
        assert _read_runs(tmp_path, "gamma") == 1

        # Phase 2: clear the marker, bare --resume (no resume-from).
        (tmp_path / "fail.txt").unlink()
        result_2 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=True,
        )
        # The formerly-failed child now succeeds.
        assert "cfan::beta" in result_2["completed_nodes"]
        assert result_2["failed_nodes"] == set()

        # ONLY beta re-ran. alpha and gamma frozen — counters stay at 1.
        assert _read_runs(tmp_path, "alpha") == 1, "completed sibling re-billed"
        assert _read_runs(tmp_path, "gamma") == 1, "completed sibling re-billed"
        # beta ran once per phase = 2.
        assert _read_runs(tmp_path, "beta") == 2

        # 'up' completed cleanly and is upstream of the dirty parent (not
        # downstream), so it is frozen — runs once total.
        assert _read_runs(tmp_path, "up") == 1


# A parent that emits a DUPLICATE-id manifest until dedup.txt exists, then a
# unique one. Phase 1 → duplicate → _fan_ fails the parent. Phase 2 (after
# writing dedup.txt) → unique manifest → re-fans cleanly.
_DUP_PARENT_SRC = """
import json, sys
from pathlib import Path
if Path("dedup.txt").exists():
    items = [{"id": "a"}, {"id": "b"}]
else:
    items = [{"id": "dup"}, {"id": "dup"}]
print(json.dumps({"items": items}))
"""


def _build_dup_manifest_fanout(workdir: Path) -> Graph:
    parent = workdir / "dup_parent.py"
    parent.write_text(_DUP_PARENT_SRC)
    return Graph(
        name="resume-dup-manifest",
        nodes=[
            {"id": "manifest",
             "run": [_PYTHON, str(parent)]},
            {"id": "dp",
             "for_each": "manifest",
             "depends_on": ["manifest"],
             "run": [_ECHO, "-n", "child-{{item.id}}"]},
        ],
    )


class TestResumeAfterDuplicateManifest:
    @pytest.mark.asyncio
    async def test_duplicate_manifest_recovers_after_rerunning_producer(
        self, tmp_path,
    ):
        """Correcting the producer allows the failed repeating node to complete."""
        config = _build_dup_manifest_fanout(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "dup-manifest-resume"

        # Phase 1: duplicate manifest → parent fails via _fan_ failure-update.
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "dp" in result_1["failed_nodes"]
        assert not any(
            k.startswith("dp::") for k in result_1["completed_nodes"]
        )
        assert any(
            "duplicate" in e.get("error", "") for e in result_1["errors"]
        )

        # Rerun the producer so the repeated node receives its corrected manifest.
        (tmp_path / "dedup.txt").write_text("go")
        result_2 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=True, resume_from=("manifest",),
        )
        assert "dp" in result_2["completed_nodes"]
        assert "dp::a" in result_2["completed_nodes"]
        assert "dp::b" in result_2["completed_nodes"]
        assert result_2["failed_nodes"] == set()


# Dispatcher that DRIFTS its child ids across the re-fan: emits r1-* on the
# first run, r2-* once refan.txt exists. Mirrors a real dispatcher that mints
# non-deterministic ids (uuid / counter / positional index).
_DRIFT_DISPATCHER_SRC = """
import json
from pathlib import Path
prefix = "r2" if Path("refan.txt").exists() else "r1"
items = [{"id": prefix + "-alpha"}, {"id": prefix + "-beta"}, {"id": prefix + "-gamma"}]
print(json.dumps({"items": items}))
"""


def _build_drift_fanout(workdir: Path, drift_policy: str = "fail") -> Graph:
    worker = _worker_path(workdir)
    dispatcher = workdir / "drift_dispatcher.py"
    dispatcher.write_text(_DRIFT_DISPATCHER_SRC)
    return Graph(
        name="resume-drift-fanout",
        nodes=[
            {"id": "manifest",
             "run": [_PYTHON, str(dispatcher)]},
            {"id": "cfan",
             "for_each": "manifest",
             "depends_on": ["manifest"],
             "run": [_PYTHON, str(worker), "{{item.id}}"]},
        ],
        settings={"on_manifest_drift": drift_policy},
    )


# A 2-node member subgraph (step1 -> step2); each inner node runs the worker
# keyed by the per-branch {{childid}}. `__PY__` / `__WORKER__` are substituted
# (not f-string, to keep the literal `{{childid}}` Jinja var intact).
_MEMBER_SUBGRAPH_TMPL = """
name: member
nodes:
  - id: step1
    name: step1
    run: ["__PY__", "__WORKER__", "{{childid}}", "step1"]
  - id: step2
    name: step2
    depends_on: [step1]
    run: ["__PY__", "__WORKER__", "{{childid}}", "step2"]
"""


def _build_subgraph_template_fanout(workdir: Path) -> Graph:
    """Top-level fan-out over stable ids alpha/beta/gamma whose template is a
    2-node member subgraph — the shape whose branches record both
    `<parent>::<item>` and `<parent>::<item>::<inner>` in completed_nodes."""
    worker = _worker_path(workdir)
    member = workdir / "member.yaml"
    member.write_text(
        _MEMBER_SUBGRAPH_TMPL
        .replace("__PY__", _PYTHON)
        .replace("__WORKER__", str(worker))
    )
    manifest = json.dumps(
        {"items": [{"id": "alpha"}, {"id": "beta"}, {"id": "gamma"}]}
    )
    return Graph(
        name="resume-subgraph-template-fanout",
        nodes=[
            {"id": "manifest",
             "run": [_ECHO, "-n", manifest]},
            {"id": "cfan",
             "for_each": "manifest",
             "depends_on": ["manifest"],
             "workflow": {"file": str(member), "with": {"childid": "{{item.id}}"}}},
        ],
    )


class TestResumeManifestDrift:
    @pytest.mark.asyncio
    async def test_drift_fails_loud_before_any_send(self, tmp_path):
        """Phase 1 fans over r1-*, r1-beta fails. Phase 2's dispatcher DRIFTS to
        r2-* ids. With `on_manifest_drift: fail` (default), the re-fan dispatcher
        detects that every prior child id vanished and HALTS the parent before
        any Send — so NO r2-* child is billed (the silent N-wide re-bill that
        would otherwise orphan r1-beta and vanish r1-alpha/r1-gamma)."""
        config = _build_drift_fanout(tmp_path, drift_policy="fail")
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "drift-fail"

        # Phase 1: r1-beta fails.
        (tmp_path / "fail.txt").write_text("r1-beta")
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "cfan::r1-alpha" in result_1["completed_nodes"]
        assert "cfan::r1-gamma" in result_1["completed_nodes"]
        assert "cfan::r1-beta" in result_1["failed_nodes"]

        # Phase 2: drift to r2-*, clear the failure marker (irrelevant — the
        # guard halts before any child runs).
        (tmp_path / "fail.txt").unlink()
        (tmp_path / "refan.txt").write_text("go")
        result_2 = await _run_phase(tmp_path, config, db_path, thread_id, resume=True, resume_from=("manifest",))

        # Parent failed loud with a drift error; NO r2-* child was dispatched.
        assert "cfan" in result_2["failed_nodes"]
        assert any(
            "drift" in e.get("error", "").lower() for e in result_2["errors"]
        ), result_2["errors"]
        assert _read_runs(tmp_path, "r2-alpha") == 0, "drifted child was billed"
        assert _read_runs(tmp_path, "r2-beta") == 0, "drifted child was billed"
        assert _read_runs(tmp_path, "r2-gamma") == 0, "drifted child was billed"

    @pytest.mark.asyncio
    async def test_drift_to_empty_manifest_fails_loud(self, tmp_path):
        """Draining the manifest counts as removing every previous item."""
        worker = _worker_path(tmp_path)
        dispatcher = tmp_path / "drain_dispatcher.py"
        dispatcher.write_text(
            "import json\n"
            "from pathlib import Path\n"
            "items = [] if Path('drain.txt').exists() else "
            "[{'id': 'alpha'}, {'id': 'beta'}, {'id': 'gamma'}]\n"
            "print(json.dumps({'items': items}))\n"
        )
        config = Graph(
            name="resume-drain-fanout",
            nodes=[{"id": "manifest",
                    "run": [_PYTHON, str(dispatcher)]},
                   {"id": "cfan",
                    "for_each": "manifest",
                    "depends_on": ["manifest"],
                    "run": [_PYTHON, str(worker), "{{item.id}}"]}],
            settings={"on_manifest_drift": "fail"},
        )
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "drift-drain"

        (tmp_path / "fail.txt").write_text("beta")
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "cfan::beta" in result_1["failed_nodes"]

        # Phase 2: manifest drains to empty. The failed child must NOT be
        # silently abandoned.
        (tmp_path / "fail.txt").unlink()
        (tmp_path / "drain.txt").write_text("go")
        result_2 = await _run_phase(tmp_path, config, db_path, thread_id, resume=True, resume_from=("manifest",))
        assert "cfan" in result_2["failed_nodes"], (
            "empty-manifest drift silently completed green"
        )
        assert any(
            "drift" in e.get("error", "").lower() for e in result_2["errors"]
        ), result_2["errors"]

    @pytest.mark.asyncio
    async def test_stable_id_resume_does_not_false_fire(self, tmp_path):
        """Existing successful children keep their original call counts."""
        config = _build_failable_fanout(tmp_path)  # stable ids alpha/beta/gamma
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "stable-no-false-fire"

        (tmp_path / "fail.txt").write_text("beta")
        await _run_phase(tmp_path, config, db_path, thread_id, resume=False)

        (tmp_path / "fail.txt").unlink()
        result_2 = await _run_phase(tmp_path, config, db_path, thread_id, resume=True)
        # Guard did NOT fire.
        assert "cfan" not in result_2["failed_nodes"]
        assert not any(
            "drift" in e.get("error", "").lower() for e in result_2["errors"]
        )
        # Only the failed child re-ran; siblings frozen.
        assert _read_runs(tmp_path, "alpha") == 1
        assert _read_runs(tmp_path, "gamma") == 1
        assert _read_runs(tmp_path, "beta") == 2

    @pytest.mark.asyncio
    async def test_subgraph_template_resume_does_not_false_fire(self, tmp_path):
        """Resuming the repeated workflow reruns only its failed item."""
        config = _build_subgraph_template_fanout(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "subgraph-no-false-fire"

        # Phase 1: beta branch's step1 fails (so step2 never runs).
        (tmp_path / "fail.txt").write_text("beta")
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "cfan::beta" in result_1["failed_nodes"]
        # alpha/gamma ran both inner steps (==2); beta failed at step1 (==1).
        assert _read_runs(tmp_path, "alpha") == 2
        assert _read_runs(tmp_path, "gamma") == 2
        assert _read_runs(tmp_path, "beta") == 1

        # Phase 2: resume through the real seed path — guard must NOT fire.
        (tmp_path / "fail.txt").unlink()
        result_2 = await _run_phase(tmp_path, config, db_path, thread_id, resume=True)
        assert "cfan" not in result_2["failed_nodes"]
        assert not any(
            "drift" in e.get("error", "").lower() for e in result_2["errors"]
        )
        # Only beta's subgraph re-ran (step1+step2 → +2 = 3); siblings frozen.
        assert _read_runs(tmp_path, "alpha") == 2
        assert _read_runs(tmp_path, "gamma") == 2
        assert _read_runs(tmp_path, "beta") == 3

    @pytest.mark.asyncio
    async def test_drift_warn_proceeds(self, tmp_path):
        """Same drift, but `on_manifest_drift: warn` → the re-fan proceeds with
        the new manifest (opt-in for an author who intends a changed manifest on
        resume). The r2-* children run; the run completes clean."""
        config = _build_drift_fanout(tmp_path, drift_policy="warn")
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "drift-warn"

        (tmp_path / "fail.txt").write_text("r1-beta")
        await _run_phase(tmp_path, config, db_path, thread_id, resume=False)

        (tmp_path / "fail.txt").unlink()
        (tmp_path / "refan.txt").write_text("go")
        result_2 = await _run_phase(tmp_path, config, db_path, thread_id, resume=True, resume_from=("manifest",))

        # warn let the drifted manifest through: r2-* children ran, run clean.
        assert result_2["failed_nodes"] == set()
        assert _read_runs(tmp_path, "r2-alpha") == 1
        assert _read_runs(tmp_path, "r2-beta") == 1
        assert _read_runs(tmp_path, "r2-gamma") == 1


async def test_clean_resume_expands_previously_empty_file(tmp_path):
    source = tmp_path / "items.json"
    source.write_text("[]")
    config = Graph(nodes=[
        {"id": "build", "for_each": {"file": "items.json"}, "run": ["printf", "%s", "{{item}}"]},
        {"id": "join", "depends_on": ["build"], "run": ["printf", "%s", "{{build}}"]},
    ])
    db = str(tmp_path / "checkpoint.db")
    before = await _run_phase(tmp_path, config, db, "empty-then-added", resume=False)
    assert json.loads(before["node_outputs"]["join"]) == []
    source.write_text('["new"]')
    after = await _run_phase(tmp_path, config, db, "empty-then-added", resume=True)
    assert not after["failed_nodes"], after["errors"]
    assert json.loads(after["node_outputs"]["join"]) == ["new"]
    assert len([key for key in after["completed_nodes"] if key.startswith("build::")]) == 1
