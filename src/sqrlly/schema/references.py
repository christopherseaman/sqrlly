"""Resolve output references against the variables available at each workflow step."""

from __future__ import annotations

import ast
from collections.abc import Collection
from typing import TYPE_CHECKING

from jinja2 import Environment, meta, nodes

if TYPE_CHECKING:
    from sqrlly.schema.models import Graph

_TEMPLATE = Environment()
_BUILTINS = {
    "workflow_inputs", "workflow_dir", "_deps", "_dep_worktrees", "evals", "sender_id", "sender",
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


def split_manifest_source(source: str, ancestors: Collection[str]) -> tuple[str, list[str]]:
    """Prefer an exact ancestor id, then the longest id before a field path."""
    if source in ancestors:
        return source, []
    root = max((name for name in ancestors if source.startswith(name + ".")), key=len, default=None)
    if root is None:
        raise ValueError(f"for_each {source!r} requires an ancestor or its field")
    fields = source[len(root) + 1:].split(".")
    if not all(fields):
        raise ValueError(f"for_each {source!r} contains an empty field name")
    return root, fields


def assign_ancestors(graph: Graph) -> None:
    by_id = {node.id: node for node in graph.nodes}
    parents = {node.id: set(node.depends_on) for node in graph.nodes}
    for node, _, target, backwards in iter_route_edges(graph):
        if not backwards and target in parents:
            parents[target].add(node.id)
    for node in graph.nodes:
        node._ancestors = sorted(_ancestors(parents, node.id))
        if isinstance(node.for_each, str):
            source, path = split_manifest_source(node.for_each, node._ancestors)
            producer = by_id[source]
            if path and producer.for_each is not None:
                raise ValueError(f"{node.id}: {source} is a for_each result list and has no named fields")
            if path and not (producer.outputs and producer.outputs.schema_file):
                raise ValueError(f"{node.id}: for_each {node.for_each!r} requires outputs.schema on {source}")


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


def validate_references(graph: Graph, inputs: frozenset[str] | None) -> None:
    input_names = inputs or frozenset()
    by_id = {node.id: node for node in graph.nodes}
    for node in graph.nodes:
        upstream = set(node._ancestors)
        allowed = _BUILTINS | input_names | {
            f"{dep}_{suffix}" for dep in upstream
            for suffix in ("structured", "worktree")
        }

        def check(name, scope, node_ids, *, field=False, whole=False):
            if name not in scope:
                raise ValueError(f"{node.id}: {name} is not an ancestor or available input")
            source = by_id[name] if name in node_ids else None
            if source and source.for_each is not None:
                if field:
                    raise ValueError(f"{node.id}: {name} is a for_each result list and has no named fields")
                return
            if source and not (source.outputs and source.outputs.schema_file):
                if field:
                    raise ValueError(f"{node.id}: {name} field access requires outputs.schema")
                if whole and source.kind in ("agent", "llm"):
                    raise ValueError(f"{node.id}: comparing {name} text requires outputs.schema")

        def check_template(template, scope, node_ids):
            tree = _TEMPLATE.parse(template)
            free = meta.find_undeclared_variables(tree)
            for name in sorted(free):
                check(name, scope, node_ids)
            for get in tree.find_all((nodes.Getattr, nodes.Getitem)):
                if isinstance(get.node, nodes.Name) and get.node.name in free:
                    if get.node.name == "workflow_inputs" and get.node.name not in node_ids and inputs is not None:
                        key = get.attr if isinstance(get, nodes.Getattr) else (
                            get.arg.value if isinstance(get.arg, nodes.Const) else None)
                        if key is not None and key not in inputs:
                            raise ValueError(f"{node.id}: workflow.with does not supply {key!r}")
                    indexed_list = (isinstance(get, nodes.Getitem) and
                                    not (isinstance(get.arg, nodes.Const) and isinstance(get.arg.value, str)))
                    source = by_id.get(get.node.name)
                    if not (indexed_list and source and source.for_each is not None):
                        check(get.node.name, scope, node_ids, field=True)

        node_ids = upstream
        scope = allowed | upstream
        if node.for_each is not None:
            scope.add("item")
            node_ids = upstream - {"item"}
        for template in _templates(node, graph):
            check_template(template, scope, node_ids)
        own_output = {node.id}
        eval_scope = scope | own_output | {"output", "node_output", "node_id", "attempt"}
        if node.eval:
            for template in _templates(node.eval, graph):
                check_template(template, eval_scope, node_ids | own_output)
        for case in node.route or []:
            if case.context not in {"none", "all", "last"}:
                check_template(case.context, eval_scope | {"history", "eval"}, node_ids | own_output)
            if not case.when:
                continue
            tree = ast.parse(case.when, mode="eval")
            route_scope = input_names | upstream | own_output | _ROUTE_NAMES
            for expr in ast.walk(tree):
                if isinstance(expr, ast.Name):
                    check(expr.id, route_scope, upstream | own_output)
                if isinstance(expr, (ast.Attribute, ast.Subscript)) and isinstance(expr.value, ast.Name):
                    indexed_list = isinstance(expr, ast.Subscript) and not (
                        isinstance(expr.slice, ast.Constant) and isinstance(expr.slice.value, str))
                    source = by_id.get(expr.value.id)
                    if not (indexed_list and source and source.for_each is not None):
                        check(expr.value.id, route_scope, upstream | own_output, field=True)
                if isinstance(expr, ast.Compare):
                    for operand in (expr.left, *expr.comparators):
                        if isinstance(operand, ast.Name):
                            check(operand.id, route_scope, upstream | own_output, whole=True)
