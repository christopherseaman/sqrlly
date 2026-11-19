"""Fixture-based unit tests for extracted node helpers in compile/nodes.py.

Known-good AND known-bad pairs for each helper, with @pytest.mark.parametrize
for routing tables where applicable.
"""

import asyncio

import pytest

from sqrlly.compile.nodes import (
    assemble_success_update,
    build_context,
    check_dep_failed,
    check_dry_run,
    run_with_timeout,
    make_failure_update,
)
from sqrlly.runtime.executor.prompt import retry_delay
from sqrlly.runtime.result import ExecutionResult
from sqrlly.schema.models import Node


def _phase(id="p1", depends_on=None, **kw):
    return Node(id=id, name=id, depends_on=depends_on or [], **kw)


class TestGetRetryDelay:
    def test_empty_backoff(self):
        assert retry_delay(1, []) == 0.0

    def test_first_attempt(self):
        assert retry_delay(1, [10, 30, 60]) == 10

    def test_second_attempt(self):
        assert retry_delay(2, [10, 30, 60]) == 30

    def test_clamps_to_last(self):
        assert retry_delay(5, [10, 30, 60]) == 60

    def test_single_value(self):
        assert retry_delay(3, [5]) == 5


class TestCheckDepFailed:
    def test_dependency_failed(self):
        node = _phase(depends_on=["dep1"])
        state = {"failed_nodes": {"dep1"}}
        result = check_dep_failed(node, state)
        assert result["failed_nodes"] == {"p1"}
        assert "dependency 'dep1' failed" in result["errors"][0]["error"]

    def test_no_failed_deps(self):
        node = _phase(depends_on=["dep1"])
        state = {"failed_nodes": set()}
        assert check_dep_failed(node, state) is None

    def test_no_deps(self):
        node = _phase(depends_on=[])
        state = {"failed_nodes": {"something"}}
        assert check_dep_failed(node, state) is None

    def test_unrelated_failure(self):
        node = _phase(depends_on=["dep1"])
        state = {"failed_nodes": {"dep2"}}
        assert check_dep_failed(node, state) is None


class TestCheckDryRun:
    def test_dry_run_without_gate(self):
        state = {"dry_run": True}
        result = check_dry_run(_phase(), state)
        assert result["completed_nodes"] == {"p1"}
        assert "[dry-run]" in result["node_outputs"]["p1"]
        assert "gate_scores" not in result

    def test_dry_run_with_gate(self):
        """Gated nodes in dry-run: node node writes node_outputs only; the
        downstream Evaluation node synthesizes a pass EvaluationRecord."""
        node = _phase(eval={"run": ["true"]})
        state = {"dry_run": True}
        result = check_dry_run(node, state)
        assert "[dry-run]" in result["node_outputs"]["p1"]
        assert "completed_nodes" not in result

    def test_not_dry_run(self):
        assert check_dry_run(_phase(), {"dry_run": False}) is None

    def test_missing_dry_run_key(self):
        assert check_dry_run(_phase(), {}) is None



