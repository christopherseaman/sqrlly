"""Resume skip-set computation (pure, langgraph-free, compile layer).

`compute_skip_set` decides which prior-run-completed nodes are safe to skip on
``--resume``: prior_completed minus the "dirty" closure (prior failures +
``--resume-from`` targets, expanded by transitive ``depends_on`` dependents,
and route-target reachability). Topological only —
no content hashing (LLM nondeterminism + fan-out/promote side-effects make a
content hash neither sufficient nor safe; see the design spec)."""
from __future__ import annotations

from sqrlly.schema.models import Graph, Node
from sqrlly.schema.v1 import RESERVED


def _route_targets(node: Node) -> list[str]:
    return [target for case in node.route or [] for target in case.targets if target not in RESERVED]


def compute_skip_set(
    config: Graph,
    prior_completed: set[str],
    prior_failed: set[str],
    rerun_targets: set[str],
) -> set[str]:
    """Freeze completed nodes outside the failure, rerun, and downstream closure."""
    ids = {n.id for n in config.nodes}
    dependents: dict[str, list[str]] = {nid: [] for nid in ids}
    for n in config.nodes:
        for dep in n.depends_on:
            if dep in dependents:
                dependents[dep].append(n.id)

    route_adj = {n.id: _route_targets(n) for n in config.nodes}
    parents = {node.id for node in config.nodes if node.for_each}
    failed_child_parents = {child.partition("::")[0] for child in prior_failed if "::" in child} & parents

    dirty: set[str] = set(prior_failed) | set(rerun_targets) | failed_child_parents
    frontier = list(dirty)
    while frontier:
        cur = frontier.pop()
        neighbors = dependents.get(cur, []) + route_adj.get(cur, [])
        for nxt in neighbors:
            if nxt not in dirty:
                dirty.add(nxt)
                frontier.append(nxt)
    return set(prior_completed) - dirty
