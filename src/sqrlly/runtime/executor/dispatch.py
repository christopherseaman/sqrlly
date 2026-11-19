from __future__ import annotations

import asyncio
import errno
import os
from pathlib import Path
from typing import Any

from jinja2 import TemplateError

from sqrlly.runtime.executor.backends.cli import _kill_process_group
from sqrlly.runtime.executor.backends.factory import create_backend
from sqrlly.runtime.executor.prompt import (
    apply_preamble, execute_with_downgrade, prepend_eval_preamble, render_template, retry_delay,
)
from sqrlly.runtime.result import ExecutionResult, PromptBackend
from sqrlly.schema.models import Node, Settings


async def run_command(argv: list[str], env: dict[str, str], cwd: str,
               timeout: float | None) -> ExecutionResult:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env={**os.environ, **env} if env else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError) as exc:
        await _kill_process_group(proc)
        if isinstance(exc, asyncio.CancelledError):
            raise
        return ExecutionResult(success=False, error=f"Command timed out after {timeout}s", error_kind="timeout")
    return ExecutionResult(
        success=proc.returncode == 0, exit_code=proc.returncode, output=stdout.decode(errors="replace"),
        error=f"Exit code {proc.returncode}: {stderr.decode(errors='replace')}" if proc.returncode else None,
    )


class DispatchExecutor:
    def __init__(self, workdir: str = ".", settings: Settings | None = None,
                 *, safe_mode: bool = False):
        self._workdir = workdir
        self._settings = settings or Settings()
        self._safe_mode = safe_mode
        self._dispatch_sem = asyncio.Semaphore(self._settings.max_parallel_jobs)

    async def execute(
        self, node: Node, context: dict[str, Any], workdir: str | None = None,
        settings_override: Settings | None = None,
    ) -> ExecutionResult:
        settings = settings_override or self._settings
        cwd = workdir or self._workdir
        workflow_dir = settings.workflow_dir or Path(self._workdir).resolve()
        context = {"workflow_dir": str(workflow_dir), **context}
        async with self._dispatch_sem:
            try:
                if node.run is not None:
                    argv = [render_template(arg, context) for arg in node.run.cmd]
                    env = {k: render_template(v, context) for k, v in node.run.env.items()}
                    return await self._run_with_retries(
                        argv, env, cwd, node.effective_timeout(settings), settings,
                    )
                if node.workflow is not None:
                    return ExecutionResult(success=False, error=f"Workflow {node.id} requires graph compilation")
                prompt = node.agent or node.llm
                if prompt is None:
                    return ExecutionResult(output="")
                defaults = settings.defaults.agent if node.agent else settings.defaults.llm
                provider = prompt.provider or defaults.provider
                model = prompt.model or defaults.model or settings.model
                body = prompt.text
                if body is None:
                    body = (workflow_dir / settings.prompts / prompt.prompt).read_text()
                body = apply_preamble(body, settings=settings, base_workdir=str(workflow_dir))
                rendered = prepend_eval_preamble(render_template(body, context), context)
                if feedback := context.get("_output_retry_reason"):
                    rendered = f"{feedback}\n\n{rendered}"
                tools = None
                cli_args = None
                if node.agent is not None:
                    tools = node.agent.tools if node.agent.tools is not None else defaults.tools
                    cli_args = (node.agent.cli_args if "cli_args" in node.agent.model_fields_set
                                else defaults.cli_args)
                schema = node.outputs.schema_document if node.outputs else None
                if schema is None and node.outputs and node.outputs.schema_file:
                    schema = workflow_dir / node.outputs.schema_file
                backend = create_backend(
                    provider, tools=tools, cli_args=cli_args, safe_mode=self._safe_mode,
                    output_schema=schema,
                )
                result = await execute_with_downgrade(
                    backend, rendered, model, cwd, timeout=node.effective_timeout(settings),
                    settings=settings, max_retries=defaults.backend_max_retries,
                )
                result.provider = provider
                return result
            except (ValueError, OSError, TemplateError) as exc:
                return ExecutionResult(success=False, error=str(exc))

    async def _run_with_retries(
        self, argv: list[str], env: dict[str, str], cwd: str,
        timeout: float | None, settings: Settings,
    ) -> ExecutionResult:
        retries = settings.defaults.run.backend_max_retries
        for attempt in range(retries + 1):
            try:
                result = await run_command(argv, env, cwd, timeout)
            except FileNotFoundError as exc:
                return ExecutionResult(success=False, error=f"Command not found on PATH: {exc.filename!r}")
            except OSError as exc:
                if exc.errno not in {errno.EAGAIN, errno.ENOMEM, errno.ETXTBSY} or attempt == retries:
                    raise
            else:
                if result.success or result.error_kind == "timeout" or attempt == retries:
                    return result
            await asyncio.sleep(retry_delay(attempt + 1, settings.retry_backoff))

    def get_backend(self, settings: Settings | None = None) -> PromptBackend:
        return create_backend((settings or self._settings).defaults.llm.provider,
                              safe_mode=self._safe_mode)

    async def close(self) -> None:
        """Provider processes finish within each execute call."""