class TestBuildContext:
    def test_with_matching_deps(self):
        node = _phase(depends_on=["a"])
        state = {
            "node_outputs": {"a": "out-a"},
            "node_structured_outputs": {"a": {"k": "v"}},
        }
        ctx = build_context(node, state)
        # `evals` is always-bound (Stage 5c) — empty dict when no
        # evaluations have run.
        assert str(ctx["a"]) == "out-a"
        assert ctx["a"]["k"] == "v"
        assert ctx["a_structured"] == {"k": "v"}
        assert ctx["evals"] == {}

    def test_no_matching_deps(self):
        node = _phase(depends_on=["b"])
        state = {"node_outputs": {"a": "out-a"}}
        assert build_context(node, state) == {"evals": {}}

    def test_empty_deps(self):
        assert build_context(_phase(), {"node_outputs": {"a": "x"}}) == {"evals": {}}

    def test_output_only_no_structured(self):
        node = _phase(depends_on=["a"])
        state = {"node_outputs": {"a": "text"}}
        assert build_context(node, state) == {"a": "text", "evals": {}}

    def test_projects_dep_worktree_path(self):
        """`{{dep_worktree}}` is populated from state.node_worktrees."""
        node = _phase(depends_on=["a"])
        state = {
            "node_outputs": {"a": "out"},
            "node_worktrees": {"a": "/tmp/wt-a"},
        }
        ctx = build_context(node, state)
        assert ctx["a_worktree"] == "/tmp/wt-a"

    def test_no_worktree_means_no_worktree_key(self):
        node = _phase(depends_on=["a"])
        state = {"node_outputs": {"a": "out"}, "node_worktrees": {}}
        ctx = build_context(node, state)
        assert "a_worktree" not in ctx

    # -- subgraph outputs: projection (dotted keys) ----------------------

    def test_subgraph_dotted_outputs_bound_as_flat_vars(self):
        """When a dep's `outputs:` projects values to `dep.key`, build_context
        binds them under `{dep}_{key}` so templates can reach them without
        dotted-attribute syntax (Jinja can't dot-into a string).

        Stage 4c: a parent declaring `outputs: { reconcile: "{{reconcile}}" }`
        on the subgraph node lets a downstream node template `{{paper_reconcile}}`.
        """
        node = _phase(depends_on=["paper"])
        state = {
            "node_outputs": {
                "paper": "terminal-out",
                "paper.reconcile": "the-paper-text",
            },
        }
        ctx = build_context(node, state)
        assert ctx["paper"] == "terminal-out"
        assert ctx["paper_reconcile"] == "the-paper-text"
        # Bare-key form is intentionally NOT bound — silent collision risk
        # when two deps both project the same key. Use the dep-prefixed form.
        assert "reconcile" not in ctx

    def test_dotted_outputs_no_bare_key_collision(self):
        """Two deps each projecting the same suffix — both per-dep keys are
        bound; the bare key is unbound (no silent collision)."""
        node = _phase(depends_on=["a", "b"])
        state = {
            "node_outputs": {
                "a": "a-out", "b": "b-out",
                "a.summary": "from-a",
                "b.summary": "from-b",
            },
        }
        ctx = build_context(node, state)
        assert ctx["a_summary"] == "from-a"
        assert ctx["b_summary"] == "from-b"
        # No silent first-dep-wins binding for bare `summary`.
        assert "summary" not in ctx

    def test_projects_branch_aggregations(self):
        """Downstream context synthesizes `{dep}_branches` + worktrees from state.

        Stage 2b moved aggregation out of the final-node wrapper; any
        node depending on a dynamic parent now sees the same aggregate
        via build_context's state projection.
        """
        node = _phase(depends_on=["parent"])
        state = {
            "node_outputs": {"parent": "parent-out"},
            "child_outputs": {"parent::a": "x", "parent::b": "y"},
            "node_worktrees": {
                "parent::a": "/tmp/wt-a",
                "parent::b": "/tmp/wt-b",
            },
        }
        ctx = build_context(node, state)
        import json as _json
        assert _json.loads(ctx["parent_branches"]) == {
            "parent::a": "x",
            "parent::b": "y",
        }
        assert sorted(_json.loads(ctx["parent_branch_worktrees"])) == [
            "/tmp/wt-a",
            "/tmp/wt-b",
        ]

    def test_no_branch_aggregations_when_absent(self):
        """Nodes with no fan-out parent in deps get no aggregate keys."""
        node = _phase(depends_on=["normal"])
        state = {
            "node_outputs": {"normal": "out"},
            "child_outputs": {},
            "node_worktrees": {},
        }
        ctx = build_context(node, state)
        assert "normal_branches" not in ctx
        assert "normal_branch_worktrees" not in ctx
        assert "normal_branch_map" not in ctx

    def test_branch_map_keyed_by_child_id(self):
        """`{{p_branch_map}}` is a JSON dict keyed by child id, each entry
        holding both output and worktree — the AP4 fix that eliminates
        positional-order coupling between _branches and _branch_worktrees.
        """
        import json
        node = _phase(depends_on=["p"])
        state = {
            "node_outputs": {"p": "parent-out"},
            "child_outputs": {"p::0": "out0", "p::1": "out1"},
            "node_worktrees": {"p::0": "/wt/p0", "p::1": "/wt/p1"},
        }
        ctx = build_context(node, state)
        bmap = json.loads(ctx["p_branch_map"])
        assert bmap == {
            "p::0": {"output": "out0", "worktree": "/wt/p0"},
            "p::1": {"output": "out1", "worktree": "/wt/p1"},
        }

    def test_branch_map_missing_worktree_is_none(self):
        """When a child has no recorded worktree, branch_map entry has worktree=None."""
        import json
        node = _phase(depends_on=["p"])
        state = {
            "node_outputs": {"p": "parent-out"},
            "child_outputs": {"p::0": "out0", "p::1": "out1"},
            "node_worktrees": {"p::0": "/wt/p0"},  # p::1 absent
        }
        ctx = build_context(node, state)
        bmap = json.loads(ctx["p_branch_map"])
        assert bmap["p::0"] == {"output": "out0", "worktree": "/wt/p0"}
        assert bmap["p::1"] == {"output": "out1", "worktree": None}

    # -- aggregate _deps / _dep_worktrees ---------------------------------

    def test_single_dep_no_aggregate(self):
        """Node with exactly one dep does NOT get _deps — it's unnecessary."""
        node = _phase(depends_on=["a"])
        state = {"node_outputs": {"a": "out-a"}}
        ctx = build_context(node, state)
        assert "_deps" not in ctx
        assert "_dep_worktrees" not in ctx

    def test_multi_dep_aggregate_outputs(self):
        """Node with 2+ deps gets _deps mapping dep_id → output."""
        import json as _json
        node = _phase(depends_on=["a", "b"])
        state = {"node_outputs": {"a": "out-a", "b": "out-b"}}
        ctx = build_context(node, state)
        assert "_deps" in ctx
        assert _json.loads(ctx["_deps"]) == {"a": "out-a", "b": "out-b"}

    def test_multi_dep_aggregate_worktrees(self):
        """_dep_worktrees is present when some deps have worktrees."""
        import json as _json
        node = _phase(depends_on=["a", "b"])
        state = {
            "node_outputs": {"a": "out-a", "b": "out-b"},
            "node_worktrees": {"a": "/tmp/wt-a", "b": "/tmp/wt-b"},
        }
        ctx = build_context(node, state)
        assert "_dep_worktrees" in ctx
        assert _json.loads(ctx["_dep_worktrees"]) == {
            "a": "/tmp/wt-a",
            "b": "/tmp/wt-b",
        }

    def test_multi_dep_no_worktrees_omits_key(self):
        """_dep_worktrees is absent when no deps have worktrees."""
        node = _phase(depends_on=["a", "b"])
        state = {
            "node_outputs": {"a": "out-a", "b": "out-b"},
            "node_worktrees": {},
        }
        ctx = build_context(node, state)
        assert "_deps" in ctx
        assert "_dep_worktrees" not in ctx

    def test_multi_dep_json_roundtrip(self):
        """_deps value is valid JSON."""
        import json as _json
        node = _phase(depends_on=["a", "b"])
        state = {"node_outputs": {"a": "hello", "b": "world"}}
        ctx = build_context(node, state)
        parsed = _json.loads(ctx["_deps"])
        assert isinstance(parsed, dict)
        assert parsed["a"] == "hello"


