"""Run the shipped jokes workflow against the real Codex CLI."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from sqrlly.cli.main import cli

_REPO_ROOT = Path(__file__).resolve().parents[2]
_JOKES_SRC = _REPO_ROOT / "examples" / "jokes"


@pytest.mark.live
@pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="`codex` CLI not on PATH",
)
def test_jokes_roundtrip_cli_transport(tmp_path):
    result = CliRunner().invoke(
        cli,
        ["run", str(_JOKES_SRC / "workflow.yaml"), "--workdir", str(tmp_path)],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, (
        f"cli run failed (exit {result.exit_code}). "
        f"Output:\n{result.output}"
    )
    assert "Completed: 2 nodes" in result.output, (
        f"Expected 2 nodes (generate + select); got:\n{result.output}"
    )
    assert "generate" in result.output
    assert "select" in result.output
