from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from jinja2 import Environment

from sqrlly.runtime.result import ExecutionResult, OverloadError, PromptBackend
from sqrlly.schema.models import Settings


# Tier list for OverloadError auto-downgrade. Fixed — the chain mirrors
# Anthropic's model tiers, not a per-workflow knob.
MODEL_DOWNGRADE_CHAIN = ["opus", "sonnet", "haiku"]


def downgrade_model(current: str | None, chain: list[str]) -> str | None:
    try:
        idx = chain.index(current)
    except ValueError:
        return None
    if idx + 1 < len(chain):
        return chain[idx + 1]
    return None


def retry_delay(attempt: int, backoff: list[float]) -> float:
    """Seconds to sleep before retry ``attempt`` (1-indexed).

    Clamps to the last value past the list length; 0.0 when ``backoff``
    is empty. The single clamp shared by gate retries (compile layer —
    the allowed import direction is compile → runtime) and backend
    retries here.
    """
    if not backoff:
        return 0.0
    idx = min(attempt - 1, len(backoff) - 1)
    return backoff[idx]


class _WorkflowEnvironment(Environment):
    def getattr(self, obj, attribute):
        if isinstance(obj, dict) and attribute in obj:
            return obj[attribute]
        return super().getattr(obj, attribute)


_TEMPLATES = _WorkflowEnvironment(keep_trailing_newline=True)


def render_template(template: str, context: dict[str, Any]) -> str:
    return _TEMPLATES.from_string(template).render(**context)


def prepend_eval_preamble(rendered: str, context: dict[str, Any]) -> str:
    """Prepend the selected route context after rendering the destination prompt."""
    eval_preamble = context.get("_route_eval_preamble")
    if not eval_preamble:
        return rendered
    return f"{eval_preamble}\n\n{rendered}"


def apply_preamble(
    template: str, *, settings: Settings, base_workdir: str,
) -> str:
    """Read the declaring workflow's preamble, using base_workdir for in-memory settings."""
    preamble_path = settings.preamble_path(Path(base_workdir))
    if preamble_path is None:
        return template
    return preamble_path.read_text() + "\n\n" + template


async def execute_with_downgrade(
    backend: PromptBackend,
    rendered: str,
    model: str | None,
    workdir: str,
    timeout: float | None = None,
    *,
    settings: Settings,
    max_retries: int | None = None,
) -> ExecutionResult:
    """Send a pre-rendered prompt with overload→downgrade fallback.

    ``settings`` is scope-aware — provides ``backend_max_retries`` /
    ``retry_backoff`` for this scope (subgraph wrappers pass the
    merged settings).

    Bounded retry layer wrapping the overload-downgrade loop: a backend
    exception (e.g. the CLI backend's `claude exited 1`) re-enters the
    downgrade loop from the ORIGINAL model up to `backend_max_retries`
    times, sleeping `retry_backoff` between attempts. An OverloadError
    first walks the downgrade chain without consuming a retry; once the
    chain has no lower model (immediately, for a model outside the chain
    such as every Codex model) it consumes one like any other error.
    attempt 0 = first try.
    """
    if max_retries is None:
        max_retries = settings.defaults.llm.backend_max_retries
    attempt = 0
    while True:
        current_model = model
        try:
            while True:
                try:
                    result = await backend.send_prompt(
                        rendered, current_model, workdir, timeout=timeout,
                    )
                    break
                except OverloadError:
                    next_model = downgrade_model(
                        current_model, MODEL_DOWNGRADE_CHAIN
                    )
                    if next_model is None:
                        raise
                    current_model = next_model
            break
        except Exception as e:
            if attempt >= max_retries:
                # Classified only at exhaustion so the retry loop treats
                # every error alike. A backend-level (params.timeout)
                # deadline is a `timeout`, not a generic `backend_error`.
                if isinstance(e, OverloadError):
                    return ExecutionResult(
                        success=False,
                        error=(
                            f"API overloaded, exhausted model chain "
                            f"(last: {current_model})"
                        ),
                        error_kind="overload",
                    )
                if isinstance(e, asyncio.TimeoutError):
                    return ExecutionResult(
                        success=False, error=f"Backend timed out: {e}",
                        error_kind="timeout",
                    )
                return ExecutionResult(
                    success=False, error=f"Backend error: {e}",
                    error_kind="backend_error",
                )
            attempt += 1
            delay = retry_delay(attempt, settings.retry_backoff)
            if delay > 0:
                await asyncio.sleep(delay)

    return ExecutionResult(
        success=True,
        output=result.output,
        structured_output=result.structured_output,
        model=current_model,
    )
