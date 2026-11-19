"""Unit tests for compile-time lint warnings (compile/lint.py).

`collect_warnings` is a pure function over `Graph` — no I/O, no
langgraph. Each check gets a known-good (silent) and known-bad (warns)
fixture per the project's function-level test doctrine.
"""
from __future__ import annotations

from sqrlly.compile.lint import collect_warnings

from helpers import make_config


def _node(node_id: str) -> dict:
    return {"id": node_id, "name": node_id, "agent": {"text": "test"}}


class TestHyphenatedIdWarnings:
    def test_underscore_ids_are_silent(self):
        config = make_config([_node("research_phase"), _node("write_up")])
        assert collect_warnings(config) == []

    def test_hyphenated_id_warns(self):
        config = make_config([_node("research-phase")])
        warnings = collect_warnings(config)
        assert len(warnings) == 1
        message = warnings[0]
        assert "research-phase" in message
        # The exact Jinja-subtraction footgun is shown verbatim.
        assert "{{research-phase}}" in message
        # The underscore rename is suggested.
        assert "research_phase" in message

    def test_one_warning_per_hyphenated_id(self):
        config = make_config([_node("a-b"), _node("ok"), _node("c-d-e")])
        warnings = collect_warnings(config)
        assert len(warnings) == 2
        assert any("a-b" in w for w in warnings)
        assert any("c-d-e" in w for w in warnings)

def test_repeating_run_defaults_to_isolation_without_shared_worktree_warning():
    config = make_config([
        {"id": "batch", "run": "echo batch", "for_each": {"file": "items.json"}},
        {"id": "serial", "run": "echo serial"},
    ])
    assert collect_warnings(config) == []


def test_upstream_route_warns_without_attempt_limit():
    graph = make_config([
        _node("plan"),
        {**_node("build"), "depends_on": ["plan"]},
        {**_node("review"), "depends_on": ["build"], "route": [
            {"when": "attempt < 3", "then": "plan", "max_attempts": 3},
            {"else": "build"},
        ]},
    ])
    assert collect_warnings(graph) == ["review: route to upstream node 'build' has no max_attempts"]


def test_setup_commands_require_promotion_excludes():
    config = make_config([_node("n")], worktree_setup=["make generated"])
    assert any("promote_exclude" in warning for warning in collect_warnings(config))
    config.settings.promote_exclude = ["generated/"]
    assert collect_warnings(config) == []


def test_unordered_shared_nodes_warn_once_per_pair():
    config = make_config([
        {"id": "first", "run": "true", "route": [{"then": "middle"}]},
        {"id": "unrelated", "agent": {"text": "write"}, "worktree": "off"},
        {"id": "middle"},
        {"id": "last", "run": "true", "depends_on": ["middle"]},
        {"id": "isolated", "agent": {"text": "edit"}},
    ])
    warnings = collect_warnings(config)
    assert [warning.split(" use ")[0] for warning in warnings] == ["first and unrelated", "unrelated and last"]


def test_forward_route_stays_silent_with_bounded_return():
    graph = make_config([
        {**_node("plan"), "route": [{"then": "review"}]},
        {**_node("review"), "route": [{"then": "plan", "max_attempts": 3}]},
    ])
    assert collect_warnings(graph) == []
