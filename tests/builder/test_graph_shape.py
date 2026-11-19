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


class TestRepeatedNodeShape:
    def test_producer_worker_and_join_compile(self):
        config = make_config([
            {"id": "plan", "run": ["echo", "[]"]},
            {"id": "build", "depends_on": ["plan"], "for_each": "plan", "run": ["echo", "{{item}}"]},
            {"id": "report", "depends_on": ["build"], "run": ["echo", "done"]},
        ])
        edges = _edges(build_workflow_graph(config))
        assert (START, "plan") in edges
        assert (START, "build") not in edges
        assert (START, "report") not in edges


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
        with pytest.raises(ValueError, match="depends_on cycle"):
            config = make_config(
                [
                    {"id": "a", "name": "A", "agent": {"text": "test"}, "depends_on": ["b"]},
                    {"id": "b", "name": "B", "agent": {"text": "test"}, "depends_on": ["a"]},
                ]
            )
            build_workflow_graph(config)

    def test_three_node_cycle_rejected(self):
        with pytest.raises(ValueError, match="depends_on cycle"):
            config = make_config(
                [
                    {"id": "a", "name": "A", "agent": {"text": "test"}, "depends_on": ["c"]},
                    {"id": "b", "name": "B", "agent": {"text": "test"}, "depends_on": ["a"]},
                    {"id": "c", "name": "C", "agent": {"text": "test"}, "depends_on": ["b"]},
                ]
            )
            build_workflow_graph(config)
