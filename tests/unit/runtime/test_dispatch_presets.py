"""Verify DispatchExecutor's preset-aware backend resolution.

Tests focus on _resolve_prompt_backend + the registry-shape construction:
given a registry of preset-keyed backends, the dispatcher picks
the right one for a node based on params.preset vs the default flag.

No actual network calls — CLIBackend spawns only on ``send_prompt``;
construction is offline-safe. The tests inspect
the returned backend's identity and per-call model resolution.
"""
from __future__ import annotations

import pytest

from sqrlly.runtime.executor.backends.cli import CLIBackend
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.schema.models import Execute, Node, LlmPreset, Settings


def _node(id_="n1", params=None):
    return Node(
        id=id_, name=id_,
        execute=Execute(url="prompt.md", params=params or {}),
    )


def _settings_with_presets():
    return Settings(presets={
        "cheap": LlmPreset(
            transport="cli", provider="anthropic", model="haiku",
        ),
        "smart": LlmPreset(
            transport="cli", provider="anthropic", model="opus",
            default=True,
        ),
    })


class TestResolvePromptBackend:
    def test_single_preset_via_registry(self):
        """One-entry registry: node resolves to that backend via default preset."""
        from sqrlly.schema.models import LlmPreset
        backend = CLIBackend()
        settings = Settings(presets={
            "default": LlmPreset(
                transport="cli", provider="anthropic",
                model="sonnet", default=True,
            ),
        })
        dispatcher = DispatchExecutor(
            prompt_backends={"default": backend}, settings=settings,
        )
        resolved = dispatcher._resolve_prompt_backend(_node(), settings)
        assert resolved is not None
        assert resolved[1] is backend

    def test_multi_preset_uses_default(self):
        settings = _settings_with_presets()
        cheap_be = CLIBackend()
        smart_be = CLIBackend()
        dispatcher = DispatchExecutor(
            prompt_backends={"cheap": cheap_be, "smart": smart_be},
            settings=settings,
        )
        # Node without params.preset → uses default (smart).
        resolved = dispatcher._resolve_prompt_backend(_node(), settings)
        assert resolved is not None
        assert resolved[1] is smart_be

    def test_multi_preset_params_override(self):
        settings = _settings_with_presets()
        cheap_be = CLIBackend()
        smart_be = CLIBackend()
        dispatcher = DispatchExecutor(
            prompt_backends={"cheap": cheap_be, "smart": smart_be},
            settings=settings,
        )
        # Node with params.preset=cheap → uses cheap.
        node = _node(params={"preset": "cheap"})
        resolved = dispatcher._resolve_prompt_backend(node, settings)
        assert resolved is not None
        assert resolved[1] is cheap_be

    def test_no_backends_returns_none(self):
        dispatcher = DispatchExecutor(settings=Settings())
        assert dispatcher._resolve_prompt_backend(_node(), Settings()) is None

    def test_missing_backend_for_preset_raises(self):
        settings = _settings_with_presets()
        # Registry missing the 'smart' backend that settings declares as default.
        dispatcher = DispatchExecutor(
            prompt_backends={"cheap": CLIBackend()},
            settings=settings,
        )
        with pytest.raises(RuntimeError, match="no backend is registered"):
            dispatcher._resolve_prompt_backend(_node(), settings)

class TestGetBackend:
    """get_backend() returns the right backend for LLM-gate dispatch."""

    def test_single_preset_returns_the_only_backend(self):
        """One-entry registry: get_backend() returns it."""
        from sqrlly.schema.models import LlmPreset
        backend = CLIBackend()
        settings = Settings(presets={
            "default": LlmPreset(
                transport="cli", provider="anthropic",
                model="sonnet", default=True,
            ),
        })
        dispatcher = DispatchExecutor(
            prompt_backends={"default": backend}, settings=settings,
        )
        assert dispatcher.get_backend() is backend

    def test_multi_preset_returns_default(self):
        settings = _settings_with_presets()
        cheap_be = CLIBackend()
        smart_be = CLIBackend()
        dispatcher = DispatchExecutor(
            prompt_backends={"cheap": cheap_be, "smart": smart_be},
            settings=settings,
        )
        # `smart` is flagged default — that backend goes to LLM gates.
        assert dispatcher.get_backend() is smart_be

    def test_no_backends_returns_none(self):
        assert DispatchExecutor().get_backend() is None

    def test_skips_command_preset_ordered_before_default(self):
        """A mixed registry where a CommandPreset precedes the default
        LlmPreset still returns the LLM default. CommandPresets carry no
        ``default`` flag, so get_backend must not assume every preset has
        one — regression for the absurd-paper example, the first workflow
        to mix an LlmPreset (gates) with a CommandPreset (PDF render)."""
        from sqrlly.schema.models import CommandPreset

        built: list[str] = []

        def cli_builder():
            built.append("cli")
            return CLIBackend()

        settings = Settings(presets={
            # CommandPreset FIRST in insertion order — get_backend iterates
            # it before reaching the default LlmPreset.
            "render_cmd": CommandPreset(command="uv run --script"),
            "default": LlmPreset(
                transport="cli", provider="anthropic",
                model="sonnet", default=True,
            ),
        })
        dispatcher = DispatchExecutor(
            prompt_backend_builders={"default": cli_builder},
            settings=settings,
        )
        backend = dispatcher.get_backend()
        assert isinstance(backend, CLIBackend)
        assert built == ["cli"]


