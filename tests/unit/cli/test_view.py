"""Tests for the `sqrlly view` command + renderer.

Three layers:
  - Mermaid emitter (pure function on Graph).
  - Status overlay computation (pure function on event list).
  - End-to-end CLI (CliRunner writes HTML, sanity-check shape).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from sqrlly.cli.main import cli
from sqrlly.cli.view import (
    compute_node_status,
    extract_node_config,
    read_jsonl_log,
    render_mermaid,
    render_view,
)
from sqrlly.schema.models import Graph

from helpers import make_config


def _node(id, **overrides):
    base = {
        "id": id,
        "name": id.upper(),
        "run": ["/usr/bin/echo"],
        **overrides,
    }
    return base


# ---------------------------------------------------------------------------
# Mermaid emitter
# ---------------------------------------------------------------------------


class TestMermaidEmitter:
    def test_linear_chain_emits_spine_and_subgraph(self):
        config = make_config([
            _node("a"),
            _node("b", depends_on=["a"]),
            _node("c", depends_on=["b"]),
        ])
        out = render_mermaid(config)

        # Header + endpoints
        assert out.startswith("flowchart TB")
        assert "START([START])" in out
        assert "END([END])" in out

        # Invisible spine MUST be emitted before subgraph contents so
        # dagre uses it for layout direction.
        spine_idx = out.index("START ~~~ workflow")
        subgraph_idx = out.index("subgraph workflow")
        assert spine_idx < subgraph_idx

        # All nodes inside the subgraph block.
        for nid in ("a", "b", "c"):
            assert f'{nid}["A"]' in out or f'{nid}["B"]' in out or f'{nid}["C"]' in out

        # Real edges
        assert "a --> b" in out
        assert "b --> c" in out

        # Entry + terminal hookups to spine endpoints
        assert "START --> a" in out
        assert "c --> END" in out

    def test_direction_lr_propagates(self):
        config = make_config([_node("a")])
        out = render_mermaid(config, direction="LR")
        assert out.startswith("flowchart LR")

    def test_direction_invalid_raises(self):
        config = make_config([_node("a")])
        with pytest.raises(ValueError, match="must be one of"):
            render_mermaid(config, direction="ZZ")

    def test_route_only_node_uses_diamond_shape(self):
        """`gate` has route but no execute → diamond."""
        config = make_config([
            _node("trigger"),
            {
                "id": "gate",
                "name": "Gate",
                "depends_on": ["trigger"],
                "route": [{"when": "true", "then": "trigger"}, {"else": "fail"}],
            },
        ])
        out = render_mermaid(config)
        assert 'gate{"Gate"}' in out

    def test_repeating_node_displays_source_and_direct_joins(self):
        config = make_config([
            _node("plan"),
            _node("build", depends_on=["plan"], for_each="plan"),
            _node("publish", depends_on=["build"]),
            _node("archive", depends_on=["build"]),
        ])
        diagram = render_mermaid(config)
        assert 'build{{' in diagram
        assert 'build__items[/"×N from plan"/]:::fanout' in diagram
        assert 'build -->|"1→N"| build__items' in diagram
        assert 'build__items -->|"N→1"| publish' in diagram
        assert 'build__items -->|"N→1"| archive' in diagram
        assert extract_node_config(config.nodes[1])["for_each"] == "plan"

    def test_static_source_uses_named_file_even_when_empty(self, tmp_path):
        (tmp_path / "items.json").write_text("[]")
        workflow = tmp_path / "workflow.yaml"
        workflow.write_text("nodes: [{id: build, run: echo, for_each: {file: items.json}}]")
        result = CliRunner().invoke(cli, ["view", str(workflow), "--out", str(tmp_path / "view.html")])
        assert result.exit_code == 0, result.output
        html = (tmp_path / "view.html").read_text()
        assert "×N from items.json" in html
        assert "build__items --> END" in html

    def test_subgraph_reference_uses_subroutine_shape(self):
        config = make_config([
            {
                "id": "sub_node",
                "name": "Sub",
                "workflow": "examples/foo/workflow.yaml",
            },
        ])
        out = render_mermaid(config)
        assert 'sub_node[["Sub"]]' in out

    def test_gated_node_gets_class_marker(self):
        config = make_config([
            {
                "id": "gated",
                "name": "Gated",
                "run": ["/usr/bin/echo"],
                "eval": {"run": ["/usr/bin/true"]},
            },
        ])
        out = render_mermaid(config)
        assert ":::gated" in out
        assert "classDef gated" in out

    def test_route_case_includes_predicate_label(self):
        config = make_config([
            _node("source"),
            {
                "id": "router",
                "name": "Router",
                "depends_on": ["source"],
                "route": [{"when": "source == 'spam'", "then": "drop"}, {"else": "keep"}],
            },
            _node("drop"),
            _node("keep"),
        ])
        out = render_mermaid(config)
        assert "router -.->" in out
        assert "source == 'spam'" in out
        assert '|"else"|' in out

    def test_route_to_end_renders_arrow_to_END(self):
        config = make_config([
            _node("a"),
            {
                "id": "decide",
                "name": "Decide",
                "depends_on": ["a"],
                "route": [{"when": "true", "then": "a"}, {"else": "fail"}],
            },
        ])
        out = render_mermaid(config)
        assert "decide -.->|\"else: fail\"| END" in out

    def test_unconditional_goto_uses_dotted_arrow_no_label(self):
        config = make_config([
            _node("step_one", route=[{"then": "step_two"}]),
            _node("step_two"),
        ])
        out = render_mermaid(config)
        assert "step_one -.-> step_two" in out

    def test_conditional_retry_renders_entry_and_fallback(self):
        config = make_config([
            _node("build", route=[{"when": "attempt < 3", "then": "retry"}]),
        ])
        diagram = render_mermaid(config)
        assert "START --> build" in diagram
        assert 'build -.->|"attempt < 3: retry"| build' in diagram
        assert "build --> END" in diagram

    def test_wave_pattern_dispatcher_loop_renders(self):
        """Simulate the wave_planner topology: gate routes back to a
        depends_on-fed parent. Both edges should be present."""
        config = make_config([
            _node("planner"),
            {
                "id": "dispatcher",
                "name": "Dispatcher",
                "depends_on": ["planner"],
                "run": ["/usr/bin/echo"],
                "for_each": "planner",
            },
            _node("reconcile", depends_on=["dispatcher"]),
            {
                "id": "gate",
                "name": "Gate",
                "depends_on": ["reconcile"],
                "route": [{"when": "'added' in reconcile", "then": "dispatcher"}, {"else": "fail"}],
            },
        ])
        out = render_mermaid(config)
        # Dep chain — reconcile consumes the branch aggregate, so its
        # edge fans in from the children stand-in (N→1), not the parent.
        assert "planner --> dispatcher" in out
        assert 'dispatcher__items -->|"N→1"| reconcile' in out
        assert "reconcile --> gate" in out
        # Loop-back
        assert "gate -.->" in out and "dispatcher" in out
        # Exit
        assert "gate -.->|\"else: fail\"| END" in out
        # Entry
        assert "START --> planner" in out


# ---------------------------------------------------------------------------
# Per-node config extraction
# ---------------------------------------------------------------------------


class TestExtractNodeConfig:
    def test_minimal_node(self):
        config = make_config([_node("a")])
        node = config.nodes[0]
        cfg = extract_node_config(node)
        assert cfg["id"] == "a"
        assert cfg["run"] == {"cmd": ["/usr/bin/echo"]}
        # Defaults shouldn't pollute the panel.
        assert "eval" not in cfg
        assert "for_each" not in cfg

    def test_evaluation_block_surfaces(self):
        config = make_config([
            {
                "id": "g",
                "name": "G",
                "run": ["/usr/bin/echo"],
                "eval": {"run": ["/usr/bin/true"]},
            },
        ])
        cfg = extract_node_config(config.nodes[0])
        assert cfg["eval"] == {"run": {"cmd": ["/usr/bin/true"]}}


# ---------------------------------------------------------------------------
# Status overlay
# ---------------------------------------------------------------------------


class TestComputeNodeStatus:
    def _config(self):
        return make_config([_node("a"), _node("b"), _node("c")])

    def test_no_log_marks_all_untouched(self):
        config = self._config()
        statuses = compute_node_status(config, None)
        assert all(s.status == "untouched" for s in statuses.values())
        assert {"a", "b", "c"} == set(statuses.keys())

    def test_passed_failed_untouched_classification(self):
        config = self._config()
        events = [
            {"event": "node_completed", "node": "a", "ts": "t1"},
            {"event": "node_failed", "node": "b", "ts": "t2", "error": "boom"},
        ]
        statuses = compute_node_status(config, events)
        assert statuses["a"].status == "passed"
        assert statuses["a"].fired_count == 1
        assert statuses["b"].status == "failed"
        assert statuses["b"].last_error == "boom"
        assert statuses["c"].status == "untouched"

    def test_retry_count_accumulates_status_from_terminal(self):
        config = self._config()
        events = [
            {"event": "node_retried", "node": "a", "ts": "t1", "attempt": 1},
            {"event": "node_retried", "node": "a", "ts": "t2", "attempt": 2},
            {"event": "node_completed", "node": "a", "ts": "t3"},
        ]
        statuses = compute_node_status(config, events)
        assert statuses["a"].retry_count == 2
        assert statuses["a"].status == "passed"

    def test_goto_re_fire_increments_fired_count(self):
        """Wave-pattern dispatcher: fires twice, both succeed."""
        config = self._config()
        events = [
            {"event": "node_completed", "node": "a", "ts": "t1"},
            {"event": "node_completed", "node": "a", "ts": "t2"},
        ]
        statuses = compute_node_status(config, events)
        assert statuses["a"].fired_count == 2
        assert statuses["a"].status == "passed"

    def test_subgraph_prefixed_node_id_preserved(self):
        """Fan-out children appear as parent::child; status is tracked
        but topology renderer ignores them (they're not in graph.nodes)."""
        config = self._config()
        events = [
            {"event": "node_completed", "node": "a::child_1", "ts": "t1"},
        ]
        statuses = compute_node_status(config, events)
        assert "a::child_1" in statuses
        assert statuses["a::child_1"].status == "passed"


# ---------------------------------------------------------------------------
# JSONL log reader
# ---------------------------------------------------------------------------


class TestReadJsonlLog:
    def test_skips_blank_and_invalid_lines(self, tmp_path):
        log = tmp_path / "out.jsonl"
        log.write_text(
            '{"event": "node_completed", "node": "a"}\n'
            '\n'
            'not-json\n'
            '{"event": "node_failed", "node": "b"}\n'
        )
        events = read_jsonl_log(log)
        assert len(events) == 2
        assert events[0]["node"] == "a"
        assert events[1]["node"] == "b"


# ---------------------------------------------------------------------------
# Full HTML render
# ---------------------------------------------------------------------------


class TestRenderView:
    def test_authoring_mode_omits_status_overlay_text(self):
        config = make_config([_node("a"), _node("b", depends_on=["a"])])
        html = render_view(config, events=None)
        assert "<title>" in html
        assert "authoring mode" in html
        assert "debug mode" not in html
        # Mermaid source embedded
        assert "flowchart TB" in html
        # Payload present
        assert '"has_log": false' in html

    def test_debug_mode_marks_badge(self):
        config = make_config([_node("a")])
        events = [{"event": "node_completed", "node": "a"}]
        html = render_view(config, events=events)
        assert "debug mode" in html
        assert '"has_log": true' in html

    def test_payload_contains_node_configs_and_statuses(self):
        config = make_config([_node("a"), {"id": "b", "llm": {"text": "Describe </script>."}}])
        events = [{"event": "node_completed", "node": "a"}]
        html = render_view(config, events=events)
        # Extract the embedded JSON
        marker = 'id="sqrlly-payload" type="application/json">'
        start = html.index(marker) + len(marker)
        end = html.index("</script>", start)
        payload = json.loads(html[start:end])
        assert "a" in payload["node_configs"]
        assert payload["node_configs"]["b"]["llm"] == {"text": "Describe </script>."}
        assert payload["statuses"]["a"]["status"] == "passed"
        assert payload["statuses"]["b"]["status"] == "untouched"


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestViewCommand:
    def test_view_writes_html_authoring_mode(self, tmp_path):
        yaml_path = tmp_path / "wf.yaml"
        yaml_path.write_text(
            "name: Test\nnodes:\n"
            "  - id: a\n    name: A\n"
            "    run: [/usr/bin/echo, hi]\n"
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["view", str(yaml_path), "--workdir", str(tmp_path)],
        )
        assert result.exit_code == 0, result.output

        out = tmp_path / "sqrlly-view.html"
        assert out.exists()
        text = out.read_text()
        assert "<title>Test" in text
        assert "authoring mode" in text

    def test_view_with_log_marks_debug_mode(self, tmp_path):
        yaml_path = tmp_path / "wf.yaml"
        yaml_path.write_text(
            "name: Test\nnodes:\n"
            "  - id: a\n    name: A\n"
            "    run: [/usr/bin/echo]\n"
        )
        log_path = tmp_path / "out.jsonl"
        log_path.write_text(
            '{"event": "node_completed", "node": "a"}\n'
        )

        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "view", str(yaml_path),
                "--log", str(log_path),
                "--workdir", str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output

        out_html = (tmp_path / "sqrlly-view.html").read_text()
        assert "debug mode" in out_html

    def test_view_explicit_out_path(self, tmp_path):
        yaml_path = tmp_path / "wf.yaml"
        yaml_path.write_text(
            "name: Test\nnodes:\n"
            "  - id: a\n    name: A\n"
            "    run: [/usr/bin/echo]\n"
        )
        out = tmp_path / "custom.html"

        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["view", str(yaml_path), "--out", str(out)],
        )
        assert result.exit_code == 0
        assert out.exists()
        # Existence alone could pass against a regression that wrote
        # an empty file; pin that the output is a real rendered HTML.
        text = out.read_text()
        assert "<title>" in text
        assert "Test" in text  # workflow name embedded in title
