import pytest
from pydantic import ValidationError

from sqrlly.schema.models import (
    DimensionCheck,
    Evaluation,
    FanOut,
    FanOutTemplate,
    Graph,
    Node,
    Outputs,
    Route,
    RouteElse,
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


class TestQualityGate:
    def test_basic_gate(self):
        gate = Evaluation(validator="gates/v.py", threshold=0.85)
        assert gate.validator == "gates/v.py"
        assert gate.threshold == 0.85
        assert gate.blocking is False
        assert gate.max_retries is None

    def test_blocking_gate(self):
        gate = Evaluation(validator="v.md", threshold=0.9, blocking=True)
        assert gate.blocking is True

    def test_threshold_bounds(self):
        with pytest.raises(ValidationError):
            Evaluation(validator="v.md", threshold=1.5)
        with pytest.raises(ValidationError):
            Evaluation(validator="v.md", threshold=-0.1)

    def test_custom_max_retries(self):
        gate = Evaluation(validator="v.md", threshold=0.8, max_retries=5)
        assert gate.max_retries == 5

    def test_dimension_gate(self):
        gate = Evaluation(
            validator="v.py",
            dimensions=[
                DimensionCheck(field="correctness", threshold=0.7),
                DimensionCheck(field="style", threshold=0.5),
            ],
        )
        assert len(gate.dimensions) == 2
        assert gate.dimensions[0].field == "correctness"
        assert gate.dimensions[0].threshold == 0.7

    def test_dimension_gate_from_yaml(self):
        # `min` is the back-compat YAML alias for `threshold`.
        raw = {
            "validator": "v.py",
            "dimensions": [
                {"field": "correctness", "min": 0.7},
                {"field": "style", "min": 0.5},
            ],
        }
        gate = Evaluation(**raw)
        assert gate.dimensions[1].field == "style"
        assert gate.dimensions[0].threshold == 0.7

    def test_dimension_check_bounds(self):
        with pytest.raises(ValidationError):
            DimensionCheck(field="x", min=1.5)
        with pytest.raises(ValidationError):
            DimensionCheck(field="x", min=-0.1)


class TestEffectiveMaxRetries:
    """Node.effective_max_retries resolves gate override vs settings default."""

    def test_no_gate_uses_settings(self):
        settings = Settings(max_retries=5)
        node = Node(id="p1", name="P1", agent="t.md")
        assert node.effective_max_retries(settings) == 5

    def test_gate_with_override(self):
        settings = Settings(max_retries=5)
        node = Node(
            id="p1",
            name="P1",
            agent="t.md",
            evaluation=Evaluation(validator="v.md", threshold=0.8, max_retries=2),
        )
        assert node.effective_max_retries(settings) == 2

    def test_gate_without_override_uses_settings(self):
        settings = Settings(max_retries=7)
        node = Node(
            id="p1",
            name="P1",
            agent="t.md",
            evaluation=Evaluation(validator="v.md", threshold=0.8),
        )
        assert node.effective_max_retries(settings) == 7


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


class TestInlineRoute:
    """Stage 5c: `Node.route` block (inline forward dispatch).

    Coexists with `execute:` (executes, evaluates, then routes) or
    stands alone (node has no execute body, just a route).
    """

    def test_unconditional_goto_str(self):
        r = Route(goto="b")
        assert r.goto == "b"
        assert r.cases == []
        assert r.else_ is None
        assert r.include_eval is False

    def test_unconditional_goto_list(self):
        r = Route(goto=["b", "c"])
        assert r.goto == ["b", "c"]

    def test_unconditional_with_include_eval(self):
        r = Route(goto="b", include_eval=True)
        assert r.include_eval is True

    def test_cases_with_string_else(self):
        r = Route.model_validate({
            "cases": [{"when": "x>0", "goto": "ship"}],
            "else": "fallback",
        })
        assert r.cases[0].goto == "ship"
        assert r.cases[0].include_eval is False
        # else: string shorthand auto-promoted to RouteElse
        assert isinstance(r.else_, RouteElse)
        assert r.else_.goto == "fallback"
        assert r.else_.include_eval is False

    def test_cases_with_list_else(self):
        r = Route.model_validate({
            "cases": [{"when": "x>0", "goto": ["a", "b"]}],
            "else": ["c", "d"],
        })
        assert r.cases[0].goto == ["a", "b"]
        assert r.else_.goto == ["c", "d"]

    def test_cases_with_structured_else(self):
        r = Route.model_validate({
            "cases": [{"when": "x>0", "goto": "ship"}],
            "else": {"goto": "human_review", "include_eval": True},
        })
        assert r.else_.goto == "human_review"
        assert r.else_.include_eval is True

    def test_per_case_include_eval(self):
        r = Route.model_validate({
            "cases": [
                {"when": "passed('x')", "goto": "ship"},
                {"when": "score('x') < 0.4", "goto": "rewrite", "include_eval": True},
            ],
            "else": "review",
        })
        assert r.cases[0].include_eval is False
        assert r.cases[1].include_eval is True

    def test_goto_and_cases_are_mutex(self):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            Route.model_validate({
                "goto": "a",
                "cases": [{"when": "x", "goto": "b"}],
                "else": "c",
            })

    def test_cases_require_else(self):
        with pytest.raises(ValidationError, match="requires `else"):
            Route.model_validate({
                "cases": [{"when": "x", "goto": "b"}],
            })

    def test_else_without_cases_rejected(self):
        """Bare `else:` without `cases:` is a silent unconditional
        redirect — confusing structure identical to `goto:` shorthand.
        Reject so authors pick the unambiguous form."""
        with pytest.raises(ValidationError, match="requires `cases"):
            Route.model_validate({"else": "fallback"})

    def test_route_must_set_at_least_one_form(self):
        with pytest.raises(ValidationError, match="requires either"):
            Route()

    def test_route_level_include_eval_only_with_goto_shorthand(self):
        # `cases:` form forbids route-level include_eval (per-case is canonical)
        with pytest.raises(ValidationError, match="route level"):
            Route.model_validate({
                "include_eval": True,
                "cases": [{"when": "x", "goto": "b"}],
                "else": "c",
            })

    def test_node_route_field(self):
        config = Graph(
            name="T",
            nodes=[
                {"id": "a", "name": "A", "agent": "a.md",
                 "route": {"goto": "b"}},
                {"id": "b", "name": "B", "agent": "b.md"},
            ],
        )
        assert config.nodes[0].route is not None
        assert config.nodes[0].route.goto == "b"

    def test_node_route_unknown_target_rejected(self):
        with pytest.raises(ValidationError, match="goto 'ghost'"):
            Graph(
                name="T",
                nodes=[
                    {"id": "a", "name": "A", "agent": "a.md",
                     "route": {"goto": "ghost"}},
                ],
            )

    def test_node_route_else_unknown_target_rejected(self):
        with pytest.raises(ValidationError, match="else goto 'ghost'"):
            Graph(
                name="T",
                nodes=[
                    {"id": "a", "name": "A", "agent": "a.md",
                     "route": {
                         "cases": [{"when": "x", "goto": "b"}],
                         "else": "ghost",
                     }},
                    {"id": "b", "name": "B", "agent": "b.md"},
                ],
            )

    def test_node_route_list_goto_unknown_rejected(self):
        with pytest.raises(ValidationError, match="goto 'ghost'"):
            Graph(
                name="T",
                nodes=[
                    {"id": "a", "name": "A", "agent": "a.md",
                     "route": {"goto": ["b", "ghost"]}},
                    {"id": "b", "name": "B", "agent": "b.md"},
                ],
            )

    def test_inline_route_node_cannot_be_dep(self):
        # Inline-route nodes dispatch via Command; static depends_on to
        # them would double-trigger the goto target.
        with pytest.raises(ValidationError, match="depends on route"):
            Graph(
                name="T",
                nodes=[
                    {"id": "a", "name": "A", "agent": "a.md",
                     "route": {"goto": "b"}},
                    {"id": "b", "name": "B", "agent": "b.md"},
                    {"id": "c", "name": "C", "agent": "c.md",
                     "depends_on": ["a"]},
                ],
            )

    def test_standalone_inline_route(self):
        # Node with route but no execute — pure forward-edge dispatcher.
        # Validates target resolution like any other route.
        config = Graph(
            name="T",
            nodes=[
                {"id": "produce", "name": "P", "agent": "p.md"},
                {"id": "dispatch", "name": "D", "depends_on": ["produce"],
                 "route": {
                     "cases": [{"when": "True", "goto": "ship"}],
                     "else": "__end__",
                 }},
                {"id": "ship", "name": "S", "agent": "s.md"},
            ],
        )
        assert config.nodes[1].kind is None
        assert config.nodes[1].route is not None

    def test_fan_out_and_route_on_one_node_rejected(self):
        # A node cannot both fan out and carry an inline route: gated fan-out
        # dispatches via _fan_<id> while route dispatches via _route_<id>;
        # ungated, both static edges fire and double-dispatch. Fail at load.
        with pytest.raises(ValidationError, match="fan_out.*route|route.*fan_out"):
            Node(
                id="p", name="P",
                agent="p.md",
                fan_out=FanOut(template=FanOutTemplate(agent="w.md")),
                route=Route(goto="d"),
            )


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


class TestFanOut:
    def test_dynamic_config(self):
        config = FanOut(
            manifest_path="manifest.json",
            template={"agent": "template.md"},
        )
        assert config.manifest_path == "manifest.json"
        assert config.template is not None

    def test_template_with_evaluation(self):
        config = FanOut(
            manifest_path="m.json",
            template={
                "agent": "template.md",
                "evaluation": {"validator": "v.md", "threshold": 0.8},
            },
        )
        assert config.template.evaluation.threshold == 0.8

    def test_legacy_enabled_field_rejected(self):
        """`enabled` was removed in the post-Stage-5c audit — the
        block's presence is the activation. Authors carrying old YAML
        get a loud Pydantic ValidationError pointing at the unsupported
        field instead of silent fan-out skipping."""
        from pydantic import ValidationError as _VE
        with pytest.raises(_VE, match="enabled"):
            FanOut(enabled=True, template={"agent": "t.md"})

    def test_final_nodes(self):
        config = FanOut(
            manifest_path="m.json",
            template={"agent": "t.md"},
            final_nodes=[
                {
                    "id": "summary",
                    "name": "Summary",
                    "agent": "s.md",
                }
            ],
        )
        assert len(config.final_nodes) == 1
        assert config.final_nodes[0].id == "summary"
        assert config.final_nodes[0].agent.prompt == "s.md"

    def test_final_node_accepts_output_contract(self):
        """C1: `output_contract` is valid on a fan-out final node (it runs
        through the standard execution path, which enforces it)."""
        config = FanOut(
            template={"agent": "t.md"},
            final_nodes=[
                {
                    "id": "summary",
                    "name": "Summary",
                    "agent": "s.md",
                    "outputs": {
                        "files": ['summary.md'],
                    },
                }
            ],
        )
        fn = config.final_nodes[0]
        assert fn.outputs.files == ["summary.md"]

    def test_empty_block_is_well_formed(self):
        """A bare `FanOut()` is structurally valid (template is optional
        at the schema level — runtime validates required fields when
        the parent node is wired). Activation is by presence-of-block,
        not by an `enabled` flag."""
        config = FanOut()
        assert config.template is None
        assert config.manifest_path is None
        assert config.final_nodes == []

    def test_fan_out_promote_field(self):
        from sqrlly.schema.models import FanOut, FanOutTemplate
        fo = FanOut(template=FanOutTemplate(agent="x.md"), promote=True)
        assert fo.promote is True
        # default is False
        assert FanOut(template=FanOutTemplate(agent="x.md")).promote is False


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
    def test_default_is_warn(self):
        from sqrlly.schema.models import Settings
        assert Settings().on_promote_conflict == "warn"

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


def test_settings_promote_include_field():
    from sqrlly.schema.models import Settings
    s = Settings(promote_include=["log/phases/*"])
    assert s.promote_include == ["log/phases/*"]
    assert Settings().promote_include == []   # default empty


class TestWorktreeSetupFields:
    def test_defaults(self):
        from sqrlly.schema.models import Settings
        s = Settings()
        assert s.worktree_setup == []
        assert s.worktree_setup_exclude == []

    def test_accepts_values(self):
        from sqrlly.schema.models import Settings
        s = Settings(
            worktree_setup=["pnpm install --prefer-offline"],
            worktree_setup_exclude=["node_modules", "src/generated/prisma"],
        )
        assert s.worktree_setup == ["pnpm install --prefer-offline"]
        assert s.worktree_setup_exclude == ["node_modules", "src/generated/prisma"]
