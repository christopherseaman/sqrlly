"""Subprocess-per-call CLI backend.

Spawns a fresh provider CLI subprocess per
``send_prompt``. No warm process, no protocol, no shared accumulator —
every call is fully independent. Concurrent calls get real
``asyncio.create_subprocess_exec`` parallelism without contending for a
single warm session.

Lifecycle: zero. ``close()`` is a no-op. There is no in-process state
to tear down, and each subprocess inherits / exits on its own.

Overload mapping uses ``OVERLOAD_SUBSTRINGS = {"529", "overload"}``,
shared by all CLI providers.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
from contextlib import ExitStack
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqrlly.runtime.executor.backends._overload import (
    OVERLOAD_SUBSTRINGS,
    maybe_raise_overload,
)
from sqrlly.schema.json_schema import load_validator, provider_envelope
from sqrlly.runtime.result import ExecutionResult


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """Terminate the process group created by ``start_new_session=True``."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            # The group keeps its original ID after the leader exits.
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        if sig is signal.SIGTERM:
            await asyncio.sleep(0.5)  # grace period for clean shutdown
    await proc.wait()


class CLIBackend:
    """Subprocess-per-call LLM backend.

    Each ``send_prompt`` spawns a fresh provider CLI subprocess, pipes the
    prompt on stdin, and captures stdout as the response. Exit code 0 →
    success. On non-zero exit, both streams are inspected for overload
    substrings and surfaced in the error.
    """

    def __init__(
        self,
        argv_prefix: tuple[str, ...] = ("claude", "-p"),
        *,
        prompt_arg: str | None = None,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        cli_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        disable_codex_mcp: bool = False,
        output_schema: Path | dict | bool | None = None,
    ):
        self._output_schema = output_schema
        self._argv_prefix = argv_prefix
        self._prompt_arg = prompt_arg
        self._permission_mode = permission_mode
        self._allowed_tools = allowed_tools
        self._disallowed_tools = disallowed_tools
        self._cli_args = cli_args
        self._env = env
        self._disable_codex_mcp = disable_codex_mcp
        self._mcp_args: dict[str, list[str]] = {}

    def _tool_argv(self) -> list[str]:
        """Provider-specific flags appended to every invocation."""
        argv: list[str] = []
        if self._permission_mode:
            argv += ["--permission-mode", self._permission_mode]
        if self._allowed_tools:
            argv += ["--allowedTools", *self._allowed_tools]
        if self._disallowed_tools:
            argv += ["--disallowedTools", *self._disallowed_tools]
        if self._cli_args:
            argv += list(self._cli_args)
        return argv

    async def send_prompt(
        self, prompt: str, model: str | None, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        argv = list(self._argv_prefix)
        if model is not None:
            argv.extend(["--model", model])
        argv.extend(self._tool_argv())
        claude = self._argv_prefix[0] == "claude"
        wrapped = False
        with ExitStack() as files:
            if self._output_schema is not None:
                schema = self._output_schema
                if isinstance(schema, Path):
                    schema, _ = load_validator(schema, schema.parent)
                # Both provider APIs require an object at the root.
                wrapped = not isinstance(schema, dict) or schema.get("type") != "object"
                if wrapped:
                    schema = provider_envelope(schema)
                if claude:
                    argv.extend(["--output-format", "json", "--json-schema", json.dumps(schema)])
                else:
                    file = files.enter_context(NamedTemporaryFile(mode="w", suffix=".json"))
                    json.dump(schema, file)
                    file.flush()
                    argv.extend(["--output-schema", file.name])
            async with asyncio.timeout(timeout):
                if self._disable_codex_mcp:
                    if workdir not in self._mcp_args:
                        self._mcp_args[workdir] = await self._disabled_mcp_args(workdir)
                    argv.extend(self._mcp_args[workdir])
                if self._prompt_arg is not None:
                    argv.append(self._prompt_arg)
                output = await self._invoke(argv, prompt, workdir)
        if self._output_schema is None:
            return ExecutionResult(output=output)
        try:
            value = json.loads(output)
        except json.JSONDecodeError:
            return ExecutionResult(output=output)
        if claude:
            if not isinstance(value, dict) or "structured_output" not in value:
                text = value.get("result", "") if isinstance(value, dict) else ""
                return ExecutionResult(output=text)
            output = value.get("result") or json.dumps(value["structured_output"])
            value = value["structured_output"]
        if wrapped and isinstance(value, dict) and "value" in value:
            value = value["value"]
            output = json.dumps(value)
        return ExecutionResult(output=output, structured_output=value)

    async def _disabled_mcp_args(self, workdir: str) -> list[str]:
        servers = await self._invoke(
            ["codex", "mcp", "list", "--json", "--disable", "apps", "--disable", "plugins"],
            "", workdir, sensitive_output=True,
        )
        disabled = ", ".join(
            f'{json.dumps(server["name"], ensure_ascii=False)}={{enabled=false}}'
            for server in json.loads(servers)
        )
        return ["-c", "mcp_servers={" + disabled + "}"]

    async def _invoke(self, argv: list[str], prompt: str, workdir: str, *,
                      sensitive_output: bool = False) -> str:
        proc_env = {**os.environ, **self._env} if self._env else None
        # start_new_session=True puts the child in its OWN process group
        # (the child becomes the group leader), so a runaway `claude` that
        # forked descendants (MCP servers, test runners, headless browsers)
        # is killable as a GROUP on timeout — see _kill_process_group.
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                env=proc_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            # The CLI binary isn't on PATH. Surface an
            # actionable message instead of a bare FileNotFoundError errno —
            # a user who installed sqrlly but not the selected CLI hits this on the
            # first prompt node.
            missing = e.filename or self._argv_prefix[0]
            raise RuntimeError(
                f"Command not found on PATH: {missing!r}. The "
                f"{self._argv_prefix[0]!r} CLI must be installed and on PATH "
                f"for transport: cli."
            ) from e
        try:
            stdout_b, stderr_b = await proc.communicate(input=prompt.encode())
        except asyncio.CancelledError:
            # The task was cancelled (e.g. the orchestrator is shutting down
            # an in-flight node). Kill the process group so the provider CLI subtree
            # doesn't leak, then re-raise so cooperative cancellation propagates.
            await _kill_process_group(proc)
            raise

        if proc.returncode != 0:
            if sensitive_output:
                raise RuntimeError(f"Codex MCP configuration lookup exited {proc.returncode}")
            stderr_text = stderr_b.decode().strip()
            stdout_text = stdout_b.decode().strip()
            # Provider CLIs may write their result and diagnostics (overload /
            # rate-limit / usage-limit messages) to STDOUT, not stderr — so
            # on a non-zero exit the actual reason may be in stdout.
            # Feed BOTH streams to the overload check (a 529/overload on
            # stdout must still engage the downgrade chain, not go terminal)
            # and surface BOTH in the error so the failure is never a bare
            # provider-exited message.
            combined = "\n".join(t for t in (stderr_text, stdout_text) if t)
            maybe_raise_overload(
                RuntimeError(combined),
                message_substrings=OVERLOAD_SUBSTRINGS,
            )
            detail = combined or "(no output on stdout or stderr)"
            raise RuntimeError(
                f"{self._argv_prefix[0]} exited {proc.returncode}: {detail}"
            )
        return stdout_b.decode().strip()

    async def close(self) -> None:
        # Subprocess-per-call backend has no warm state. ``close`` is
        # part of the ``PromptBackend`` protocol; satisfying it as a
        # no-op keeps the executor registry's close-all loop uniform.
        return
