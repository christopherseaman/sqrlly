"""Tests for the prompt module (Stage 5b API) and template rendering.

Stage 5b removed the legacy ``PromptExecutor.execute(node, context)``
method. The module now exposes a narrow set of free functions used by
``DispatchExecutor._dispatch_prompt``:

  - ``apply_preamble(template, *, settings, base_workdir) -> str``
  - ``execute_with_downgrade(backend, rendered, model, workdir, timeout)
    -> ExecutionResult``

Module-level helpers ``downgrade_model``,
``render_template`` are also covered here. End-to-end prompt-execution
flow (node → prompt file fetch → render → backend) lives behind
``DispatchExecutor`` and is exercised here through that entry point.
"""

import time

import pytest

from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.executor.prompt import (
    apply_preamble,
    execute_with_downgrade,
    retry_delay,
    downgrade_model,
    prepend_eval_preamble,
    render_template,
)
from sqrlly.runtime.result import (
    ExecutionResult,
    OverloadError,
    PromptBackend,
)
from sqrlly.schema.models import Settings




# ---------------------------------------------------------------------------
# render_template
# ---------------------------------------------------------------------------


class TestRenderTemplate:
    def test_substitutes_known_variables(self):
        result = render_template("Hello {{name}}", {"name": "world"})
        assert result == "Hello world"

    def test_unknown_variables_render_empty(self):
        """Jinja2 default Undefined renders missing vars as empty string."""
        result = render_template("Hello {{name}}", {})
        assert result == "Hello "

    def test_multiple_variables(self):
        result = render_template(
            "{{greeting}} {{name}}!", {"greeting": "Hi", "name": "Claude"}
        )
        assert result == "Hi Claude!"

    def test_spaces_in_braces(self):
        result = render_template("{{ name }}", {"name": "world"})
        assert result == "world"

    def test_no_placeholders(self):
        result = render_template("plain text", {"name": "world"})
        assert result == "plain text"

    def test_repeated_variable(self):
        result = render_template("{{x}} and {{x}}", {"x": "yes"})
        assert result == "yes and yes"

    def test_non_string_value_converted(self):
        result = render_template("count={{n}}", {"n": 42})
        assert result == "count=42"

    def test_value_containing_braces(self):
        """Substituted value containing {{ }} must not trigger double-substitution."""
        result = render_template("{{x}}", {"x": "{{y}}"})
        assert result == "{{y}}"

    def test_hyphenated_node_id_errors(self):
        """Jinja2 parses {{research-node}} as subtraction (research minus node).
        This is a known limitation — prompt templates must use underscores for
        node IDs that need substitution. Documented in CLAUDE.md."""
        from jinja2 import UndefinedError

        with pytest.raises((UndefinedError, TypeError)):
            render_template("{{research-node}}", {})


# ---------------------------------------------------------------------------
# prepend_eval_preamble — Stage 5c auto-prepend for inline-route gotos
# ---------------------------------------------------------------------------


class TestPrependEvalPreamble:
    """The pure helper called by `_dispatch_prompt` after Jinja render.
    Tests the helper directly so the auto-prepend behavior is covered
    without instrumenting the PromptBackend boundary.
    """

    def test_no_preamble_returns_rendered_unchanged(self):
        assert prepend_eval_preamble("body", {}) == "body"

    def test_empty_preamble_returns_rendered_unchanged(self):
        assert prepend_eval_preamble("body", {"_route_eval_preamble": ""}) == "body"

    def test_non_empty_preamble_prepends_with_blank_line(self):
        result = prepend_eval_preamble(
            "Rewrite from classify.",
            {"_route_eval_preamble": "Previous evaluation: score=0.42, threshold=0.8."},
        )
        assert result == (
            "Previous evaluation: score=0.42, threshold=0.8."
            "\n\nRewrite from classify."
        )

    def test_preserves_rendered_body_verbatim(self):
        body = "Multi-line\nbody\nwith trailing newline\n"
        result = prepend_eval_preamble(
            body, {"_route_eval_preamble": "P"},
        )
        assert result.endswith(body)



# ---------------------------------------------------------------------------
# In-test backend doubles (Protocol implementations, not unittest.mock)
# ---------------------------------------------------------------------------


class MemoryBackend:
    """Test backend that records calls and returns configurable responses."""

    def __init__(
        self,
        response: str = "backend-output",
        structured: dict | None = None,
    ):
        self._response = response
        self._structured = structured
        self.calls: list[tuple[str, str, str, float | None]] = []

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        self.calls.append((prompt, model, workdir, timeout))
        return ExecutionResult(
            output=self._response,
            structured_output=self._structured,
        )

    async def close(self) -> None:
        pass