class TestRunWithTimeout:
    @pytest.mark.asyncio
    async def test_successful_execution(self):
        async def _coro():
            return ExecutionResult(output="done")

        result = await run_with_timeout(_coro(), None)
        assert isinstance(result, ExecutionResult)
        assert result.output == "done"

    @pytest.mark.asyncio
    async def test_with_timeout_succeeds(self):
        async def _coro():
            return ExecutionResult(output="fast")

        result = await run_with_timeout(_coro(), 5.0)
        assert result.output == "fast"

    @pytest.mark.asyncio
    async def test_timeout_returns_failure_result(self):
        async def _slow():
            await asyncio.sleep(10)
            return ExecutionResult(output="never")

        result = await run_with_timeout(_slow(), 0.01)
        assert result.success is False
        assert "timed out after 0.01s" in result.error


class TestMakeFailureUpdate:
    def test_structure_defaults_kind_to_node_error(self):
        result = make_failure_update("p1", "something broke")
        assert result == {
            "failed_nodes": {"p1"},
            "errors": [
                {"node": "p1", "error": "something broke", "kind": "node_error"},
            ],
        }

    def test_explicit_kind_is_carried(self):
        result = make_failure_update("p1", "overloaded", kind="overload")
        assert result["errors"][0]["kind"] == "overload"


class TestAssembleSuccessUpdate:
    def test_basic_output(self):
        result = ExecutionResult(output="hello")
        update = assemble_success_update(_phase(), result)
        assert update == {"node_outputs": {"p1": "hello"}}

    def test_with_structured_output(self):
        result = ExecutionResult(output="x", structured_output={"key": "val"})
        update = assemble_success_update(_phase(), result)
        assert update["node_structured_outputs"] == {"p1": {"key": "val"}}

    def test_none_structured_excluded(self):
        result = ExecutionResult(output="x", structured_output=None)
        update = assemble_success_update(_phase(), result)
        assert "node_structured_outputs" not in update
