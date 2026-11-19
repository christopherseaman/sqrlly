"""Validate workflow template and predicate references in their execution scope."""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from jinja2 import Environment, meta, nodes

if TYPE_CHECKING:
    from sqrlly.schema.models import Graph, KindStep

_TEMPLATE = Environment()
_BUILTINS = {
    "workflow_dir", "_deps", "_dep_worktrees", "evals", "sender_id", "sender",
    "sender_structured", "sender_worktree", "_retry_reason", "_route_eval_preamble",
    "_output_retry_reason",
}
_ROUTE_NAMES = {"state", "history", "evals", "passed", "score", "scores", "len", "any", "all", "min", "max", "sum", "eval", "attempt"}


def assign_ancestors(graph: Graph) -> None:
    parents = {node.id: set(node.depends_on) for node in graph.nodes}
    for node in graph.nodes:
        if node.route:
            targets = [node.route.goto, *(case.goto for case in node.route.cases)]
            if node.route.else_:
                targets.append(node.route.else_.goto)
            for target in targets:
                for dest in target if isinstance(target, list) else [target]:
                    if dest in parents:
                        parents[dest].add(node.id)
    for node in graph.nodes:
        seen = {node.id}
        pending = list(parents[node.id])
        while pending:
            ancestor = pending.pop()
            if ancestor not in seen:
                seen.add(ancestor)
                pending.extend(parents.get(ancestor, ()))
        node._ancestors = sorted(seen - {node.id})


def validate_references(graph: Graph, inputs: frozenset[str]) -> None:
    by_id = {node.id: node for node in graph.nodes}
    for node in graph.nodes:
        ancestors = set(node._ancestors)
        allowed = _BUILTINS | inputs | {
            f"{dep}_{suffix}" for dep in ancestors
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

        def check_step(step: KindStep, scope, item_fields=False):
            templates = []
            prompt = step.agent or step.llm
            if prompt:
                if prompt.text is not None:
                    templates.append(prompt.text)
                elif graph.workflow_dir:
                    templates.append((graph.workflow_dir / graph.settings.prompts / prompt.prompt).read_text())
            if step.run:
                templates.extend([*step.run.cmd, *step.run.env.values()])
            if step.workflow:
                templates.extend(v for v in step.workflow.inputs.values() if isinstance(v, str))
            for template in templates:
                tree = _TEMPLATE.parse(template)
                free = meta.find_undeclared_variables(tree)
                local_scope = scope | (free - by_id.keys() if item_fields else set())
                for name in sorted(free):
                    check(name, local_scope)
                for get in tree.find_all((nodes.Getattr, nodes.Getitem)):
                    if isinstance(get.node, nodes.Name) and get.node.name in free:
                        check(get.node.name, local_scope, field=True)

        check_step(node, allowed | ancestors)
        if node.fan_out:
            branch_scope = allowed | ancestors | {node.id}
            if node.fan_out.template:
                check_step(node.fan_out.template, branch_scope, item_fields=True)
            for final in node.fan_out.final_nodes:
                check_step(final, branch_scope | {f"{node.id}_{suffix}" for suffix in ("branches", "branch_worktrees", "branch_map")})
        if node.route:
            scope = ancestors | {node.id} | _ROUTE_NAMES
            for case in node.route.cases:
                tree = ast.parse(case.when, mode="eval")
                for expr in ast.walk(tree):
                    if isinstance(expr, ast.Name):
                        check(expr.id, scope)
                    if isinstance(expr, (ast.Attribute, ast.Subscript)) and isinstance(expr.value, ast.Name):
                        check(expr.value.id, scope, field=True)
                    if isinstance(expr, ast.Compare):
                        for operand in (expr.left, *expr.comparators):
                            if isinstance(operand, ast.Name):
                                check(operand.id, scope, whole=True)