class ErrorBackend:
    """Backend that always raises."""

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        raise RuntimeError("connection failed")

    async def close(self) -> None:
        pass


class TimeoutBackend:
    """Backend whose send_prompt hits its own (params.timeout) deadline."""

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        import asyncio
        raise asyncio.TimeoutError()

    async def close(self) -> None:
        pass


class OverloadThenSucceedBackend:
    """Raises OverloadError until ``fail_count`` is reached, then succeeds.

    Records every send_prompt call (including the overloaded ones) so
    the test can assert on the model sequence the executor walked.
    """

    def __init__(self, fail_count: int = 1, response: str = "ok"):
        self._fail_count = fail_count
        self._response = response
        self.calls: list[tuple[str, str, str, float | None]] = []

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        self.calls.append((prompt, model, workdir, timeout))
        if len(self.calls) <= self._fail_count:
            raise OverloadError("overloaded")
        return ExecutionResult(output=self._response)

    async def close(self) -> None:
        pass


class AlwaysOverloadBackend:
    """Raises OverloadError every call — exhausts the downgrade chain."""

    def __init__(self):
        self.calls: list[tuple[str, str, str, float | None]] = []

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        self.calls.append((prompt, model, workdir, timeout))
        raise OverloadError("overloaded")

    async def close(self) -> None:
        pass


class TransientThenSucceedBackend:
    """Raises a non-OverloadError (a plain RuntimeError, like the CLI
    backend's `claude exited 1`) on the first ``fail_count`` calls, then
    succeeds. Records every call so the test can count attempts. This is
    a deterministic scripted-exit double for transient-error simulation —
    sanctioned instrumentation, not a fake of the real network behavior."""

    def __init__(self, fail_count: int = 1, response: str = "recovered"):
        self._fail_count = fail_count
        self._response = response
        self.calls: list[tuple[str, str, str, float | None]] = []

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        self.calls.append((prompt, model, workdir, timeout))
        if len(self.calls) <= self._fail_count:
            raise RuntimeError(f"claude exited 1: transient blip {len(self.calls)}")
        return ExecutionResult(output=self._response)

    async def close(self) -> None:
        pass


class AlwaysTransientErrorBackend:
    """Raises a non-OverloadError on every call. Counts calls."""

    def __init__(self):
        self.calls: list[tuple[str, str, str, float | None]] = []

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        self.calls.append((prompt, model, workdir, timeout))
        raise RuntimeError("claude exited 1: persistent failure")

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# apply_preamble
# ---------------------------------------------------------------------------


class TestApplyPreamble:
    def test_no_preamble_returns_template_unchanged(self, tmp_path):
        result = apply_preamble(
            "body", settings=Settings(), base_workdir=str(tmp_path),
        )
        assert result == "body"

    def test_preamble_prepended_with_separator(self, tmp_path):
        (tmp_path / "preamble.md").write_text("SHARED CONTEXT")
        result = apply_preamble(
            "Do the thing",
            settings=Settings(preamble_file="preamble.md"),
            base_workdir=str(tmp_path),
        )
        assert result == "SHARED CONTEXT\n\nDo the thing"

    def test_missing_preamble_raises(self, tmp_path):
        # apply_preamble surfaces missing-file as a FileNotFoundError;
        # _dispatch_prompt translates to ExecutionResult(success=False)
        # at the boundary so the rest of the runtime sees a single
        # return type from apply_preamble.
        with pytest.raises(FileNotFoundError):
            apply_preamble(
                "body",
                settings=Settings(preamble_file="missing.md"),
                base_workdir=str(tmp_path),
            )

    def test_preamble_resolved_from_base_workdir(self, tmp_path):
        """Preamble lives with the config (base_workdir), not any
        per-call worktree. base_workdir is the DispatchExecutor's base."""
        base = tmp_path / "base"
        base.mkdir()
        (base / "preamble.md").write_text("BASE PREAMBLE")
        result = apply_preamble(
            "X",
            settings=Settings(preamble_file="preamble.md"),
            base_workdir=str(base),
        )
        assert result == "BASE PREAMBLE\n\nX"


# ---------------------------------------------------------------------------
# execute_with_downgrade
# ---------------------------------------------------------------------------


