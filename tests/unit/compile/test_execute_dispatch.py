"""Compiler integration for workflow calls, joins and routes."""


from __future__ import annotations

import shutil

import pytest

from sqrlly.compile.graph import (
    _is_route,
    build_workflow_graph,
)
from sqrlly.compile.subgraph import SubgraphCycleError, node_subgraph_path
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import (
    Graph,
    Node,
    Route,
    RouteCase,
    RouteElse,
)

_ECHO = shutil.which("echo") or "/bin/echo"


class TestSubgraphRefDetection:
    def test_execute_yaml_recognized(self):
        n = Node(id="a", name="A", workflow="sub.yaml")
        assert (node_subgraph_path(n) is not None) is True
        assert node_subgraph_path(n) == "sub.yaml"

    def test_execute_yml_recognized(self):
        n = Node(id="a", name="A", workflow="sub.yml")
        assert (node_subgraph_path(n) is not None) is True

    def test_execute_md_not_subgraph(self):
        n = Node(id="a", name="A", agent={"text": "test"})
        assert (node_subgraph_path(n) is not None) is False
        assert node_subgraph_path(n) is None

    def test_no_execute_not_subgraph(self):
        n = Node(id="a", name="A")
        assert (node_subgraph_path(n) is not None) is False


class TestRouteDetection:
    def test_inline_route_recognized(self):
        n = Node(
            id="r", name="R",
            route=Route(
                cases=[RouteCase(when="x > 0", goto="ship")],
                else_=RouteElse(goto="produce"),
            ),
        )
        assert _is_route(n) is True
        assert n.route.cases[0].when == "x > 0"
        assert n.route.else_.goto == "produce"

    def test_url_node_not_route(self):
        n = Node(id="a", name="A", agent={"text": "test"})
        assert _is_route(n) is False

    def test_no_execute_not_route(self):
        n = Node(id="a", name="A")
        assert _is_route(n) is False


class TestWorkflowCalls:
    """Compile and execute child workflows."""

    @pytest.mark.asyncio
    async def test_compiles_workflow_file(self, tmp_path):
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "name: sub\n"
            "nodes:\n"
            "  - id: inner\n    name: Inner\n"
            f"    run: [{_ECHO}, "
            "'from-sub']\n"
        )
        config = Graph(
            name="parent",
            nodes=[
                Node(id="p", name="P", workflow="sub.yaml"),
            ],
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(
            config, executor, _base_dir=tmp_path,
        )
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p" in result["completed_nodes"]
        assert "from-sub" in result["node_outputs"]["p"]

    @pytest.mark.asyncio
    async def test_workflow_with_inputs_returns_terminal_output(self, tmp_path):
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "name: sub\n"
            "nodes:\n"
            "  - id: step1\n    name: Step1\n"
            f"    run: [{_ECHO}, "
            "'{{topic}}']\n"
            "  - id: step2\n    name: Step2\n    depends_on: [step1]\n"
            f"    run: [{_ECHO}, "
            "'final-{{step1}}']\n"
        )
        producer = Node(
            id="producer", name="Producer",
            run=[_ECHO, "alpha"],
        )
        wrapper = Node(
            id="p", name="P", depends_on=["producer"],
            workflow={"file": "sub.yaml", "with": {"topic": "{{producer}}"}},
        )
        config = Graph(name="parent", nodes=[producer, wrapper])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor, _base_dir=tmp_path)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p" in result["completed_nodes"]
        assert "final-alpha" in result["node_outputs"]["p"]


    @pytest.mark.asyncio
    async def test_workflow_timeout_fails_parent_node(self, tmp_path):
        import sys

        import yaml

        (tmp_path / "slow.yaml").write_text(yaml.safe_dump({"nodes": [{
            "id": "child", "run": [sys.executable, "-c", "import time; time.sleep(5)"],
        }]}))
        config = Graph(nodes=[Node(id="parent", workflow="slow.yaml", timeout=0.1)])
        graph = build_workflow_graph(config, DispatchExecutor(str(tmp_path)), _base_dir=tmp_path)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert result["failed_nodes"] == {"parent"}
        assert result["errors"][0]["kind"] == "timeout"


class TestCycleDetectionAcrossShapes:
    def test_cycle_through_workflow_references(self, tmp_path):
        a = tmp_path / "a.yaml"
        b = tmp_path / "b.yaml"
        a.write_text(
            "name: a\n"
            "nodes:\n"
            "  - id: ref\n    name: R\n"
            "    workflow: b.yaml\n"
        )
        b.write_text(
            "name: b\n"
            "nodes:\n"
            "  - id: ref\n    name: R\n"
            "    workflow: a.yaml\n"
        )
        config = Graph(
            name="parent",
            nodes=[Node(id="p", name="P", workflow="a.yaml")],
        )
        with pytest.raises(SubgraphCycleError):
            build_workflow_graph(config, executor=None, _base_dir=tmp_path)


class TestRouteCompileViaInlineRoute:
    """A standalone route authored as inline ``Node.route`` (no execute
    body) compiles and dispatches via Command(goto=). Stage 5c shape."""

    @pytest.mark.asyncio
    async def test_inline_route_dispatches(self, tmp_path):
        config = Graph(
            name="t",
            nodes=[
                Node(
                    id="produce", name="produce",
                    run=[_ECHO, "draft"],
                ),
                Node(
                    id="decide", name="decide", depends_on=["produce"],
                    route=Route(
                        cases=[RouteCase(when="True", goto="ship")],
                        else_=RouteElse(goto="__end__"),
                    ),
                ),
                Node(
                    id="ship", name="ship",
                    run=[_ECHO, "shipped"],
                ),
            ],
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor, _base_dir=tmp_path)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "ship" in result["completed_nodes"]
        assert "shipped" in result["node_outputs"]["ship"]


class TestJoin:
    """A node without a kind joins its dependencies."""

    @pytest.mark.asyncio
    async def test_join_runs_after_deps(self, tmp_path):
        config = Graph(
            name="t",
            nodes=[
                Node(
                    id="a", name="A",
                    run=[_ECHO, "alpha"],
                ),
                Node(
                    id="b", name="B",
                    run=[_ECHO, "beta"],
                ),
                Node(
                    id="merge", name="Merge",
                    depends_on=["a", "b"],
                ),
            ],
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor, _base_dir=tmp_path)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "merge" in result["completed_nodes"]
        assert "a" in result["completed_nodes"]
        assert "b" in result["completed_nodes"]
