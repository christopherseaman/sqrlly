import json
import subprocess

from click.testing import CliRunner

from sqrlly.cli.main import CHECKPOINT_DB, cli


def test_resume_selects_an_older_run_without_replacing_latest(tmp_path):
    config = tmp_path / "workflow.yaml"
    config.write_text(
        """
nodes:
  - id: prepare
    run: [sh, -c, 'echo "$1" >> prepared', sh, "{{ vars.label }}"]
  - id: finish
    depends_on: [prepare]
    run: [sh, -c, 'test "$1" != first || test -e allow || exit 7; echo "$1" >> finished', sh, "{{ vars.label }}"]
"""
    )
    runner = CliRunner()
    args = ["run", str(config), "--workdir", str(tmp_path), "--quiet"]

    result = runner.invoke(cli, args + ["--var", "label=first"])
    assert result.exit_code == 1, result.output
    status = ["status", str(config), "--workdir", str(tmp_path), "--json"]
    result = runner.invoke(cli, status)
    assert result.exit_code == 0, result.output
    first = json.loads(result.output)["run_id"]

    result = runner.invoke(cli, args + ["--var", "label=second"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli, status)
    assert result.exit_code == 0, result.output
    latest = json.loads(result.output)["run_id"]
    assert latest != first

    result = runner.invoke(cli, status + ["--run-id", first])
    assert result.exit_code == 0, result.output
    earlier = json.loads(result.output)
    assert earlier["run_id"] == first
    assert earlier["failed_nodes"] == ["finish"]

    (tmp_path / "allow").touch()
    result = runner.invoke(cli, args + ["--resume", "--run-id", first])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "prepared").read_text().splitlines() == ["first", "second"]
    assert (tmp_path / "finished").read_text().splitlines() == ["second", "first"]

    result = runner.invoke(cli, status + ["--history", "1"])
    assert result.exit_code == 0, result.output
    history = json.loads(result.output)
    assert history["latest_run_id"] == latest
    assert [item["run_id"] for item in history["runs"]] == [first]

    result = runner.invoke(cli, args + ["--resume-from", "finish"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "finished").read_text().splitlines() == ["second", "first", "second"]

    unrelated = tmp_path / "other.yaml"
    unrelated.write_bytes(config.read_bytes())
    result = runner.invoke(cli, ["run", str(unrelated), "--workdir", str(tmp_path), "--resume", "--run-id", first])
    assert result.exit_code == 1, result.output
    assert "No saved run" in result.output
    assert (tmp_path / "finished").read_text().splitlines() == ["second", "first", "second"]

    foreign_history = ["status", str(unrelated), "--workdir", str(tmp_path), "--history", "2"]
    result = runner.invoke(cli, foreign_history)
    assert result.exit_code == 1 and "no checkpoint" in result.output


def test_explicit_run_id_requires_resume_and_a_checkpoint(tmp_path):
    config = tmp_path / "workflow.yaml"
    config.write_text('nodes: [{id: action, run: ["sh", "-c", "touch dispatched"]}]\n')
    runner = CliRunner()
    args = ["run", str(config), "--workdir", str(tmp_path), "--quiet"]
    unknown = "0" * 32

    result = runner.invoke(cli, args + ["--run-id", unknown])
    assert result.exit_code == 2, result.output
    assert "requires --resume" in result.output
    result = runner.invoke(cli, args + ["--resume", "--run-id", unknown])
    assert result.exit_code == 1, result.output
    assert "No saved run" in result.output
    assert not (tmp_path / CHECKPOINT_DB).exists()
    assert not (tmp_path / "dispatched").exists()


def test_resume_earlier_git_run_keeps_newer_output_snapshot(tmp_path):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    git("-c", "user.name=Workflow Test", "-c", "user.email=workflow@example.test",
        "commit", "--allow-empty", "-qm", "Base")
    config = tmp_path / "workflow.yaml"
    config.write_text(
        """
nodes:
  - id: output
    worktree: isolated
    promote: false
    run: [sh, -c, 'echo "$1" > result.txt', sh, "{{ vars.label }}"]
"""
    )
    runner = CliRunner()
    args = ["run", str(config), "--workdir", str(tmp_path), "--quiet"]
    status = ["status", str(config), "--workdir", str(tmp_path), "--json"]

    result = runner.invoke(cli, args + ["--var", "label=first"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(cli, status)
    assert result.exit_code == 0, result.output
    first = json.loads(result.output)["run_id"]

    result = runner.invoke(cli, args + ["--var", "label=second"])
    assert result.exit_code == 0, result.output
    newer_ref = next(line.split()[-1] for line in result.output.splitlines() if line.startswith("Retained snapshot:"))
    assert git("show", f"{newer_ref}:result.txt") == "second"

    result = runner.invoke(cli, args + ["--resume", "--run-id", first])
    assert result.exit_code == 0, result.output
    assert git("show", f"{newer_ref}:result.txt") == "second"
