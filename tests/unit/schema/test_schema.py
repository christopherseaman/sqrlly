import pytest
from pydantic import ValidationError

from sqrlly.schema.models import (
    Graph,
    Node,
    Outputs,
    Settings,
)

class TestMinimalWorkflow:
    def test_minimal_config(self, minimal_config_dict):
        config = Graph(**minimal_config_dict)
        assert config.name == "Test Workflow"
        assert len(config.nodes) == 1

    def test_optional_graph_metadata(self):
        graph = Graph(nodes=[{"id": "a"}])
        assert graph.name == "workflow"
        assert graph.nodes[0].name == "a"


class TestOutputs:
    def test_basic_contract(self):
        contract = Outputs(
            files=['output/result.json']
        )
        assert contract.files == ["output/result.json"]

    def test_empty_required_files(self):
        contract = Outputs(files=[])
        assert contract.files == []


class TestSettingsMemoryGates:
    """Memory back-pressure: percent-based gate."""

    def test_defaults_disabled(self):
        s = Settings()
        assert s.memory_threshold_pct is None

    def test_pct_passthrough(self):
        s = Settings(memory_threshold_pct=80.0)
        assert s.memory_threshold_pct == 80.0



class TestSettingsSafeMode:
    """`settings.safe_mode` opts a workflow into running Claude with
    `--safe-mode` (operator customizations disabled). Default off — sqrlly
    never overrides operator settings unless asked."""

    def test_default_off(self):
        assert Settings().safe_mode is False

    def test_accepts_true(self):
        assert Settings(safe_mode=True).safe_mode is True


class TestDependencyValidation:
    def test_valid_dependencies(self, multi_phase_config_dict):
        config = Graph(**multi_phase_config_dict)
        assert config.nodes[1].depends_on == ["node-1"]

    def test_invalid_dependency_reference(self):
        with pytest.raises(ValidationError, match="nonexistent"):
            Graph(
                name="Test",
                nodes=[
                    {"id": "p1", "name": "P1", "agent": "t.md"},
                    {
                        "id": "p2",
                        "name": "P2",
                        "agent": "t.md",
                        "depends_on": ["nonexistent"],
                    },
                ],
            )

    def test_duplicate_phase_ids(self):
        with pytest.raises(ValidationError, match="[Dd]uplicate"):
            Graph(
                name="Test",
                nodes=[
                    {"id": "p1", "name": "P1", "agent": "t.md"},
                    {"id": "p1", "name": "P1 Again", "agent": "t2.md"},
                ],
            )

    def test_self_dependency(self):
        with pytest.raises(ValidationError, match="self-dependency"):
            Graph(
                name="Test",
                nodes=[
                    {
                        "id": "p1",
                        "name": "P1",
                        "agent": "t.md",
                        "depends_on": ["p1"],
                    }
                ],
            )

    def test_no_dependencies(self):
        node = Node(id="p1", name="P1", agent="t.md")
        assert node.depends_on == []


def test_kitchen_sink_composes_a_child_workflow(kitchen_sink_workflow_path):
    from sqrlly.schema.models import load_workflow

    graph = load_workflow(kitchen_sink_workflow_path)
    nodes = {node.id: node for node in graph.nodes}
    assert graph.name == "Absurd Academic Paper"
    assert (nodes["abstract"].agent or nodes["abstract"].llm).prompt.endswith(".md")
    assert nodes["paper"].workflow.file.endswith(".yaml")
    assert nodes["paper"].workflow.inputs


class TestNodeTimeout:
    def test_phase_timeout_field(self):
        p = Node(id="a", name="A", timeout=30.0)
        assert p.timeout == 30.0

    def test_phase_timeout_defaults_none(self):
        p = Node(id="a", name="A")
        assert p.timeout is None

    def test_settings_default_timeout(self):
        s = Settings(default_timeout=60.0)
        assert s.default_timeout == 60.0

    def test_settings_default_timeout_defaults_none(self):
        s = Settings()
        assert s.default_timeout is None

    def test_effective_timeout_phase_overrides_settings(self):
        s = Settings(default_timeout=60.0)
        p = Node(id="a", name="A", timeout=10.0)
        assert p.effective_timeout(s) == 10.0

    def test_effective_timeout_falls_back_to_settings(self):
        s = Settings(default_timeout=60.0)
        p = Node(id="a", name="A")
        assert p.effective_timeout(s) == 60.0

    def test_effective_timeout_both_none(self):
        s = Settings()
        p = Node(id="a", name="A")
        assert p.effective_timeout(s) is None


class TestOnPromoteConflict:
    def test_default_is_fail(self):
        from sqrlly.schema.models import Settings
        assert Settings().on_promote_conflict == "fail"

    def test_accepts_all_four_modes(self):
        from sqrlly.schema.models import Settings
        for mode in ("fail", "warn", "overwrite", "skip"):
            assert Settings(on_promote_conflict=mode).on_promote_conflict == mode

    def test_rejects_unknown_mode(self):
        import pytest
        from pydantic import ValidationError
        from sqrlly.schema.models import Settings
        with pytest.raises(ValidationError):
            Settings(on_promote_conflict="merge")


class TestOnManifestDrift:
    def test_default_is_fail(self):
        from sqrlly.schema.models import Settings
        assert Settings().on_manifest_drift == "fail"

    def test_accepts_both_modes(self):
        from sqrlly.schema.models import Settings
        for mode in ("fail", "warn"):
            assert Settings(on_manifest_drift=mode).on_manifest_drift == mode

    def test_rejects_unknown_mode(self):
        import pytest
        from pydantic import ValidationError
        from sqrlly.schema.models import Settings
        with pytest.raises(ValidationError):
            Settings(on_manifest_drift="skip")


class TestPromoteExclude:
    def test_defaults_empty(self):
        from sqrlly.schema.models import Settings
        assert Settings().promote_exclude == []

    def test_accepts_list(self):
        from sqrlly.schema.models import Settings
        assert Settings(promote_exclude=["node_modules", ".next/cache"]).promote_exclude \
            == ["node_modules", ".next/cache"]


class TestWorktreeSetupFields:
    def test_defaults(self):
        from sqrlly.schema.models import Settings
        s = Settings()
        assert s.worktree_setup == []

    def test_accepts_values(self):
        from sqrlly.schema.models import Settings
        s = Settings(
            worktree_setup=["pnpm install --prefer-offline"],
        )
        assert s.worktree_setup == ["pnpm install --prefer-offline"]
