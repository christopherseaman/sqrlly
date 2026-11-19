import pytest
from langgraph.graph import END, START

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.schema.models import Graph

from helpers import make_config


def _edges(graph):
    """Return (source, target) tuples for every edge."""
    return {(e.source, e.target) for e in graph.get_graph().edges}


def _conditional_edges(graph):
    return {(e.source, e.target) for e in graph.get_graph().edges if e.conditional}


class TestSingleNodeGraph:
    def test_single_prompt_node(self):
        config = make_config(
            [{"id": "p1", "name": "P1", "agent": {"text": "test"}}]
        )
        graph = build_workflow_graph(config)
        assert "p1" in graph.get_graph().nodes
        assert _edges(graph) == {(START, "p1"), ("p1", END)}

    def test_single_command_node(self):
        config = make_config(
            [
                {
                    "id": "c1",
                    "name": "C1",
                    "run": ["/usr/bin/echo", "hello"],
                }
            ]
        )
        graph = build_workflow_graph(config)
        assert "c1" in graph.get_graph().nodes
        assert _edges(graph) == {(START, "c1"), ("c1", END)}

    def test_single_gate_only_node(self):
        """Gated node compiles to exec → _eval_<id> (one plain edge). The
        gate node returns Command(goto=...) at runtime; no _decide_<id>
        node, no static conditional edges remain."""
        config = make_config(
            [
                {
                    "id": "g1",
                    "name": "G1",
                    # gate-only-by-elision: no execute: block
                    "eval": {"run": ["true"]},
                }
            ]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "g1" in nodes
        assert "_eval_g1" in nodes
        assert "_decide_g1" not in nodes
        edges = _edges(graph)
        assert (START, "g1") in edges
        assert ("g1", "_eval_g1") in edges
        # No conditional edges from the gate — Command(goto=) replaces them.
        assert _conditional_edges(graph) == set()


class TestLinearChain:
    def test_two_phase_chain(self, multi_phase_config_dict):
        config = Graph(**multi_phase_config_dict)
        graph = build_workflow_graph(config)
        assert _edges(graph) == {
            (START, "node-1"),
            ("node-1", "node-2"),
            ("node-2", END),
        }

    def test_three_phase_chain(self):
        config = make_config(
            [
                {"id": "a", "name": "A", "agent": {"text": "test"}},
                {"id": "b", "name": "B", "agent": {"text": "test"}, "depends_on": ["a"]},
                {"id": "c", "name": "C", "agent": {"text": "test"}, "depends_on": ["b"]},
            ]
        )
        graph = build_workflow_graph(config)
        assert _edges(graph) == {
            (START, "a"),
            ("a", "b"),
            ("b", "c"),
            ("c", END),
        }


class TestParallelNodes:
    def test_diamond_dependency(self, parallel_config_dict):
        """A → (B, C) → D — both forks must be wired, and D must fan in from both."""
        config = Graph(**parallel_config_dict)
        graph = build_workflow_graph(config)
        assert _edges(graph) == {
            (START, "a"),
            ("a", "b"),
            ("a", "c"),
            ("b", "d"),
            ("c", "d"),
            ("d", END),
        }

    def test_multiple_roots(self):
        config = make_config(
            [
                {"id": "a", "name": "A", "agent": {"text": "test"}},
                {"id": "b", "name": "B", "agent": {"text": "test"}},
            ]
        )
        graph = build_workflow_graph(config)
        assert _edges(graph) == {
            (START, "a"),
            (START, "b"),
            ("a", END),
            ("b", END),
        }


class TestGateRouting:
    """Collapsed-gate wiring: a gated node compiles to
    ``exec → _eval_<id>`` with ONE plain edge. The gate node's
    ``Command(goto=...)`` replaces the static conditional-edge router;
    routing destinations are not visible in ``graph.get_graph().edges``
    because they're chosen at runtime. No ``_decide_<id>`` node exists.
    """

    def test_terminal_gate_wiring(self):
        config = make_config(
            [
                {
                    "id": "p1",
                    "name": "P1",
                    "agent": {"text": "test"},
                    "eval": {"run": ["true"]},
                }
            ]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_eval_p1" in nodes
        assert "_decide_p1" not in nodes
        edges = _edges(graph)
        assert (START, "p1") in edges
        assert ("p1", "_eval_p1") in edges
        # No conditional edges — Command(goto=) replaces them.
        assert _conditional_edges(graph) == set()

    def test_non_terminal_gate_wiring(self):
        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "agent": {"text": "test"},
                    "eval": {"run": ["true"]},
                },
                {
                    "id": "b",
                    "name": "B",
                    "agent": {"text": "test"},
                    "depends_on": ["a"],
                },
            ]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_eval_a" in nodes
        assert "_decide_a" not in nodes
        assert "b" in nodes  # b is reached via Command(goto=b) at runtime
        edges = _edges(graph)
        assert (START, "a") in edges
        assert ("a", "_eval_a") in edges
        # No conditional edges from the gate.
        assert _conditional_edges(graph) == set()
        # b's static `b → END` edge is added by the terminal-end wiring;
        # LangGraph may filter it from get_graph().edges when b has no
        # static incoming edges (its only path in is the runtime Command
        # from the _eval_a gate). Not asserting on `(b, END) in edges` —
        # downstream connectivity is verified end-to-end by e2e tests.

    def test_gate_with_multiple_dependents_fans_out(self):
        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "agent": {"text": "test"},
                    "eval": {"run": ["true"]},
                },
                {"id": "b", "name": "B", "agent": {"text": "test"}, "depends_on": ["a"]},
                {"id": "c", "name": "C", "agent": {"text": "test"}, "depends_on": ["a"]},
            ]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_after_a" not in nodes
        assert "_eval_a" in nodes
        assert "_decide_a" not in nodes
        assert "b" in nodes
        assert "c" in nodes

        edges = _edges(graph)
        assert ("a", "_eval_a") in edges
        # gate → [b, c] is a runtime Command(goto=[b, c]); no static
        # conditional edges. b/c terminal-END edges are added but
        # LangGraph may filter them from get_graph().edges (see
        # test_non_terminal_gate_wiring); end-to-end connectivity is
        # verified by e2e tests.
        assert _conditional_edges(graph) == set()