class TestExecuteRendered:
    @pytest.mark.asyncio
    async def test_sends_rendered_prompt_to_backend(self, tmp_path):
        backend = MemoryBackend()
        result = await execute_with_downgrade(
            backend, "rendered prompt", "sonnet", str(tmp_path), timeout=None,
            settings=Settings(),
        )
        assert result.success is True
        assert result.output == "backend-output"
        assert backend.calls == [
            ("rendered prompt", "sonnet", str(tmp_path), None)
        ]

    @pytest.mark.asyncio
    async def test_model_argument_passed_through(self, tmp_path):
        backend = MemoryBackend()
        await execute_with_downgrade(
            backend, "x", "opus", str(tmp_path), timeout=None,
            settings=Settings(),
        )
        assert backend.calls[0][1] == "opus"

    @pytest.mark.asyncio
    async def test_timeout_threaded_to_backend(self, tmp_path):
        backend = MemoryBackend()
        await execute_with_downgrade(
            backend, "x", "sonnet", str(tmp_path), timeout=15.5,
            settings=Settings(),
        )
        assert backend.calls[0][3] == 15.5

    @pytest.mark.asyncio
    async def test_timeout_none_passed_through(self, tmp_path):
        backend = MemoryBackend()
        await execute_with_downgrade(
            backend, "x", "sonnet", str(tmp_path), timeout=None,
            settings=Settings(),
        )
        assert backend.calls[0][3] is None

    @pytest.mark.asyncio
    async def test_per_call_workdir_forwarded_to_backend(self, tmp_path):
        """The workdir passed to execute_with_downgrade is the one the
        backend sees."""
        backend = MemoryBackend()
        await execute_with_downgrade(
            backend, "x", "sonnet", str(tmp_path), timeout=None,
            settings=Settings(),
        )
        assert backend.calls[0][2] == str(tmp_path)

    @pytest.mark.asyncio
    async def test_backend_error_returns_failure(self, tmp_path):
        result = await execute_with_downgrade(
            ErrorBackend(), "x", "sonnet", str(tmp_path), timeout=None,
            settings=Settings(),
        )
        assert result.success is False
        assert "connection failed" in result.error
        assert result.error_kind == "backend_error"

    @pytest.mark.asyncio
    async def test_backend_timeout_is_kind_timeout_not_backend_error(self, tmp_path):
        """A backend-level (params.timeout) TimeoutError classifies as
        `timeout`, not `backend_error` — matching the SCHEMA definition that
        `timeout` covers a node-execution deadline."""
        result = await execute_with_downgrade(
            TimeoutBackend(), "x", "sonnet", str(tmp_path), timeout=0.01,
            settings=Settings(),
        )
        assert result.success is False
        assert result.error_kind == "timeout"

    @pytest.mark.asyncio
    async def test_structured_output_passed_through(self, tmp_path):
        backend = MemoryBackend(response="text", structured={"key": "value"})
        result = await execute_with_downgrade(
            backend, "x", "sonnet", str(tmp_path), timeout=None,
            settings=Settings(),
        )
        assert result.success is True
        assert result.structured_output == {"key": "value"}


# ---------------------------------------------------------------------------
# Overload → downgrade fallback (covers execute_with_downgrade's retry loop)
# ---------------------------------------------------------------------------


class TestOverloadDowngrade:
    @pytest.mark.asyncio
    async def test_overload_downgrades_to_next_model(self, tmp_path):
        """First call raises OverloadError; executor downgrades and retries
        with the next model from the chain."""
        backend = OverloadThenSucceedBackend(fail_count=1, response="recovered")
        result = await execute_with_downgrade(
            backend, "x", "opus", str(tmp_path), timeout=None,
            settings=Settings(), max_retries=0,
        )
        assert result.success is True
        assert result.output == "recovered"
        # Two calls — first with opus, second with sonnet.
        assert [c[1] for c in backend.calls] == ["opus", "sonnet"]

    @pytest.mark.asyncio
    async def test_overload_walks_full_chain(self, tmp_path):
        """Overload at every step walks the entire chain before giving up."""
        backend = OverloadThenSucceedBackend(fail_count=2, response="haiku-ok")
        result = await execute_with_downgrade(
            backend, "x", "opus", str(tmp_path), timeout=None,
            settings=Settings(), max_retries=0,
        )
        assert result.success is True
        assert result.output == "haiku-ok"
        assert [c[1] for c in backend.calls] == ["opus", "sonnet", "haiku"]

    @pytest.mark.asyncio
    async def test_overload_exhausted_returns_failure(self, tmp_path):
        """OverloadError at every step exhausts chain → failure result
        naming the last model attempted."""
        backend = AlwaysOverloadBackend()
        result = await execute_with_downgrade(
            backend, "x", "opus", str(tmp_path), timeout=None,
            settings=Settings(), max_retries=0,
        )
        assert result.success is False
        assert "exhausted model chain" in result.error
        assert "haiku" in result.error
        assert result.error_kind == "overload"
        assert [c[1] for c in backend.calls] == ["opus", "sonnet", "haiku"]

    @pytest.mark.asyncio
    async def test_unknown_starting_model_fails_immediately(self, tmp_path):
        """A starting model not in the chain → downgrade returns None →
        the executor cannot retry, surfaces the failure."""
        backend = AlwaysOverloadBackend()
        result = await execute_with_downgrade(
            backend, "x", "gpt-4", str(tmp_path), timeout=None,
            settings=Settings(), max_retries=0,
        )
        assert result.success is False
        assert "exhausted model chain" in result.error
        assert "gpt-4" in result.error
        # Backend was called exactly once with the unknown model.
        assert len(backend.calls) == 1
        assert backend.calls[0][1] == "gpt-4"