class TestSafeMode:
    """`create_backend_from_preset(preset, safe_mode=True)` appends
    ``--safe-mode`` to a cli backend's argv (isolating the run from the
    operator's Claude customizations) — it's a `claude` CLI flag, so
    Codex presets are left untouched."""

    def test_safe_mode_appends_flag_to_cli_backend(self):
        from sqrlly.runtime.executor.backends.factory import (
            create_backend_from_preset,
        )
        be = create_backend_from_preset(
            LlmPreset(transport="cli", provider="anthropic", model="sonnet"),
            safe_mode=True,
        )
        assert "--safe-mode" in (be._cli_args or [])

    def test_safe_mode_off_leaves_cli_args_clean(self):
        from sqrlly.runtime.executor.backends.factory import (
            create_backend_from_preset,
        )
        be = create_backend_from_preset(
            LlmPreset(transport="cli", provider="anthropic", model="sonnet"),
            safe_mode=False,
        )
        assert "--safe-mode" not in (be._cli_args or [])

    def test_safe_mode_preserves_existing_cli_args_and_dedups(self):
        from sqrlly.runtime.executor.backends.factory import (
            create_backend_from_preset,
        )
        be = create_backend_from_preset(
            LlmPreset(
                transport="cli", provider="anthropic", model="sonnet",
                cli_args=["--safe-mode", "--verbose"],
            ),
            safe_mode=True,
        )
        assert be._cli_args.count("--safe-mode") == 1
        assert "--verbose" in be._cli_args

    def test_safe_mode_does_not_add_claude_flag_to_codex(self):
        from sqrlly.runtime.executor.backends.factory import (
            create_backend_from_preset,
        )
        be = create_backend_from_preset(
            LlmPreset(
                transport="cli", provider="openai", model="gpt-5.6-luna",
            ),
            safe_mode=True,
        )
        assert "--safe-mode" not in (be._cli_args or [])


def _two_preset_settings():
    """cli (default) + unused (declared but referenced by no node)."""
    return Settings(presets={
        "cli": LlmPreset(
            transport="cli", provider="anthropic",
            model="sonnet", default=True,
        ),
        "unused": LlmPreset(
            transport="cli", provider="anthropic", model="haiku",
        ),
    })


class TestLazyBackendBuild:
    """Backends are materialized lazily — a declared-but-unused preset
    never constructs its backend; it is built only when a node actually
    dispatches to that preset."""

    def test_unused_preset_builder_not_invoked(self):
        built: list[str] = []

        def cli_builder():
            built.append("cli")
            return CLIBackend()

        def unused_builder():
            built.append("unused")
            raise AssertionError("unused preset backend must not be built")

        s = _two_preset_settings()
        d = DispatchExecutor(
            prompt_backend_builders={"cli": cli_builder, "unused": unused_builder},
            settings=s,
        )
        # A node with no params.preset resolves to the default (cli).
        resolved = d._resolve_prompt_backend(_node(), s)
        assert resolved is not None
        assert built == ["cli"]  # unused_builder never called

    def test_builder_invoked_once_then_cached(self):
        calls: list[int] = []

        def cli_builder():
            calls.append(1)
            return CLIBackend()

        s = _two_preset_settings()
        d = DispatchExecutor(
            prompt_backend_builders={
                "cli": cli_builder,
                "unused": lambda: CLIBackend(),
            },
            settings=s,
        )
        first = d._resolve_prompt_backend(_node("a"), s)
        second = d._resolve_prompt_backend(_node("b"), s)
        assert first[1] is second[1]  # cached backend
        assert calls == [1]           # backend built exactly once

    def test_get_backend_builds_only_default(self):
        built: list[str] = []

        def cli_builder():
            built.append("cli")
            return CLIBackend()

        def unused_builder():
            built.append("unused")
            raise AssertionError("non-default preset must not be built")

        s = _two_preset_settings()
        d = DispatchExecutor(
            prompt_backend_builders={"cli": cli_builder, "unused": unused_builder},
            settings=s,
        )
        backend = d.get_backend()
        assert isinstance(backend, CLIBackend)
        assert built == ["cli"]

    @pytest.mark.asyncio
    async def test_close_skips_unbuilt_backends(self):
        def unused_builder():
            raise AssertionError("unbuilt preset must not be built by close()")

        s = _two_preset_settings()
        d = DispatchExecutor(
            prompt_backend_builders={
                "cli": lambda: CLIBackend(),
                "unused": unused_builder,
            },
            settings=s,
        )
        d._resolve_prompt_backend(_node(), s)  # builds cli only
        await d.close()  # must not touch the unbuilt builder

    def test_prebuilt_backends_still_supported(self):
        """Embedders/tests passing already-built backends keep working;
        identity is preserved (wrapped as a constant builder)."""
        backend = CLIBackend()
        s = Settings(presets={
            "default": LlmPreset(
                transport="cli", provider="anthropic",
                model="sonnet", default=True,
            ),
        })
        d = DispatchExecutor(prompt_backends={"default": backend}, settings=s)
        assert d.get_backend() is backend

