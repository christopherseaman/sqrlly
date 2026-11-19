"""Shared langgraph-free compile helpers.

Lives in its own module so `compile/graph.py`, `compile/dynamic.py`,
and `compile/subgraph.py` can all import these without crossing a
private-import boundary in the other direction. Pure functions — no
langgraph imports.
"""
from __future__ import annotations

import json
import hashlib
from urllib.parse import quote
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqrlly.runtime._json import extract_json
from sqrlly.runtime.result import ManifestError
from sqrlly.runtime.state import WorkflowState
from sqrlly.schema.models import Node
from sqrlly.schema.references import split_manifest_source
from sqrlly.schema.v1 import File, repository_root, require_reference

if TYPE_CHECKING:
    from sqrlly.schema.models import Graph


def find_terminal_nodes(config: "Graph") -> list[str]:
    """Node ids not depended on by any other node, in declaration order.

    A subgraph's terminal output is the *last* such node; the top-level
    builder wants the set. Returning an ordered list serves both —
    callers ``set(...)`` it or take ``[-1]`` as needed.
    """
    depended_on: set[str] = set()
    for node in config.nodes:
        depended_on.update(node.depends_on)
    return [n.id for n in config.nodes if n.id not in depended_on]


def manifest_drift(prior_children: set[str], new_child_ids: set[str]) -> set[str]:
    """Prior fan-out branch ids NOT covered by the freshly read manifest.

    ``prior - new``: empty for a stable-id resume OR a purely additive re-fan
    (``new`` is a superset of ``prior``), so those never trip the drift guard —
    it fires only when a resume DROPS a prior branch (silent data loss: a
    completed sibling vanishes, or the failed child is orphaned).
    """
    return set(prior_children) - set(new_child_ids)


def _array(value: Any, *, allow_wrapper: bool) -> list:
    if allow_wrapper and isinstance(value, dict):
        value = value.get("items")
    if not isinstance(value, list):
        raise ValueError("manifest must be a JSON array")
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _child_ids(parent: str, items: list) -> list[tuple[str, Any]]:
    branches = []
    seen = set()
    counts = {}
    for item in items:
        if isinstance(item, dict) and "id" in item:
            raw = item["id"]
            # Preserve ordinary string ids and reserve @ and ! for the other namespaces.
            key = quote(raw, safe="", errors="surrogatepass") if isinstance(raw, str) else "!" + quote(_canonical(raw), safe="")
        else:
            digest = hashlib.sha256(_canonical(item).encode("ascii")).hexdigest()
            ordinal = counts.get(digest, 0)
            counts[digest] = ordinal + 1
            key = f"@{digest}.{ordinal}"
        child_id = f"{parent}::{key}"
        if child_id in seen:
            raise ValueError(f"duplicate manifest id {child_id!r}")
        seen.add(child_id)
        branches.append((child_id, item))
    return branches


def _read_manifest(
    state: WorkflowState, node: Node, *, workflow_dir: Path | str | None = None,
) -> list[tuple[str, Any]]:
    """Read the declared source and pair each raw JSON value with its stable branch id."""
    source = node.for_each
    try:
        if isinstance(source, File):
            directory = Path(workflow_dir or state.get("workdir", ".")).resolve()
            path = directory / source.file
            require_reference(repository_root(directory), path, "for_each.file")
            items = _array(json.loads(path.read_text(encoding="utf-8")), allow_wrapper=True)
        else:
            if not isinstance(source, str):
                raise ValueError("missing manifest source")
            structured = state.get("node_structured_outputs", {})
            outputs = state.get("node_outputs", {})
            root, fields = split_manifest_source(source, node._ancestors or node.depends_on)
            if root in structured:
                value = structured[root]
                for field in fields:
                    if not isinstance(value, dict) or field not in value:
                        raise ValueError(f"source {source!r} has no field {field!r}")
                    value = value[field]
                items = _array(value, allow_wrapper=False)
            elif not fields and root in outputs:
                items = _array(json.loads(extract_json(outputs[root])), allow_wrapper=True)
            else:
                raise ValueError(f"source {source!r} has no output")
        return _child_ids(node.id, items)
    except (ValueError, OSError, TypeError, UnicodeError) as exc:
        raise ManifestError(f"for_each {node.id!r} ({source}): {exc}") from exc
