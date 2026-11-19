"""Path ownership and conflict policy for snapshot promotion."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PromotionPlan:
    """Reconciliation result for multiple nodes' promote footprints.

    ``allowed`` maps ``node_id`` → the ``{path: kind}`` subset that node is
    cleared to apply. ``conflicts`` maps ``path`` → the owning ``node_id``s
    for any path claimed by more than one node (the report the caller
    surfaces to the user)."""
    allowed: dict[str, dict[str, str]]
    conflicts: dict[str, list[str]]


class PromoteConflictError(RuntimeError):
    """Raised by ``plan_promotions`` under ``on_promote_conflict='fail'``
    when two or more nodes promote the same path. Carries the conflict map
    so the CLI can render an actionable message."""

    def __init__(self, conflicts: dict[str, list[str]]):
        self.conflicts = conflicts
        detail = "; ".join(
            f"{path} <- {', '.join(nodes)}"
            for path, nodes in sorted(conflicts.items())
        )
        super().__init__(
            f"Promote conflict ({len(conflicts)} path(s)): {detail}"
        )


def plan_promotions(
    footprints: dict[str, dict[str, str]], mode: str,
) -> PromotionPlan:
    """Reconcile per-node promote footprints under ``mode``.

    ``footprints`` is ``{node_id: {path: kind}}`` in promote order (the
    iteration order of ``config.nodes``). A path appearing in two or more
    footprints is a conflict (deletions count — a delete-vs-edit on the
    same path collides). Modes:

    - ``fail``      → raise ``PromoteConflictError`` (before any apply).
    - ``warn``      → every node keeps its full footprint (last-write-wins);
                      ``conflicts`` rides along for the caller to log.
    - ``overwrite`` → same allowance as ``warn``; the caller stays silent.
    - ``skip``      → the first owner (promote order) keeps a conflicting
                      path; later owners drop it (other paths still apply).
    """
    owners: dict[str, list[str]] = {}
    for node_id, changes in footprints.items():
        for path in changes:
            owners.setdefault(path, []).append(node_id)
    conflicts = {p: ns for p, ns in owners.items() if len(ns) > 1}

    if conflicts and mode == "fail":
        raise PromoteConflictError(conflicts)

    if mode == "skip":
        allowed: dict[str, dict[str, str]] = {}
        claimed: set[str] = set()
        for node_id, changes in footprints.items():
            allowed[node_id] = {
                p: k for p, k in changes.items() if p not in claimed
            }
            claimed.update(changes)
        return PromotionPlan(allowed=allowed, conflicts=conflicts)

    return PromotionPlan(
        allowed={nid: dict(ch) for nid, ch in footprints.items()},
        conflicts=conflicts,
    )
