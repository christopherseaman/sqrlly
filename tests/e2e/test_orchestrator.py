"""Orchestrator tests — full LangGraph pipeline execution.

Most tests use real subprocesses via DispatchExecutor + command nodes.
MockExecutor is used only where the thing under test is wiring/context
propagation rather than executor semantics.
"""

import shutil

import sys

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.state import make_initial_state
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.schema.models import Node

from helpers import cmd_phase, fail_phase, make_config

_ECHO = shutil.which("echo") or "/bin/echo"
_FALSE = shutil.which("false") or "/bin/false"


# ---------------------------------------------------------------------------
# Single node execution
# ---------------------------------------------------------------------------


class TestSingleNodeExecution:
    @pytest.mark.asyncio
    async def test_single_phase_completes(self):
        config = make_config([cmd_phase("p1", output="hello")])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert "p1" in result["completed_nodes"]
        assert result["node_outputs"]["p1"] == "hello"

    @pytest.mark.asyncio
    async def test_dry_run_skips_execution(self):
        from mock_executor import MockExecutor

        mock = MockExecutor()
        config = make_config([cmd_phase("p1")])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(dry_run=True))
        assert "p1" in result["completed_nodes"]
        assert "dry-run" in result["node_outputs"]["p1"]
        assert mock.execution_order == [], "executor should not be called in dry-run"


# ---------------------------------------------------------------------------
# Linear execution and context passing
# ---------------------------------------------------------------------------


class TestLinearExecution:
    @pytest.mark.asyncio
    async def test_two_nodes_execute_in_order(self):
        config = make_config([
            cmd_phase("a", output="a-out"),
            cmd_phase("b", output="b-out", depends_on=["a"]),
        ])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert result["completed_nodes"] == {"a", "b"}
        assert result["node_outputs"]["a"] == "a-out"
        assert result["node_outputs"]["b"] == "b-out"

    @pytest.mark.asyncio
    async def test_three_phase_chain(self):
        config = make_config([
            cmd_phase("a", output="1"),
            cmd_phase("b", output="2", depends_on=["a"]),
            cmd_phase("c", output="3", depends_on=["b"]),
        ])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert set(result["completed_nodes"]) == {"a", "b", "c"}
        assert result["node_outputs"]["a"] == "1"
        assert result["node_outputs"]["b"] == "2"
        assert result["node_outputs"]["c"] == "3"


# ---------------------------------------------------------------------------
# Parallel / diamond execution
# ---------------------------------------------------------------------------


