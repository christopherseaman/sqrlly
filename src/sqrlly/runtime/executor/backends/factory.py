"""Translate workflow tool policies to provider CLI arguments."""
from sqrlly.runtime.executor.backends.cli import CLIBackend


def create_backend(
    provider: str = "openai", *, tools: str | list[str] | None = None,
    cli_args: list[str] | None = None, safe_mode: bool = False,
) -> CLIBackend:
    if provider == "anthropic":
        if isinstance(tools, list):
            args = ["--tools", ",".join(tools), "--allowedTools", ",".join(tools)]
        elif tools is None:
            args = ["--tools", "", "--strict-mcp-config"]
        else:
            modes = {"read": "plan", "edit": "acceptEdits", "full": "bypassPermissions"}
            args = ["--permission-mode", modes[tools]]
        if safe_mode:
            args.append("--safe-mode")
        return CLIBackend(("claude", "-p"), cli_args=[*args, *(cli_args or [])])

    modes = {None: "read-only", "read": "read-only", "edit": "workspace-write",
             "full": "danger-full-access"}
    args = ["--sandbox", modes[tools]]
    if tools is None:
        for feature in (
            "shell_tool", "view_image", "apps", "plugins", "multi_agent_v2",
            "goals", "computer_use", "image_generation", "code_mode", "hooks",
        ):
            args.extend(["--disable", feature])
        for setting in (
            "agents.enabled=false", 'web_search="disabled"',
            "tools.experimental_request_user_input.enabled=false",
            "tools.update_plan.enabled=false",
        ):
            args.extend(["-c", setting])
    return CLIBackend(
        ("codex", "exec", "--skip-git-repo-check"),
        cli_args=[*args, *(cli_args or [])], prompt_arg="-", disable_codex_mcp=tools is None,
    )