# ---------------------------------------------------------------------------
# downgrade_model
# ---------------------------------------------------------------------------


class TestDowngradeModel:
    CHAIN = ["opus", "sonnet", "haiku"]

    def test_downgrade_opus_to_sonnet(self):
        assert downgrade_model("opus", self.CHAIN) == "sonnet"

    def test_downgrade_sonnet_to_haiku(self):
        assert downgrade_model("sonnet", self.CHAIN) == "haiku"

    def test_downgrade_haiku_returns_none(self):
        assert downgrade_model("haiku", self.CHAIN) is None

    def test_downgrade_unknown_model_returns_none(self):
        assert downgrade_model("gpt-4", self.CHAIN) is None

    def test_custom_chain(self):
        assert downgrade_model("a", ["a", "b", "c"]) == "b"
        assert downgrade_model("c", ["a", "b", "c"]) is None


# ---------------------------------------------------------------------------
# retry_delay — delay-indexing and clamp logic
# ---------------------------------------------------------------------------


class TestBackendRetryDelay:
    @pytest.mark.parametrize("attempt,backoff,expected", [
        (1, [], 0.0),                    # empty list → always 0.0
        (1, [1.0, 2.0], 1.0),           # attempt 0-indexed as attempt-1=0 → first slot
        (2, [1.0, 2.0], 2.0),           # second slot
        (3, [1.0, 2.0], 2.0),           # past end → clamps to last
        (1, [0.5], 0.5),                # single element
        (99, [0.5], 0.5),               # single element clamps at any attempt
    ])
    def test_delay_cases(self, attempt, backoff, expected):
        assert retry_delay(attempt, backoff) == expected


class TestMemoryBackendProtocol:
    def test_satisfies_protocol(self):
        assert isinstance(MemoryBackend(), PromptBackend)


# ---------------------------------------------------------------------------
# End-to-end prompt flow through DispatchExecutor (Stage 5b entry point)
#
# In Stage 5b, node → prompt-file → render → backend orchestration moved
# from PromptExecutor.execute() into DispatchExecutor._dispatch_prompt().
# These tests cover the full pipeline at that entry point.
# ---------------------------------------------------------------------------