class TestInlineRouteShape:
    @pytest.mark.parametrize("kind", [{}, {"run": ["echo", "draft"]}, {"run": ["echo", "draft"], "eval": {"run": ["true"]}}])
    def test_route_after_body(self, kind):
        config = make_config([
            {"id": "classify", **kind, "route": [{"then": ["ship", "review"]}]},
            {"id": "ship", "run": ["true"]},
            {"id": "review", "run": ["true"]},
        ])
        graph = build_workflow_graph(config)
        edges = _edges(graph)
        assert (START, "classify") in edges
        assert ("classify", "_eval_classify") in edges
        assert (START, "ship") not in edges
        assert (START, "review") not in edges
        assert ("classify", END) not in edges


class TestEvaluationNodeShape:
    def test_ungated_phase_no_eval_node(self):
        config = make_config(
            [{"id": "p1", "name": "P1", "agent": {"text": "test"}}]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_eval_p1" not in nodes
        assert _edges(graph) == {(START, "p1"), ("p1", END)}


class TestDynamicGraphShape:
    def _dynamic_phase(self, template_evaluation=None):
        template = {"agent": {"text": "test"}}
        if template_evaluation is not None:
            template["eval"] = template_evaluation
        dsc = {
            "template": template,
            "final_nodes": [
                {"id": "f0", "name": "F0", "agent": {"text": "test"}},
            ],
        }
        return {
            "id": "p",
            "name": "P",
            "run": ["/usr/bin/echo", "manifest"],
            "fan_out": dsc,
        }

    def test_ungated_template_registers_template_and_final_nodes(self):
        config = make_config([self._dynamic_phase()])
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_sub_p" in nodes and "_final_p_f0" in nodes
        # Fan-out dispatch is a _fan_<id> node, not conditional edges.
        assert "_fan_p" in nodes
        edges = _edges(graph)
        # Ungated parent: plain edge parent → _fan_<id>.
        assert ("p", "_fan_p") in edges
        # No conditional edges for fan-out dispatch (Command(goto=[Send...])).
        assert _conditional_edges(graph) == set()

    def test_gated_template_registers_nodes_and_parent_fanout(self):
        # The evaluation is on the TEMPLATE (children), so the PARENT `p`
        # is ungated: exec → _fan_<id> plain edge, no _eval_p gate on the
        # parent. Child gates live inside the _sub_ inline retry loop.
        config = make_config([self._dynamic_phase(
            template_evaluation={"run": ["true"]},
        )])
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_sub_p" in nodes
        assert "_fan_p" in nodes
        assert "_eval_p" not in nodes  # parent ungated
        edges = _edges(graph)
        assert ("p", "_fan_p") in edges
        # No conditional edges anywhere — _fan_ is Command-driven.
        assert _conditional_edges(graph) == set()

    def _gated_parent_phase(self):
        """A fan-out whose PARENT carries the evaluation (parent-gate)."""
        return {
            "id": "p",
            "name": "P",
            "run": ["/usr/bin/echo", "manifest"],
            "eval": {"run": ["true"]},
            "fan_out": {
                "template": {"agent": {"text": "test"}},
                "final_nodes": [
                    {"id": "f0", "name": "F0", "agent": {"text": "test"}},
                ],
            },
        }

    def test_parent_gated_fanout_wires_gate_to_fan(self):
        """A parent-gated fan-out: exec → _eval_<id> gate, gate gotoes
        _fan_<id> on pass. No _decide_<id>, no conditional edges."""
        config = make_config([self._gated_parent_phase()])
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_eval_p" in nodes
        assert "_decide_p" not in nodes
        assert "_fan_p" in nodes
        edges = _edges(graph)
        assert ("p", "_eval_p") in edges
        assert _conditional_edges(graph) == set()


class TestCycleDetection:
    def test_self_dependency_rejected_at_schema(self):
        with pytest.raises(Exception, match="self-dependency"):
            make_config(
                [
                    {
                        "id": "a",
                        "name": "A",
                        "agent": {"text": "test"},
                        "depends_on": ["a"],
                    }
                ]
            )

    def test_circular_dependency_rejected(self):
        with pytest.raises(ValueError, match="[Cc]ircular"):
            config = make_config(
                [
                    {"id": "a", "name": "A", "agent": {"text": "test"}, "depends_on": ["b"]},
                    {"id": "b", "name": "B", "agent": {"text": "test"}, "depends_on": ["a"]},
                ]
            )
            build_workflow_graph(config)

    def test_three_node_cycle_rejected(self):
        with pytest.raises(ValueError, match="[Cc]ircular"):
            config = make_config(
                [
                    {"id": "a", "name": "A", "agent": {"text": "test"}, "depends_on": ["c"]},
                    {"id": "b", "name": "B", "agent": {"text": "test"}, "depends_on": ["a"]},
                    {"id": "c", "name": "C", "agent": {"text": "test"}, "depends_on": ["b"]},
                ]
            )
            build_workflow_graph(config)
