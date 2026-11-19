"""Compile-time lint warnings — non-fatal author footgun checks.

Distinct from schema validation (which raises ``ValidationError``) and
graph compilation (which builds the LangGraph): these are advisory
warnings about constructs that are *valid* but likely a mistake.

Pure functions over ``Graph`` — no langgraph imports, no I/O. Top-level
config only; subgraph configs are not loaded (a subgraph is linted when
run as its own top-level workflow).
"""
from __future__ import annotations

from sqrlly.schema.models import Graph
from sqrlly.schema.references import iter_route_edges


def collect_warnings(config: Graph) -> list[str]:
    """Advisory warnings for a workflow config, in declaration order."""
    return (
        _hyphenated_id_warnings(config)
        + _upstream_route_warnings(config)
        + _worktree_setup_exclude_warnings(config)
        + _shared_worktree_warnings(config)
    )


def _upstream_route_warnings(config: Graph) -> list[str]:
    return [
        f"{node.id}: route to upstream node {target!r} has no max_attempts"
        for node, case, target, is_upstream in iter_route_edges(config)
        if is_upstream and case.max_attempts is None
    ]


def _worktree_setup_exclude_warnings(config: Graph) -> list[str]:
    if config.settings.worktree_setup and not config.settings.promote_exclude:
        return ["settings.worktree_setup has no promote_exclude; generated files may be promoted"]
    return []


def _shared_worktree_warnings(config: Graph) -> list[str]:
    shared = [node for node in config.nodes if node.kind in ("agent", "run")
              and node.effective_worktree(config.settings, fan_child=node.for_each is not None) == "off"]
    return [
        f"{left.id} and {right.id} use worktree: off without a dependency path; both can write the base checkout"
        for index, left in enumerate(shared) for right in shared[index + 1:]
        if left.id not in right._ancestors and right.id not in left._ancestors
    ]


def _hyphenated_id_warnings(config: Graph) -> list[str]:
    """Flag node ids containing ``-``.

    A hyphenated id referenced as ``{{the-id}}`` in a Jinja template
    parses as subtraction (``the - id``), silently producing wrong or
    empty output rather than the node's value. Underscores are safe.
    """
    out: list[str] = []
    for node in config.nodes:
        node_id = node.id
        if "-" not in node_id:
            continue
        templated = "{{" + node_id + "}}"
        suggestion = node_id.replace("-", "_")
        out.append(
            f"node id '{node_id}' contains a hyphen — referenced in a "
            f"Jinja template as '{templated}' it parses as subtraction, "
            f"not a variable. Rename to '{suggestion}' if this id will "
            f"be templated."
        )
    return out
