"""Tests for ``runtime/settings_merge.merge_settings``.

The merge function is the foundation of Phase 3 scope-aware settings.
It must:
  - Inherit any field the child YAML didn't explicitly set.
  - Honor any field the child YAML did set, even if its value matches
    the schema default (author intent: "I declared this on purpose").
  - Compose across nested levels via repeated calls.

Pydantic's ``model_fields_set`` powers the "explicitly set?" decision.
These tests pin its behavior at the point of YAML parse, which is
where merge_settings is supposed to be called.
"""
from __future__ import annotations

from sqrlly.runtime.settings_merge import merge_settings
from sqrlly.schema.models import Settings


class TestSingleFieldOverride:
    def test_child_max_parallel_jobs_wins(self):
        parent = Settings(max_parallel_jobs=3)
        child = Settings(max_parallel_jobs=5)
        merged = merge_settings(parent, child)
        assert merged.max_parallel_jobs == 5

    def test_unset_child_inherits_parent(self):
        parent = Settings(max_parallel_jobs=5, default_timeout=60.0)
        child = Settings()  # nothing authored
        merged = merge_settings(parent, child)
        assert merged.max_parallel_jobs == 5
        assert merged.default_timeout == 60.0

    def test_partial_child_keeps_parent_for_other_fields(self):
        parent = Settings(max_parallel_jobs=5, default_timeout=300.0)
        child = Settings(max_parallel_jobs=10)
        merged = merge_settings(parent, child)
        assert merged.max_parallel_jobs == 10
        assert merged.default_timeout == 300.0


class TestExplicitDefaultStillWins:
    """A subgraph that *explicitly* sets a field to the schema default
    must still beat parent — author intent, not absence."""

    def test_child_explicitly_resets_to_default(self):
        parent = Settings(max_parallel_jobs=5)
        child = Settings(max_parallel_jobs=4)  # 4 == schema default
        merged = merge_settings(parent, child)
        assert merged.max_parallel_jobs == 4


class TestWorktreeInheritance:
    """Worktree isolation inherits graph→subgraph: a subgraph inherits the
    graph's default or overrides it, and authoring either field of the
    mutually-exclusive (worktree, worktree_group) pair clears the inherited
    sibling for that scope."""

    def test_subgraph_inherits_parent_worktree(self):
        parent = Settings(worktree="off")
        child = Settings()  # subgraph authored no worktree
        assert merge_settings(parent, child).worktree == "off"

    def test_subgraph_overrides_parent_worktree(self):
        parent = Settings(worktree="off")
        child = Settings(worktree="isolated")
        assert merge_settings(parent, child).worktree == "isolated"

    def test_child_mode_clears_inherited_group(self):
        parent = Settings(worktree_group="prd")
        child = Settings(worktree="isolated")  # subgraph forces isolation
        merged = merge_settings(parent, child)
        assert merged.worktree == "isolated"
        assert merged.worktree_group is None  # inherited group shadowed

    def test_child_group_clears_inherited_mode(self):
        parent = Settings(worktree="isolated")
        child = Settings(worktree_group="team-a")
        merged = merge_settings(parent, child)
        assert merged.worktree_group == "team-a"
        assert merged.worktree == "auto"  # neutralized; group is active


class TestComposesNested:
    """Real workflows have parent → subgraph → sub-subgraph chains.
    merge_settings must compose left-to-right; the right-most explicit
    set wins per-field."""

    def test_three_level_inheritance(self):
        top = Settings(max_parallel_jobs=2, default_timeout=60.0, preamble_file="top")
        mid = Settings(preamble_file="mid")
        bot = Settings(max_parallel_jobs=10)

        # Each layer merges with the result of the previous merge.
        l1 = merge_settings(top, mid)
        l2 = merge_settings(l1, bot)

        assert l2.preamble_file == "mid"  # mid's win held through l2
        assert l2.max_parallel_jobs == 10          # bot wins
        assert l2.default_timeout == 60.0    # top's flowed through


class TestMultiFieldCarriage:
    def test_max_parallel_jobs_inherited_when_child_unset(self):
        parent = Settings(max_parallel_jobs=8)
        child = Settings()
        merged = merge_settings(parent, child)
        assert merged.max_parallel_jobs == 8


class TestRoundTripConstraint:
    """``model_fields_set`` is preserved by ``Settings(**kwargs)`` and
    ``model_validate(dict)``, but NOT by ``model_dump() →
    model_validate(...)``. The merge function relies on the freshly-
    validated case (the YAML-parse case). Pin that behavior so future
    refactors don't accidentally use a round-trip path."""

    def test_fields_set_preserved_through_kwargs(self):
        s = Settings(max_parallel_jobs=7)
        assert "max_parallel_jobs" in s.model_fields_set
        # Reconstruction via kwargs preserves the set.
        s2 = Settings(**s.model_dump(exclude_unset=True))
        assert "max_parallel_jobs" in s2.model_fields_set

    def test_nested_defaults_preserve_sibling_options(self):
        parent = Settings(defaults={"agent": {"model": "sonnet", "backend_max_retries": 4}})
        child = Settings(defaults={"agent": {"model": "opus"}})
        merged = merge_settings(parent, child)
        assert merged.defaults.agent.model == "opus"
        assert merged.defaults.agent.backend_max_retries == 4


class TestModelInheritance:
    def test_parent_model_supplies_child_default(self):
        parent = Settings(model="sonnet")
        assert merge_settings(parent, Settings()).model == "sonnet"

    def test_child_model_overrides_parent(self):
        parent = Settings(model="sonnet")
        assert merge_settings(parent, Settings(model="opus")).model == "opus"
