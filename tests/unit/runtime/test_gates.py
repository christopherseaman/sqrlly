"""Measurements expose command output and destination context."""

import sys

import pytest

from sqrlly.runtime.gates import build_eval_preamble, run_evaluation
from sqrlly.runtime.result import EvaluationBackendError
from sqrlly.schema.models import Node, Settings


@pytest.mark.parametrize("stdout", ['{"quality": 4, "exit_code": 0}', "[3]", "plain text"])
async def test_run_eval_exposes_text_and_exit_status(tmp_path, stdout):
    script = "import sys; print(sys.argv[1]); sys.exit(7)"
    node = Node(id="build", eval={"run": [sys.executable, "-c", script, stdout]})
    result = await run_evaluation(node, {}, str(tmp_path), Settings(), None)
    assert result["text"].strip() == stdout
    assert result["exit_code"] == 7
    if stdout.startswith("{"):
        assert result["quality"] == 4


async def test_eval_renders_argv_and_env_in_workdir(tmp_path):
    (tmp_path / "input").write_text("branch")
    script = "import os, pathlib, sys; print(pathlib.Path(sys.argv[1]).read_text() + os.environ['TAG'])"
    node = Node(id="build", eval={"run": {
        "cmd": [sys.executable, "-c", script, "{{file}}"], "env": {"TAG": "{{tag}}"},
    }})
    result = await run_evaluation(node, {"file": "input", "tag": "-abc"}, str(tmp_path), Settings(), None)
    assert result["text"].strip() == "branch-abc"


async def test_eval_timeout_distinguishes_missing_measurement(tmp_path):
    node = Node(id="build", eval={"run": [sys.executable, "-c", "import time; time.sleep(10)"], "timeout": 0.05})
    with pytest.raises(EvaluationBackendError, match="timed out"):
        await run_evaluation(node, {}, str(tmp_path), Settings(), None)


def test_context_controls_destination_feedback():
    history = [{"attempt": i, "result": {"detail": f"result-{i}"}} for i in (1, 2)]
    last = build_eval_preamble("last", history, 2, {})
    assert "result-1" not in last
    assert "result-2" in last and "2" in last
    all_records = build_eval_preamble("all", history, 2, {})
    assert all_records.index("result-1") < all_records.index("result-2")
    assert build_eval_preamble("none", history, 2, {}) == ""
    template = "{{ label }}:{{ eval.detail }}:{{ attempt }}"
    assert build_eval_preamble(template, history, 2, {"label": "check"}) == "check:result-2:2"


@pytest.mark.parametrize("kind", ["agent", "llm"])
async def test_prompt_measurement_exposes_score_and_explanation(tmp_path, monkeypatch, kind):
    from sqrlly.runtime.executor import dispatch
    from sqrlly.runtime.executor.backends.cli import CLIBackend

    # head returns the rendered first prompt line through the real subprocess backend.
    backend = CLIBackend(("head", "-n", "1"))
    monkeypatch.setattr(dispatch, "create_backend", lambda *args, **kwargs: backend)
    node = Node(id="build", eval={kind: {"text": '{"score": {{ quality }}, "explanation": "tested"}'}},
                route=[{"when": "eval.score < 0.5", "then": "fail"}])
    result = await run_evaluation(node, {"quality": 0.75}, str(tmp_path), Settings(), None)
    assert result["score"] == 0.75
    assert result["explanation"] == "tested"
    assert '"score": 0.75' in result["text"]
