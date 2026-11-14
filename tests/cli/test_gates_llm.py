"""Live LLM evaluation gate through the CLI backend.

The LLM gate path in runtime/gates.py::run_evaluation_llm dispatches an .md
template through the node's PromptBackend, so it runs here against a real
``codex exec`` subprocess.
"""
import asyncio

import pytest

from sqrlly.runtime.executor.backends.factory import create_backend_from_preset
from sqrlly.runtime.gates import run_evaluation_llm
from sqrlly.schema.models import Evaluation, LlmPreset

CLI_TIMEOUT = 180
MODEL = "gpt-5.6-luna"


@pytest.mark.cli
class TestEvaluateGateLLMPositivePath:
    @pytest.mark.asyncio
    async def test_high_score_from_real_llm(self, tmp_path):
        """A clear instruction to return {"score": 1.0, ...} yields a passing score.

        Pins the happy path: .md template rendering → backend dispatch →
        JSON parsing → EvaluationResult population.
        """
        gate_file = tmp_path / "llm_gate.md"
        gate_file.write_text(
            "You are a test validator. The node ran and produced this output:\n\n"
            "---\n"
            "{{output}}\n"
            "---\n\n"
            "Respond with EXACTLY this JSON object and nothing else — no prose, "
            'no markdown fences:\n\n'
            '{"score": 1.0, "feedback": "ok", "pass_criteria_met": ["ran"], '
            '"pass_criteria_unmet": []}\n'
        )

        evaluation = Evaluation(validator="llm_gate.md", threshold=0.8)
        backend = create_backend_from_preset(LlmPreset(model=MODEL))
        try:
            async with asyncio.timeout(CLI_TIMEOUT):
                result = await run_evaluation_llm(
                    evaluation=evaluation,
                    node_id="test_phase",
                    workdir=str(tmp_path),
                    node_output="some node output",
                    backend=backend,
                    default_model=MODEL,
                )
            assert result.score >= 0.8, (
                f"expected passing score from LLM gate; got {result.score} "
                f"with feedback={result.feedback!r}"
            )
            assert result.feedback == "ok"
        finally:
            await backend.close()
