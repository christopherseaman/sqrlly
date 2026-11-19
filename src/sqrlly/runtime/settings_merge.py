"""Settings inherited by a nested workflow."""
from __future__ import annotations

from pathlib import Path

from sqrlly.schema.models import Settings


def merge_settings(parent: Settings, child: Settings, parent_dir: Path = Path(".")) -> Settings:
    """Apply authored child overrides, including individual per-kind defaults."""
    base = parent.model_dump(exclude_unset=True)
    overrides = child.model_dump(exclude_unset=True)
    defaults = base.setdefault("defaults", {})
    for kind, options in overrides.pop("defaults", {}).items():
        defaults.setdefault(kind, {}).update(options)
    if "worktree" in overrides and "worktree_group" not in overrides:
        base.pop("worktree_group", None)
    if "worktree_group" in overrides and "worktree" not in overrides:
        base.pop("worktree", None)
    merged = Settings.model_validate(base | overrides)
    merged._workflow_dir = child.workflow_dir or parent.workflow_dir
    if "preamble_file" in overrides:
        merged._preamble_path = child._preamble_path
    else:
        merged._preamble_path = parent.preamble_path(parent.workflow_dir or parent_dir.resolve())
    return merged
