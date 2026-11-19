"""Standalone command eval reads its dependency through an argv template."""
from __future__ import annotations

import sys

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state

from helpers import make_config


_PYTHON = sys.executable


class TestGateOnlyPhaseSeesUpstream:
    @pytest.mark.asyncio
    async def test_gate_only_phase_validates_upstream_via_argv(
        self, tmp_path,
    ):
        """`produce` writes a marker string; `check` is gate-only and
        validates it via argv without touching disk."""
        validator = tmp_path / "validate.py"
        validator.write_text("import sys; assert sys.argv[1] == 'MARKER:wired-up content here'")

        config = make_config([
            {
                "id": "produce",
                "name": "Produce",
                "run": ["/usr/bin/echo", "-n", "MARKER:wired-up content here"],
            },
            {
                "id": "check",
                "name": "Check upstream content",
                "depends_on": ["produce"],
                "eval": {"run": [sys.executable, str(validator), "{{produce}}"]},
            },
        ])

        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(
            make_initial_state(workdir=str(tmp_path)),
        )

        # Both nodes complete; the gate validator passed because it
        # could see the upstream content.
        assert "produce" in result["completed_nodes"]
        assert "check" in result["completed_nodes"]
        assert "check" not in result["failed_nodes"]
