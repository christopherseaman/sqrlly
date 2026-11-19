from pathlib import Path

import pytest
from click.testing import CliRunner

from sqrlly.cli.main import (
    CHECKPOINT_DB,
    _db_path,
    _effective_safe_mode,
    _is_git_repo,
    _thread_id_for,
    cli,
)

EXAMPLES_DIR = Path(__file__).resolve().parents[3] / "examples"


@pytest.fixture
def runner():
    return CliRunner()


class TestEffectiveSafeMode:
    """`--safe-mode/--no-safe-mode` (tri-state, default None) overrides
    `settings.safe_mode`; absent flag falls back to the setting."""

    def test_flag_absent_uses_setting(self):
        from sqrlly.schema.models import Settings
        assert _effective_safe_mode(None, Settings(safe_mode=True)) is True
        assert _effective_safe_mode(None, Settings(safe_mode=False)) is False

    def test_flag_forces_on(self):
        from sqrlly.schema.models import Settings
        assert _effective_safe_mode(True, Settings(safe_mode=False)) is True

    def test_flag_forces_off(self):
        from sqrlly.schema.models import Settings
        assert _effective_safe_mode(False, Settings(safe_mode=True)) is False


class TestValidateCommand:
    def test_validate_valid_config(self, runner, kitchen_sink_workflow_path):
        result = runner.invoke(cli, ["validate", str(kitchen_sink_workflow_path)])
        assert result.exit_code == 0
        assert "Valid:" in result.output
        assert "Absurd Academic Paper" in result.output

    def test_validate_nonexistent_file(self, runner):
        result = runner.invoke(cli, ["validate", "nonexistent.yaml"])
        assert result.exit_code != 0

    def test_validate_invalid_yaml(self, runner, tmp_path):
        bad_config = tmp_path / "bad.yaml"
        bad_config.write_text("name: test\n")
        result = runner.invoke(cli, ["validate", str(bad_config)])
        assert result.exit_code != 0

    def test_validate_reports_phase_count(self, runner, tmp_path):
        config = tmp_path / "simple.yaml"
        config.write_text(
            "name: Test\nnodes:\n"
            "  - id: p1\n    name: Node 1\n"
            "    agent: {text: test}\n"
        )
        result = runner.invoke(cli, ["validate", str(config)])
        assert result.exit_code == 0
        assert "1 nodes" in result.output

    def test_validate_warns_on_hyphenated_id(self, runner, tmp_path):
        """A hyphenated node id is a valid config — validate still
        succeeds (exit 0, 'Valid:') but emits an advisory warning."""
        config = tmp_path / "hyphen.yaml"
        config.write_text(
            "name: Test\nnodes:\n"
            "  - id: research-phase\n    name: Research\n"
            "    agent: {text: test}\n"
        )
        result = runner.invoke(cli, ["validate", str(config)])
        assert result.exit_code == 0
        assert "Valid:" in result.output
        assert "research-phase" in result.stderr
        assert "warning:" in result.stderr


class TestGraphCommand:
    def test_graph_prints_phase_ids(self, runner, kitchen_sink_workflow_path):
        result = runner.invoke(cli, ["graph", str(kitchen_sink_workflow_path)])
        assert result.exit_code == 0
        # absurd-paper has these named nodes
        assert "abstract" in result.output
        assert "paper" in result.output
        assert "render_pdf" in result.output

    def test_graph_mermaid_format(self, runner, kitchen_sink_workflow_path):
        """Default format is Mermaid — output should contain the header."""
        result = runner.invoke(cli, ["graph", str(kitchen_sink_workflow_path)])
        assert result.exit_code == 0
        assert "flowchart TB" in result.output

    def test_graph_renders_authored_topology_not_compiled_internals(
        self, runner, kitchen_sink_workflow_path
    ):
        """`graph` shares `view`'s schema reconstruction: it draws the
        AUTHORED workflow (gated nodes marked ``:::gated``), never the
        compiled synthetics (``_eval_``/``_sub_``/``_fan_``) whose routing
        is runtime Command(goto=) and would render edge-less."""
        result = runner.invoke(cli, ["graph", str(kitchen_sink_workflow_path)])
        assert result.exit_code == 0
        assert "choose_topic" in result.output
        assert ":::gated" in result.output
        assert "classDef gated" in result.output
        assert "_eval_" not in result.output
        assert "_sub_" not in result.output
        assert "_fan_" not in result.output

    def test_graph_draws_depends_on_edges(
        self, runner, kitchen_sink_workflow_path
    ):
        """Plain depends_on edges appear as solid arrows between real ids."""
        result = runner.invoke(cli, ["graph", str(kitchen_sink_workflow_path)])
        assert result.exit_code == 0
        assert "paper --> render_pdf" in result.output

    def test_graph_shows_start_and_end(self, runner, kitchen_sink_workflow_path):
        """START/END stadium nodes plus the invisible layout spine that
        anchors the workflow subgraph between them."""
        result = runner.invoke(cli, ["graph", str(kitchen_sink_workflow_path)])
        assert result.exit_code == 0
        assert "START([START])" in result.output
        assert "END([END])" in result.output
        assert "START ~~~ workflow" in result.output

    def test_graph_draws_route_edges(self, runner):
        """Declared route: targets render as dashed edges — the pre-Batch-E
        `graph` (compiled-LangGraph render) could not draw these at all."""
        wf = EXAMPLES_DIR / "route_classify" / "workflow.yaml"
        result = runner.invoke(cli, ["graph", str(wf)])
        assert result.exit_code == 0
        assert "-.->" in result.output

    def test_graph_draws_fanout_hexagon(self, runner):
        """A fan-out parent renders as a hexagon, same as in `view`."""
        wf = EXAMPLES_DIR / "wave_planner" / "workflow.yaml"
        result = runner.invoke(cli, ["graph", str(wf)])
        assert result.exit_code == 0
        assert "{{" in result.output