class TestParallelExecution:
    @pytest.mark.asyncio
    async def test_diamond_all_complete(self):
        """Diamond: A -> (B, C) -> D"""
        config = make_config([
            cmd_phase("a", output="root"),
            cmd_phase("b", output="left", depends_on=["a"]),
            cmd_phase("c", output="right", depends_on=["a"]),
            cmd_phase("d", output="join", depends_on=["b", "c"]),
        ])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert set(result["completed_nodes"]) == {"a", "b", "c", "d"}
        assert result["node_outputs"]["a"] == "root"
        assert result["node_outputs"]["b"] == "left"
        assert result["node_outputs"]["c"] == "right"
        assert result["node_outputs"]["d"] == "join"

    @pytest.mark.asyncio
    async def test_multi_gated_predecessor_joins_correctly(self, tmp_path):
        """Regression for the multi-gated-predecessor join bug.

        The bug: a gated node X's router emits ALL its dependents via
        conditional edges. If a dependent Y depends on BOTH X and some
        other node Z (where Z is deeper in a chain from a common
        upstream), X's completion routes to Y in the same superstep that
        Z starts running. Y then runs in the next superstep with only X
        in its completed_nodes — Z still pending.

        Topology mirrors the absurd-paper bug that triggered this fix:
            root
              ├─→ chain_1 ─→ chain_2          (slow branch)
              └─→ fast_gated (gate passes)     (fast gated branch)
                                                           ↘
            join depends_on [chain_2, fast_gated]          ←
        """
        from mock_executor import MockExecutor

        pass_script = tmp_path / "pass.py"
        pass_script.write_text("import sys\nsys.stdin.read()\nprint('1.0')\n")
        gate = {"run": [sys.executable, str(pass_script)]}

        mock = MockExecutor()
        config = make_config([
            cmd_phase("root"),
            cmd_phase("chain_1", depends_on=["root"]),
            cmd_phase("chain_2", depends_on=["chain_1"]),
            cmd_phase("fast_gated", depends_on=["root"], eval=gate),
            cmd_phase("join_node", depends_on=["chain_2", "fast_gated"]),
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "join_node" in result["completed_nodes"]
        ctx = mock.received_contexts["join_node"]
        assert ctx.get("chain_2") == "[mock] chain_2 completed"
        assert ctx.get("fast_gated") == "[mock] fast_gated completed"

    @pytest.mark.asyncio
    async def test_independent_nodes_both_complete(self):
        """Two nodes with no dependencies both run."""
        config = make_config([
            cmd_phase("x", output="x-out"),
            cmd_phase("y", output="y-out"),
        ])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert set(result["completed_nodes"]) == {"x", "y"}
        assert result["node_outputs"]["x"] == "x-out"
        assert result["node_outputs"]["y"] == "y-out"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_failed_phase_skips_dependent(self):
        config = make_config([
            fail_phase("a"),
            cmd_phase("b", depends_on=["a"]),
        ])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert "a" in result["failed_nodes"]
        assert "b" in result["failed_nodes"]
        assert "b" not in result["completed_nodes"]
        errors_by_phase = {e["node"]: e["error"] for e in result["errors"]}
        assert "dependency" in errors_by_phase["b"].lower()

    @pytest.mark.asyncio
    async def test_parallel_failure_one_branch(self):
        """In diamond, if B fails, C still runs; D is skipped."""
        config = make_config([
            cmd_phase("a", output="ok"),
            fail_phase("b", depends_on=["a"]),
            cmd_phase("c", output="ok", depends_on=["a"]),
            cmd_phase("d", output="ok", depends_on=["b", "c"]),
        ])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert "a" in result["completed_nodes"]
        assert "c" in result["completed_nodes"]
        assert "b" in result["failed_nodes"]
        assert "d" in result["failed_nodes"]

    @pytest.mark.asyncio
    async def test_error_captures_message(self):
        config = make_config([fail_phase("p1")])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert "p1" in result["failed_nodes"]
        assert len(result["errors"]) > 0
        assert result["errors"][0]["node"] == "p1"


# ---------------------------------------------------------------------------
# Gate integration with real validators
# ---------------------------------------------------------------------------


class TestGateIntegration:
    @pytest.mark.asyncio
    async def test_multi_retry_then_pass(self, tmp_path):
        """Two failed command evals precede a successful third attempt."""
        counter = tmp_path / "n.txt"
        counter.write_text("0")
        validator = tmp_path / "staged.py"
        validator.write_text(
            f"import sys\nsys.stdin.read()\n"
            f"n = int(open('{counter}').read())\n"
            f"open('{counter}', 'w').write(str(n + 1))\n"
            "sys.exit(n < 2)\n"
        )
        config = make_config([
            cmd_phase(
                "p1", output="data",
                eval={"run": [sys.executable, str(validator)]},
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_nodes"]
        records = result["evaluations"]["p1"]
        attempts = [r["attempt"] for r in records]
        assert attempts == [1, 2, 3]
        assert records[-1]["result"]["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_passing_gate_completes(self, tmp_path):
        script = tmp_path / "pass.py"
        script.write_text("print('{\"score\":0.95}')")
        config = make_config([
            cmd_phase("p1", output="data",
                      eval={"run": [sys.executable, str(script)]}),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert result["evaluations"]["p1"][-1]["result"]["score"] == 0.95
        assert "p1" in result["completed_nodes"]

    @pytest.mark.asyncio
    async def test_gate_pass_allows_dependent(self, tmp_path):
        script = tmp_path / "pass.py"
        script.write_text("print(1.0)")
        config = make_config([
            cmd_phase("a", output="ok",
                      eval={"run": [sys.executable, str(script)]}),
            cmd_phase("b", output="ok", depends_on=["a"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "a" in result["completed_nodes"]
        assert "b" in result["completed_nodes"]

    @pytest.mark.asyncio
    async def test_default_retry_exhaustion_fails(self, tmp_path):
        script = tmp_path / "fail.py"
        script.write_text("raise SystemExit(1)")
        config = make_config([
            cmd_phase("p1", output="data",
                      eval={"run": [sys.executable, str(script)]}),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["failed_nodes"]
        assert any("exhausted" in e["error"].lower() for e in result["errors"])

    @pytest.mark.asyncio
    async def test_nonzero_exit_without_matching_case_completes(self, tmp_path):
        script = tmp_path / "fail.py"
        script.write_text("raise SystemExit(1)")
        config = make_config([
            cmd_phase("p1", output="data",
                      eval={"run": [sys.executable, str(script)]}, route=[{"when": "eval.exit_code == 2", "then": "fail"}]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["completed_nodes"]
        assert result["evaluations"]["p1"][-1]["result"]["exit_code"] == 1

    @pytest.mark.asyncio
    async def test_resume_skipped_gate_still_routes_downstream(self, tmp_path):
        """A gated node frozen on --resume must STILL emit its pass goto so
        its dependent runs — the collapsed gate's step-2 skip branch returns
        Command(goto=pass_goto) with no update and no validator call. The
        validator is a broken script (would raise if it ran), proving the
        gate skipped it yet still routed to the dependent."""
        broken = tmp_path / "broken.py"
        broken.write_text("import sys; sys.exit(3)")  # nonzero => eval error if run
        config = make_config([
            cmd_phase("a", output="frozen-out",
                      eval={"run": [sys.executable, str(broken)]}),
            cmd_phase("b", output="b-ran", depends_on=["a"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        # Reseed as --resume would: a is frozen (skip + completed), so its
        # exec and gate both skip; the gate must still route to b.
        state = make_initial_state(workdir=str(tmp_path))
        state["completed_nodes"] = {"a"}
        state["node_outputs"] = {"a": "frozen-out"}
        state["_resume_skip"] = {"a"}
        result = await graph.ainvoke(state)
        assert "a" in result["completed_nodes"]
        assert "b" in result["completed_nodes"]
        assert result["node_outputs"]["b"] == "b-ran"
        # The validator never ran → no eval record was produced this run.
        assert result.get("evaluations", {}).get("a", []) == []

    @pytest.mark.asyncio
    async def test_gate_eval_timeout_routes_end_via_command(self, tmp_path):
        """An eval-validator timeout is an infra failure: the collapsed gate
        returns Command(update=failure, goto=END) so the run terminates
        cleanly (no hang, no orphaned path). The node lands in failed_nodes
        with a timeout error and no evaluation record."""
        slow = tmp_path / "slow.py"
        slow.write_text("import time, sys; sys.stdin.read(); time.sleep(5)")
        config = make_config([
            cmd_phase("p1", output="data",
                      eval={"run": [sys.executable, str(slow)], "timeout": 0.2},
                      timeout=1),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["failed_nodes"]
        assert any("timed out" in e["error"].lower() for e in result["errors"])
        assert result.get("evaluations", {}).get("p1", []) == []


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_traces_gated_chain(self):
        config = make_config([
            cmd_phase("a", eval={"run": ["true"]}),
            cmd_phase("b", depends_on=["a"]),
        ])
        graph = build_workflow_graph(config)
        result = await graph.ainvoke(make_initial_state(dry_run=True))
        assert "a" in result["completed_nodes"]
        assert "b" in result["completed_nodes"]


# ---------------------------------------------------------------------------
# DispatchExecutor routing
# ---------------------------------------------------------------------------


class TestDispatchExecutor:
    @pytest.mark.asyncio
    async def test_command_phase_runs_subprocess(self):
        executor = DispatchExecutor()
        node = Node(
            id="c1", name="C1",
            run=[_ECHO, "hello"],
        )
        result = await executor.execute(node, {})
        assert result.success is True
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_gate_only_phase(self):
        """Gate-only-by-elision: a node with no kind returns empty output."""
        executor = DispatchExecutor()
        node = Node(id="g1", name="G1")
        result = await executor.execute(node, {})
        assert result.success is True
        assert result.output == ""

    @pytest.mark.asyncio
    async def test_command_phase_failure(self):
        executor = DispatchExecutor()
        node = Node(
            id="c1", name="C1",
            run=[_FALSE],
        )
        result = await executor.execute(node, {})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_missing_prompt_reports_filename(self, tmp_path):
        executor = DispatchExecutor(workdir=str(tmp_path))
        node = Node(id="p1", agent="missing.md")
        result = await executor.execute(node, {})
        assert result.success is False
        assert "missing.md" in result.error


# ---------------------------------------------------------------------------
# Gate retry re-execution
# ---------------------------------------------------------------------------


class TestGateRetryExecution:
    @pytest.mark.asyncio
    async def test_retry_re_executes_phase(self, tmp_path):
        """The counter advances once for each execution of the command eval."""
        counter = tmp_path / "counter.txt"
        counter.write_text("0")

        validator = tmp_path / "counting_validator.py"
        validator.write_text(
            "from pathlib import Path\n"
            f"counter = Path(r'{counter}')\n"
            "n = int(counter.read_text().strip())\n"
            "n += 1\n"
            "counter.write_text(str(n))\n"
            "print('needs another attempt'); raise SystemExit(n < 2)\n"
        )

        config = make_config([
            cmd_phase(
                "p1", output="data",
                eval={"run": [sys.executable, str(validator)]},
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_nodes"]
        assert int(counter.read_text().strip()) == 2


# ---------------------------------------------------------------------------
# Retry context injection
# ---------------------------------------------------------------------------


class TestRetryContextInjection:
    @pytest.mark.asyncio
    async def test_retry_injects_route_eval_preamble(self, tmp_path):
        """On retry, executor receives _route_eval_preamble in context."""
        counter = tmp_path / "counter.txt"
        counter.write_text("0")

        validator = tmp_path / "counting_validator.py"
        validator.write_text(
            "from pathlib import Path\n"
            f"counter = Path(r'{counter}')\n"
            "n = int(counter.read_text().strip())\n"
            "n += 1\n"
            "counter.write_text(str(n))\n"
            "print('needs another attempt'); raise SystemExit(n < 2)\n"
        )

        from mock_executor import MockExecutor
        from sqrlly.runtime.result import ExecutionResult

        mock = MockExecutor(results={
            "p1": ExecutionResult(success=True, output="data"),
        })
        config = make_config([
            {
                "id": "p1",
                "name": "P1",
                "agent": "t.md",
                "eval": {"run": [sys.executable, str(validator)]},
            },
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_nodes"]
        # Node was executed twice (initial + retry)
        assert mock.execution_order.count("p1") == 2
        # MockExecutor overwrites context per node ID, so last call wins —
        # the retry call should have _route_eval_preamble
        assert "_route_eval_preamble" in mock.received_contexts["p1"]

    @pytest.mark.asyncio
    async def test_retry_context_contains_measurement_and_attempt(self, tmp_path):
        """The retry context includes the previous command output and attempt."""
        counter = tmp_path / "counter.txt"
        counter.write_text("0")

        validator = tmp_path / "counting_validator.py"
        validator.write_text(
            "from pathlib import Path\n"
            f"counter = Path(r'{counter}')\n"
            "n = int(counter.read_text().strip())\n"
            "n += 1\n"
            "counter.write_text(str(n))\n"
            "print('needs another attempt'); raise SystemExit(n < 2)\n"
        )

        from mock_executor import MockExecutor
        from sqrlly.runtime.result import ExecutionResult

        mock = MockExecutor(results={
            "p1": ExecutionResult(success=True, output="data"),
        })
        config = make_config([
            {
                "id": "p1",
                "name": "P1",
                "agent": "t.md",
                "eval": {"run": [sys.executable, str(validator)]},
            },
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_nodes"]
        # MockExecutor overwrites context on each call, so last call has _route_eval_preamble
        ctx = mock.received_contexts["p1"]
        assert "_route_eval_preamble" in ctx
        assert "needs another attempt" in ctx["_route_eval_preamble"]
        assert "Attempt 1" in ctx["_route_eval_preamble"]
        # Neutral preamble — no "failed" framing.
        assert "failed" not in ctx["_route_eval_preamble"].lower()

    @pytest.mark.asyncio
    async def test_no_route_eval_preamble_on_first_attempt(self, tmp_path):
        """First execution should not have _route_eval_preamble in context."""
        script = tmp_path / "pass.py"
        script.write_text("print(1.0)")

        from mock_executor import MockExecutor
        from sqrlly.runtime.result import ExecutionResult

        mock = MockExecutor(results={
            "p1": ExecutionResult(success=True, output="data"),
        })
        config = make_config([
            {
                "id": "p1",
                "name": "P1",
                "agent": "t.md",
                "eval": {"run": [sys.executable, str(script)]},
            },
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_nodes"]
        assert "_route_eval_preamble" not in mock.received_contexts["p1"]


# ---------------------------------------------------------------------------
# Context propagation via MockExecutor
# ---------------------------------------------------------------------------


class TestContextPropagation:
    """Wiring tests — uses MockExecutor because the thing under test is
    context-flow between nodes, not executor semantics."""

    @pytest.mark.asyncio
    async def test_dependency_output_in_executor_context(self):
        from mock_executor import MockExecutor
        from sqrlly.runtime.result import ExecutionResult

        mock = MockExecutor(results={
            "a": ExecutionResult(success=True, output="a-output"),
        })
        config = make_config([
            {"id": "a", "name": "A", "agent": "t.md"},
            {"id": "b", "name": "B", "agent": "t.md", "depends_on": ["a"]},
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state())

        assert "a" in result["completed_nodes"]
        assert "b" in result["completed_nodes"]
        assert mock.received_contexts["b"]["a"] == "a-output"

    @pytest.mark.asyncio
    async def test_structured_output_flows_to_dependent(self):
        from mock_executor import MockExecutor
        from sqrlly.runtime.result import ExecutionResult

        mock = MockExecutor(results={
            "a": ExecutionResult(
                success=True, output="text",
                structured_output={"key": "val"},
            ),
        })
        config = make_config([
            {"id": "a", "name": "A", "agent": "t.md"},
            {"id": "b", "name": "B", "agent": "t.md", "depends_on": ["a"]},
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state())

        assert "b" in result["completed_nodes"]
        assert mock.received_contexts["b"]["a_structured"] == {"key": "val"}


# ---------------------------------------------------------------------------
# Output contract enforcement
# ---------------------------------------------------------------------------


class TestDimensionGateExecution:
    @pytest.mark.asyncio
    async def test_dimension_gate_passes_all_checks(self, tmp_path):
        """Script returns multi-dimension JSON; all dimensions pass thresholds."""
        validator = tmp_path / "dim_validator.py"
        validator.write_text(
            'import json\n'
            'print(json.dumps({"correctness": 0.9, "style": 0.7}))\n'
        )
        config = make_config([
            cmd_phase(
                "p1", output="data",
                eval={"run": [sys.executable, str(validator)]},
                route=[{"when": "eval.correctness < 0.8 or eval.style < 0.5", "then": "retry", "max_attempts": 3}],
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_nodes"]
        last_result = result["evaluations"]["p1"][-1]["result"]
        assert last_result["correctness"] == 0.9
        assert last_result["style"] == 0.7

    @pytest.mark.asyncio
    async def test_dimension_gate_fails_one_dimension(self, tmp_path):
        """One dimension below min → retry, then pass on second attempt."""
        counter = tmp_path / "counter.txt"
        counter.write_text("0")

        validator = tmp_path / "dim_validator.py"
        validator.write_text(
            'import json\n'
            'from pathlib import Path\n'
            f'counter = Path(r"{counter}")\n'
            'n = int(counter.read_text().strip())\n'
            'n += 1\n'
            'counter.write_text(str(n))\n'
            'style = 0.9 if n >= 2 else 0.3\n'
            'print(json.dumps({"correctness": 0.9, "style": style}))\n'
        )
        config = make_config([
            cmd_phase(
                "p1", output="data",
                eval={"run": [sys.executable, str(validator)]},
                route=[{"when": "eval.correctness < 0.8 or eval.style < 0.5", "then": "retry", "max_attempts": 3}],
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_nodes"]
        assert int(counter.read_text().strip()) == 2


class TestOutputs:
    @pytest.mark.asyncio
    async def test_contract_pass_continues_to_gate(self, tmp_path):
        """Files exist → gate runs → node completes."""
        out = tmp_path / "out"
        out.mkdir()
        (out / "result.txt").write_text("done")

        script = tmp_path / "pass.py"
        script.write_text("print(1.0)")

        config = make_config([
            cmd_phase(
                "p1", output="data",
                outputs={"files": ["out/result.txt"]},
                eval={"run": [sys.executable, str(script)]},
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["completed_nodes"]
        assert result["evaluations"]["p1"][-1]["result"]["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_contract_fail_blocks_phase(self, tmp_path):
        """Missing files → node fails, gate never runs."""
        config = make_config([
            cmd_phase(
                "p1", output="data",
                outputs={"files": ["out/missing.txt"]},
                eval={"run": [sys.executable, "never_called.py"]},
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["failed_nodes"]
        assert "p1" not in result.get("evaluations", {})
        assert any("contract violated" in e["error"].lower() for e in result["errors"])

    @pytest.mark.asyncio
    async def test_contract_fail_skips_dependent(self, tmp_path):
        """Contract failure cascades — downstream node also fails."""
        config = make_config([
            cmd_phase(
                "a", output="data",
                outputs={"files": ['out/missing.txt']},
            ),
            cmd_phase("b", output="ok", depends_on=["a"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "a" in result["failed_nodes"]
        assert "b" in result["failed_nodes"]

    @pytest.mark.asyncio
    async def test_contract_without_gate(self, tmp_path):
        """Contract checked even when no evaluation is defined."""
        config = make_config([
            cmd_phase(
                "p1", output="data",
                outputs={"files": ['out/missing.txt']},
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["failed_nodes"]

    @pytest.mark.asyncio
    async def test_contract_fail_no_retry(self, tmp_path):
        """Contract failure is a hard fail — does NOT trigger gate retry."""
        config = make_config([
            cmd_phase(
                "p1", output="data",
                outputs={"files": ["out/missing.txt"]},
                eval={"run": [sys.executable, "never_called.py"]},
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["failed_nodes"]
        assert result.get("retries", {}).get("p1", 0) == 0