class TestBackendTransientRetry:
    @pytest.mark.asyncio
    async def test_zero_retries_is_terminal_today_behavior(self, tmp_path):
        """defaults={"llm": {"backend_max_retries": 0}} (default): a transient error is terminal,
        one attempt — exactly today's behavior."""
        backend = AlwaysTransientErrorBackend()
        result = await execute_with_downgrade(
            backend, "x", "sonnet", str(tmp_path), timeout=None,
            settings=Settings(defaults={"llm": {"backend_max_retries": 0}}),
        )
        assert result.success is False
        assert "Backend error" in result.error
        assert "claude exited 1" in result.error
        assert len(backend.calls) == 1  # no retry

    @pytest.mark.asyncio
    async def test_transient_error_retried_then_succeeds(self, tmp_path):
        """Two transient failures then success, with budget 3 → 3 calls,
        final success."""
        backend = TransientThenSucceedBackend(fail_count=2, response="recovered")
        result = await execute_with_downgrade(
            backend, "x", "sonnet", str(tmp_path), timeout=None,
            settings=Settings(defaults={"llm": {"backend_max_retries": 3}}),
        )
        assert result.success is True
        assert result.output == "recovered"
        # 2 failed + 1 success = 3 calls.
        assert len(backend.calls) == 3
        # Each attempt re-sent the ORIGINAL model (no downgrade — transient
        # errors are not overload).
        assert [c[1] for c in backend.calls] == ["sonnet", "sonnet", "sonnet"]

    @pytest.mark.asyncio
    async def test_budget_exhausted_returns_failure(self, tmp_path):
        """Persistent transient error with budget 2 → 1 initial + 2 retries
        = 3 calls, then terminal failure."""
        backend = AlwaysTransientErrorBackend()
        result = await execute_with_downgrade(
            backend, "x", "sonnet", str(tmp_path), timeout=None,
            settings=Settings(defaults={"llm": {"backend_max_retries": 2}}),
        )
        assert result.success is False
        assert "Backend error" in result.error
        assert len(backend.calls) == 3  # 1 + 2 retries

    @pytest.mark.asyncio
    async def test_overload_uses_downgrade_not_backend_retry(self, tmp_path):
        """Overload must take the model-downgrade path, NOT the backend-retry
        path — it is not double-counted. With defaults={"llm": {"backend_max_retries": 5}} but a
        2-step overload, the executor walks opus→sonnet (2 calls) and
        succeeds; the backend-retry budget is never consumed."""
        backend = OverloadThenSucceedBackend(fail_count=1, response="recovered")
        result = await execute_with_downgrade(
            backend, "x", "opus", str(tmp_path), timeout=None,
            settings=Settings(defaults={"llm": {"backend_max_retries": 5}}),
        )
        assert result.success is True
        assert result.output == "recovered"
        # Exactly the downgrade walk — opus then sonnet. No extra
        # backend-retry attempts (which would re-send opus a 3rd time).
        assert [c[1] for c in backend.calls] == ["opus", "sonnet"]

    @pytest.mark.asyncio
    async def test_overload_exhausted_chain_retries_with_backoff(self, tmp_path):
        """A model outside the downgrade chain (every Codex model) exhausts
        the chain on its first overload. The exhausted chain then spends the
        backend-retry budget: two overloads + success = 3 dispatches of the
        same model, each retry preceded by its ``retry_backoff`` sleep. With
        budget 0 the first overload is terminal."""
        backend = OverloadThenSucceedBackend(fail_count=2, response="recovered")
        started = time.monotonic()
        result = await execute_with_downgrade(
            backend, "x", "gpt-5.3-codex", str(tmp_path), timeout=None,
            settings=Settings(defaults={"llm": {"backend_max_retries": 2}}, retry_backoff=[0.01]),
        )
        assert time.monotonic() - started >= 0.02
        assert result.success is True
        assert result.output == "recovered"
        assert [c[1] for c in backend.calls] == ["gpt-5.3-codex"] * 3

        backend = OverloadThenSucceedBackend(fail_count=2, response="recovered")
        result = await execute_with_downgrade(
            backend, "x", "gpt-5.3-codex", str(tmp_path), timeout=None,
            settings=Settings(defaults={"llm": {"backend_max_retries": 0}}),
        )
        assert result.success is False
        assert result.error_kind == "overload"
        assert len(backend.calls) == 1

    @pytest.mark.asyncio
    async def test_overload_exhaustion_rewalks_chain_per_backend_retry(self, tmp_path):
        """Persistent overload with budget 1: the chain is walked from the
        original model twice (1 initial + 1 retry), then the failure keeps
        kind ``overload``."""
        backend = AlwaysOverloadBackend()
        result = await execute_with_downgrade(
            backend, "x", "opus", str(tmp_path), timeout=None,
            settings=Settings(defaults={"llm": {"backend_max_retries": 1}}, retry_backoff=[0.01]),
        )
        assert result.success is False
        assert "exhausted model chain" in result.error
        assert "haiku" in result.error
        assert result.error_kind == "overload"
        assert [c[1] for c in backend.calls] == ["opus", "sonnet", "haiku"] * 2

    @pytest.mark.asyncio
    async def test_backend_retry_distinct_from_gate_max_retries(self, tmp_path):
        """The backend-retry budget is read from backend_max_retries, NOT
        max_retries. A high gate max_retries with defaults={"llm": {"backend_max_retries": 0}} does
        NOT retry a transient backend error."""
        backend = AlwaysTransientErrorBackend()
        result = await execute_with_downgrade(
            backend, "x", "sonnet", str(tmp_path), timeout=None,
            settings=Settings(max_retries=9, defaults={"llm": {"backend_max_retries": 0}}),
        )
        assert result.success is False
        assert len(backend.calls) == 1  # gate budget irrelevant here