class TestRunCommand:
    def test_run_dry_run(self, runner, kitchen_sink_workflow_path):
        result = runner.invoke(
            cli, ["run", str(kitchen_sink_workflow_path), "--dry-run"]
        )
        assert result.exit_code == 0
        assert "Dry run completed" in result.output
        assert "nodes traced" in result.output

    def test_run_nonexistent_file(self, runner):
        result = runner.invoke(cli, ["run", "nonexistent.yaml"])
        assert result.exit_code != 0

    def test_run_dry_run_lists_nodes(self, runner, kitchen_sink_workflow_path):
        result = runner.invoke(
            cli, ["run", str(kitchen_sink_workflow_path), "--dry-run"]
        )
        assert result.exit_code == 0
        assert "Nodes:" in result.output
        assert "abstract" in result.output

    def test_run_simple_workflow(self, runner, tmp_path):
        """End-to-end: command node that actually runs."""
        import shutil
        echo_bin = shutil.which("echo") or "/bin/echo"
        config = tmp_path / "simple.yaml"
        config.write_text(
            "name: Simple\nnodes:\n"
            "  - id: echo\n    name: Echo Test\n"
            f"    run: [{echo_bin}, 'hello']\n"
        )
        result = runner.invoke(cli, ["run", str(config), "--workdir", str(tmp_path)])
        assert result.exit_code == 0
        assert "Completed: 1 nodes" in result.output

    def test_run_warns_on_hyphenated_id(self, runner, tmp_path):
        """`run` surfaces the same advisory warning as `validate` — the
        node still executes; the warning is non-fatal."""
        import shutil
        echo_bin = shutil.which("echo") or "/bin/echo"
        config = tmp_path / "hyphen.yaml"
        config.write_text(
            "name: Simple\nnodes:\n"
            "  - id: echo-test\n    name: Echo Test\n"
            f"    run: [{echo_bin}, 'hello']\n"
        )
        result = runner.invoke(
            cli, ["run", str(config), "--workdir", str(tmp_path)]
        )
        assert result.exit_code == 0
        assert "Completed: 1 nodes" in result.output
        assert "echo-test" in result.stderr
        assert "warning:" in result.stderr

    def test_run_failing_command_exits_nonzero(self, runner, tmp_path):
        """A failing command node should cause non-zero exit."""
        import shutil
        false_bin = shutil.which("false") or "/bin/false"
        config = tmp_path / "fail.yaml"
        config.write_text(
            "name: Fail\nnodes:\n"
            "  - id: fail\n    name: Fail Test\n"
            f"    run: [{false_bin}]\n"
        )
        result = runner.invoke(cli, ["run", str(config), "--workdir", str(tmp_path)])
        assert result.exit_code != 0
        assert "Failed:" in result.output

    @pytest.mark.parametrize("promote", [True, False])
    def test_isolated_success_cleans_tree_and_reports_retained_ref(self, runner, tmp_path, promote):
        from helpers import init_git_repo
        import subprocess
        import yaml

        init_git_repo(tmp_path)
        node = {"id": "write", "worktree": "isolated", "run": ["sh", "-c", "echo hi > out.txt"]}
        if not promote:
            node["promote"] = False
        config = tmp_path / "workflow.yaml"
        config.write_text(yaml.safe_dump({"nodes": [node]}))
        result = runner.invoke(cli, ["run", str(config), "--workdir", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert not list((tmp_path / ".sqrlly").glob("wt-*"))
        if promote:
            assert (tmp_path / "out.txt").read_text() == "hi\n"
        else:
            assert not (tmp_path / "out.txt").exists()
            refs = subprocess.check_output(["git", "-C", tmp_path, "for-each-ref",
                                            "--format=%(refname)", "refs/sqrlly"], text=True)
            ref, = (line for line in refs.splitlines() if line.endswith("/out"))
            assert ref in result.output
            assert subprocess.check_output(["git", "-C", tmp_path, "show", ref + ":out.txt"]) == b"hi\n"

    def test_isolated_failure_keeps_working_tree(self, runner, tmp_path):
        from helpers import init_git_repo
        import yaml

        init_git_repo(tmp_path)
        config = tmp_path / "workflow.yaml"
        config.write_text(yaml.safe_dump({"nodes": [{"id": "bad", "worktree": "isolated",
                                         "run": ["sh", "-c", "echo diagnosis > evidence; exit 1"]}]}))
        result = runner.invoke(cli, ["run", str(config), "--workdir", str(tmp_path)])
        assert result.exit_code != 0
        tree, = (tmp_path / ".sqrlly").glob("wt-*")
        assert (tree / "evidence").read_text() == "diagnosis\n"
        assert not (tmp_path / "evidence").exists()


class TestRunOptions:
    @pytest.mark.parametrize("flag", ["--preset", "-p"])
    def test_removed_preset_flag_rejected(self, runner, tmp_path, flag):
        config = tmp_path / "simple.yaml"
        config.write_text("nodes: [{id: hello, run: echo}]\n")
        result = runner.invoke(cli, ["run", str(config), flag, "example"])
        assert result.exit_code == 2
        assert f"No such option: {flag}" in result.output


class TestResumeCommand:
    def _simple_config(self, tmp_path):
        import shutil
        echo_bin = shutil.which("echo") or "/bin/echo"
        config = tmp_path / "simple.yaml"
        config.write_text(
            "name: Test\nnodes:\n"
            "  - id: a\n    name: A\n"
            f"    run: [{echo_bin}, 'hi']\n"
        )
        return config

    def test_resume_without_checkpoint_errors(self, runner, tmp_path):
        """--resume with no prior run → clean error."""
        config = self._simple_config(tmp_path)
        result = runner.invoke(
            cli, ["run", str(config), "--resume", "--workdir", str(tmp_path)]
        )
        assert result.exit_code != 0
        assert "No saved state" in result.output

    def test_resume_reads_previous_checkpoint(self, runner, tmp_path):
        """Run, then --resume → picks up completed nodes from SQLite checkpoint."""
        config = self._simple_config(tmp_path)

        first = runner.invoke(
            cli, ["run", str(config), "--workdir", str(tmp_path)]
        )
        assert first.exit_code == 0

        second = runner.invoke(
            cli, ["run", str(config), "--resume", "--workdir", str(tmp_path)]
        )
        assert second.exit_code == 0
        assert "Resuming: skipping 1 completed" in second.output


# ---------------------------------------------------------------------------
# CLI helper unit tests (J9)
# ---------------------------------------------------------------------------


class TestCliHelpers:
    def test_is_git_repo_true(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        assert _is_git_repo(str(tmp_path)) is True

    def test_is_git_repo_false(self, tmp_path):
        assert _is_git_repo(str(tmp_path)) is False

    def test_thread_id_deterministic(self, tmp_path):
        from sqrlly.schema.models import Graph

        config = Graph(
            name="test",
            nodes=[{"id": "a", "name": "A", "agent": {"text": "test"}}],
        )
        id1 = _thread_id_for(config, str(tmp_path))
        id2 = _thread_id_for(config, str(tmp_path))
        assert id1 == id2
        assert len(id1) == 16
        assert all(c in "0123456789abcdef" for c in id1)

    def test_thread_id_workdir_sensitive(self, tmp_path):
        from sqrlly.schema.models import Graph

        config = Graph(
            name="test",
            nodes=[{"id": "a", "name": "A", "agent": {"text": "test"}}],
        )
        id_a = _thread_id_for(config, str(tmp_path / "a"))
        id_b = _thread_id_for(config, str(tmp_path / "b"))
        assert id_a != id_b

    def test_db_path(self, tmp_path):
        result = _db_path(str(tmp_path))
        assert result == str(Path(tmp_path) / CHECKPOINT_DB)


@pytest.mark.parametrize("mode,success,content", [
    ("fail", False, "external"), ("warn", True, "node"),
    ("overwrite", True, "node"), ("skip", True, "external"),
])
def test_concurrent_base_change_conflict_modes(runner, tmp_path, mode, success, content):
    from helpers import init_git_repo
    import yaml

    init_git_repo(tmp_path, files={"shared": "old"})
    config = tmp_path / "workflow.yaml"
    config.write_text(yaml.safe_dump({
        "settings": {"on_promote_conflict": mode},
        "nodes": [{"id": "edit", "worktree": "isolated", "run": [
            "sh", "-c", 'echo node > shared; echo external > "$1/shared"', "edit", str(tmp_path),
        ]}],
    }))
    result = runner.invoke(cli, ["run", str(config), "--workdir", str(tmp_path)])
    assert (result.exit_code == 0) == success, result.output
    assert (tmp_path / "shared").read_text().strip() == content
