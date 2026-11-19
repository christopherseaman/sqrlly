"""Resolve output references against the variables available at each workflow step."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from jinja2 import Environment, meta, nodes

if TYPE_CHECKING:
    from sqrlly.schema.models import Graph

_TEMPLATE = Environment()
_BUILTINS = {
    "workflow_dir", "_deps", "_dep_worktrees", "evals", "sender_id", "sender",
    "sender_structured", "sender_worktree", "_retry_reason", "_route_eval_preamble",
    "_output_retry_reason",
}
_ROUTE_NAMES = {
    "state", "history", "evals", "passed", "score", "scores", "len", "any", "all",
    "min", "max", "sum", "eval", "attempt",
}


def _ancestors(parents, node_id):
    seen = {node_id}
    pending = list(parents[node_id])
    while pending:
        ancestor = pending.pop()
        if ancestor not in seen:
            seen.add(ancestor)
            pending.extend(parents.get(ancestor, ()))
    return seen - {node_id}


def iter_route_edges(graph: Graph):
    """Classify routes against dependencies and preceding forward routes."""
    parents = {node.id: set(node.depends_on) for node in graph.nodes}
    for node in graph.nodes:
        upstream = _ancestors(parents, node.id) | {node.id}
        for case in node.route or []:
            for target in case.targets:
                backwards = target in upstream
                yield node, case, target, backwards
                if not backwards and target in parents:
                    parents[target].add(node.id)


def assign_ancestors(graph: Graph) -> None:
    parents = {node.id: set(node.depends_on) for node in graph.nodes}
    for node, _, target, backwards in iter_route_edges(graph):
        if not backwards and target in parents:
            parents[target].add(node.id)
    for node in graph.nodes:
        node._ancestors = sorted(_ancestors(parents, node.id))


def _templates(step, graph):
    prompt = step.agent or step.llm
    if prompt:
        if prompt.text is not None:
            yield prompt.text
        elif graph.workflow_dir:
            yield (graph.workflow_dir / graph.settings.prompts / prompt.prompt).read_text()
    if step.run:
        yield from step.run.cmd
        yield from step.run.env.values()
    if workflow := getattr(step, "workflow", None):
        yield from (v for v in workflow.inputs.values() if isinstance(v, str))


def validate_references(graph: Graph, inputs: frozenset[str]) -> None:
    by_id = {node.id: node for node in graph.nodes}
    for node in graph.nodes:
        upstream = set(node._ancestors)
        allowed = _BUILTINS | inputs | {
            f"{dep}_{suffix}" for dep in upstream
            for suffix in ("structured", "worktree", "branches", "branch_worktrees", "branch_map")
        }

        def check(name, scope, *, field=False, whole=False):
            if name not in scope:
                raise ValueError(f"{node.id}: {name} is not an ancestor or available input")
            source = by_id.get(name)
            if source and not (source.outputs and source.outputs.schema_file):
                if field:
                    raise ValueError(f"{node.id}: {name} field access requires outputs.schema")
                if whole and source.kind in ("agent", "llm"):
                    raise ValueError(f"{node.id}: comparing {name} text requires outputs.schema")

        def check_template(template, scope, item_fields):
            tree = _TEMPLATE.parse(template)
            free = meta.find_undeclared_variables(tree)
            local = scope | (free - by_id.keys() if item_fields else set())
            for name in sorted(free):
                check(name, local)
            for get in tree.find_all((nodes.Getattr, nodes.Getitem)):
                if isinstance(get.node, nodes.Name) and get.node.name in free:
                    check(get.node.name, local, field=True)

        def check_step(step, scope, *, item_fields=False):
            for template in _templates(step, graph):
                check_template(template, scope, item_fields)
            own_output = {node.id} if step is node else set()
            eval_scope = scope | own_output | {"output", "node_output", "node_id", "attempt"}
            if step.eval:
                for template in _templates(step.eval, graph):
                    check_template(template, eval_scope, item_fields)
            for case in step.route or []:
                if case.context not in {"none", "all", "last"}:
                    check_template(case.context, eval_scope | {"history", "eval"}, item_fields)
                if not case.when:
                    continue
                tree = ast.parse(case.when, mode="eval")
                route_scope = inputs | upstream | own_output | _ROUTE_NAMES
                for expr in ast.walk(tree):
                    if isinstance(expr, ast.Name):
                        check(expr.id, route_scope)
                    if isinstance(expr, (ast.Attribute, ast.Subscript)) and isinstance(expr.value, ast.Name):
                        check(expr.value.id, route_scope, field=True)
                    if isinstance(expr, ast.Compare):
                        for operand in (expr.left, *expr.comparators):
                            if isinstance(operand, ast.Name):
                                check(operand.id, route_scope, whole=True)

        check_step(node, allowed | upstream)
        if node.fan_out:
            branch_scope = allowed | upstream | {node.id}
            if node.fan_out.template:
                check_step(node.fan_out.template, branch_scope, item_fields=True)
            for final in node.fan_out.final_nodes:
                suffixes = ("branches", "branch_worktrees", "branch_map")
                check_step(final, branch_scope | {f"{node.id}_{suffix}" for suffix in suffixes})
