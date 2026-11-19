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
        + _fanout_parent_promote_warnings(config)
        + _fanout_resume_drift_warnings(config)
    )


def _fanout_resume_drift_warnings(config: Graph) -> list[str]:
    """Advise deterministic item ids on a fan-out whose manifest is a RUNTIME
    output (no static ``manifest_path``).

    On ``--resume`` the parent re-reads its manifest and re-fans; if the
    dispatcher mints non-deterministic ids (uuid / counter / positional
    index), completed siblings silently vanish and the failed child is
    orphaned while all N re-run. The runtime drift guard
    (``settings.on_manifest_drift``) catches it, but flagging it at
    ``validate`` time steers authors to stable ids up front. A static
    ``manifest_path`` file has stable ids by construction — silent.
    """
    out: list[str] = []
    for node in config.nodes:
        fo = node.fan_out
        if fo is not None and fo.manifest_path is None:
            out.append(
                f"node '{node.id}': fan-out manifest is a runtime output — on "
                f"'--resume' the dispatcher must emit deterministic item "
                f"'id's, else completed children are re-billed and the failed "
                f"child orphaned. Set 'settings.on_manifest_drift: warn' to "
                f"opt into intentional re-fanning."
            )
    return out


def _upstream_route_warnings(config: Graph) -> list[str]:
    return [
        f"{node.id}: route to upstream node {target!r} has no max_attempts"
        for node, case, target, is_upstream in iter_route_edges(config)
        if is_upstream and case.max_attempts is None
    ]


def _worktree_setup_exclude_warnings(config: Graph) -> list[str]:
    """Flag a worktree_setup that generates an in-tree artifact (prisma
    generate) without a worktree_setup_exclude — the generated client would
    leak into the promote footprint."""
    s = config.settings
    runs_prisma_generate = any("prisma generate" in cmd for cmd in s.worktree_setup)
    if runs_prisma_generate and not s.worktree_setup_exclude:
        return [
            "settings.worktree_setup runs 'prisma generate' but "
            "worktree_setup_exclude is empty — the generated client may leak "
            "into the promote footprint. Add its output path (e.g. "
            "'src/generated/prisma' or 'node_modules') to worktree_setup_exclude."
        ]
    return []


def _fanout_parent_promote_warnings(config: Graph) -> list[str]:
    """Flag ``node.promote: true`` on a fan-out parent.

    A fan-out parent's OWN worktree holds only the manifest, not the branch
    deltas. Setting ``node.promote`` there promotes the manifest-only tree,
    not what the author almost certainly intends. ``fan_out.promote`` is the
    correct knob — it promotes each Send branch's worktree through the same
    reconcile path.
    """
    out: list[str] = []
    for node in config.nodes:
        if node.fan_out is not None and node.promote:
            out.append(
                f"node {node.id!r}: promote on a fan-out parent promotes only "
                f"its (manifest-only) worktree, not the branch worktrees — set "
                f"fan_out.promote to merge the branch deltas back to base."
            )
    return out


def _hyphenated_id_warnings(config: Graph) -> list[str]:
    """Flag node ids containing ``-``.

    A hyphenated id referenced as ``{{the-id}}`` in a Jinja template
    parses as subtraction (``the - id``), silently producing wrong or
    empty output rather than the node's value. Underscores are safe.
    Covers top-level node ids and fan-out final-node ids.
    """
    ids: list[str] = []
    for node in config.nodes:
        ids.append(node.id)
        if node.fan_out:
            ids.extend(fn.id for fn in node.fan_out.final_nodes)

    out: list[str] = []
    for node_id in ids:
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
