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

    def test_fan_out_final_node_id_is_checked(self):
        """A hyphenated id on a fan-out final node is flagged too."""
        parent = {
            "id": "fan",
            "name": "fan",
            "run": ["echo"],
            "fan_out": {
                "template": {"agent": {"text": "test"}},
                "final_nodes": [{"id": "merge-results", "name": "Merge"}],
            },
        }
        config = make_config([parent])
        warnings = collect_warnings(config)
        # (A runtime-manifest fan-out also draws the drift advisory; filter to
        # the hyphen check this test is about.)
        hyphen = [w for w in warnings if "{{merge-results}}" in w]
        assert len(hyphen) == 1
        assert "merge_results" in hyphen[0]


def _fan_node(node_id: str, *, manifest_path: str | None = None) -> dict:
    fo: dict = {"template": {"agent": {"text": "test"}}}
    if manifest_path is not None:
        fo["manifest_path"] = manifest_path
    return {
        "id": node_id, "name": node_id,
        "run": ["echo"], "fan_out": fo,
    }


class TestManifestDriftLint:
    def test_runtime_manifest_fanout_warns(self):
        """A fan-out whose manifest is a RUNTIME output (no manifest_path) gets
        an advisory: --resume needs deterministic item ids."""
        config = make_config([_fan_node("workers")])
        drift = [
            w for w in collect_warnings(config)
            if "workers" in w and "resume" in w.lower()
        ]
        assert len(drift) == 1
        assert "on_manifest_drift" in drift[0]

    def test_static_manifest_path_is_silent(self):
        """A static `manifest_path` file has stable ids by construction — no
        drift advisory."""
        config = make_config([_fan_node("workers", manifest_path="m.json")])
        drift = [
            w for w in collect_warnings(config)
            if "workers" in w and "resume" in w.lower()
        ]
        assert drift == []

    def test_non_fanout_node_is_silent(self):
        config = make_config([_node("plain")])
        assert [w for w in collect_warnings(config) if "resume" in w.lower()] == []


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


class TestFanoutParentPromoteWarnings:
    def test_fanout_parent_promote_warns(self):
        from sqrlly.compile.lint import collect_warnings
        from sqrlly.schema.models import Graph
        cfg = Graph(**{
            "name": "t",
            "nodes": [{
                "id": "build", "name": "build", "promote": True,
                "fan_out": {"manifest_path": "m.json",
                            "template": {"workflow": "w.yaml"}},
            }],
            "settings": {},
        })
        warns = collect_warnings(cfg)
        assert any("fan_out.promote" in w and "build" in w for w in warns)

    def test_fanout_promote_no_warn(self):
        from sqrlly.compile.lint import collect_warnings
        from sqrlly.schema.models import Graph
        cfg = Graph(**{
            "name": "t",
            "nodes": [{
                "id": "build", "name": "build",
                "fan_out": {"manifest_path": "m.json",
                            "template": {"workflow": "w.yaml"},
                            "promote": True},
            }],
            "settings": {},
        })
        assert not any("fan_out.promote" in w for w in collect_warnings(cfg))


class TestWorktreeSetupExcludeWarnings:
    def test_warns_prisma_generate_without_exclude(self):
        config = make_config(
            [_node("n")],
            worktree_setup=["pnpm exec prisma generate"],
        )
        warnings = collect_warnings(config)
        assert any("prisma generate" in w and "worktree_setup_exclude" in w for w in warnings)

    def test_no_warn_when_exclude_present(self):
        config = make_config(
            [_node("n")],
            worktree_setup=["pnpm exec prisma generate"],
            worktree_setup_exclude=["src/generated/prisma"],
        )
        assert not any("prisma generate" in w for w in collect_warnings(config))

    def test_no_warn_when_no_prisma_generate(self):
        config = make_config(
            [_node("n")],
            worktree_setup=["pnpm install --prefer-offline"],
        )
        assert not any("prisma generate" in w for w in collect_warnings(config))


def test_forward_route_stays_silent_with_bounded_return():
    graph = make_config([
        {**_node("plan"), "route": [{"then": "review"}]},
        {**_node("review"), "route": [{"then": "plan", "max_attempts": 3}]},
    ])
    assert collect_warnings(graph) == []
