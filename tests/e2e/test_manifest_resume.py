"""Manifest edits and route cycles across real CLI checkpoints."""
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml


def test_resume_then_back_route_starts_a_fresh_manifest_wave(tmp_path):
    nodes = [
        {"id": "dispatch", "run": ["sh", "-c", """if test -e next; then echo '["b"]'; else echo '["a"]'; fi"""]},
        {"id": "work", "for_each": "dispatch", "depends_on": ["dispatch"], "worktree": "off",
         "run": ["sh", "-ec", 'test -e allow; echo "$1" >> calls', "worker", "{{item}}"]},
        {"id": "advance", "depends_on": ["work"], "run": ["sh", "-c",
         'if test -e next; then echo done; else touch next; echo again; fi']},
        {"id": "gate", "depends_on": ["advance"], "route": [
            {"when": "'again' in advance", "then": "dispatch", "max_attempts": 3}]},
    ]
    path = tmp_path / "flow.yaml"
    path.write_text(yaml.safe_dump({"nodes": nodes}))
    argv = [str(Path(sys.executable).with_name("sqrlly")), "run", str(path), "-w", str(tmp_path)]
    failed = subprocess.run(argv, capture_output=True, text=True)
    assert failed.returncode == 1 and "work" in failed.stdout
    (tmp_path / "allow").touch()
    recovered = subprocess.run([*argv, "--resume"], capture_output=True, text=True)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert (tmp_path / "calls").read_text().splitlines() == ["a", "b"]


def test_warn_shrink_prunes_checkpoint_and_readdition_runs_again(tmp_path):
    manifest, config = tmp_path / "items.json", tmp_path / "flow.yaml"
    config.write_text(yaml.safe_dump({"settings": {"on_manifest_drift": "warn"}, "nodes": [
        {"id": "work", "for_each": {"file": "items.json"}, "worktree": "off",
         "run": ["sh", "-ec", 'echo "$1" >> calls', "worker", "{{item.id}}"]}]}))
    argv = [str(Path(sys.executable).with_name("sqrlly")), "run", str(config), "-w", str(tmp_path)]
    def run(ids, args):
        manifest.write_text(json.dumps([{"id": key} for key in ids]))
        result = subprocess.run([*argv, *args], capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        return next(line for line in result.stdout.splitlines() if line.strip().startswith("Nodes:"))
    assert "work::b" in run(["a", "b", "c"], [])
    assert run(["a"], ["--resume"]).strip() == "Nodes: work, work::a"
    assert "work::b" in run(["a", "b"], ["--resume"])
    assert sorted((tmp_path / "calls").read_text().splitlines()) == ["a", "b", "b", "c"]


def test_interrupt_repeated_child_id_keeps_current_wave_tree(tmp_path):
    base = tmp_path / "repo"
    base.mkdir()
    def git(*args):
        subprocess.run(["git", "-C", str(base), *args], check=True, capture_output=True)
    git("init", "-q")
    git("-c", "user.email=test@example.org", "-c", "user.name=Test", "commit", "--allow-empty", "-qm", "seed")
    waiting, release = tmp_path / "waiting", tmp_path / "release"
    nodes = [
        {"id": "plan", "run": ["sh", "-ec", """if test "$(cat round)" = 2; then echo '[{"id":"b"}]'; else echo '[{"id":"a"}]'; fi"""]},
        {"id": "work", "for_each": "plan", "depends_on": ["plan"], "run": ["sh", "-ec",
         'echo "$1" >> results; if test "$(cat round)" = 3; then touch "$2"; while test ! -e "$3"; do sleep .05; done; fi',
         "worker", "{{item.id}}", str(waiting), str(release)]},
        {"id": "advance", "depends_on": ["work"], "run": ["sh", "-ec", 'echo $(( $(cat round) + 1 )) > round; cat round']},
        {"id": "gate", "depends_on": ["advance"], "route": [{"when": "'4' not in advance", "then": "plan", "max_attempts": 4}]},
    ]
    (base / "round").write_text("1")
    config = base / "flow.yaml"
    config.write_text(yaml.safe_dump({"nodes": nodes}))
    cli = [str(Path(sys.executable).with_name("sqrlly")), "run", str(config), "-w", str(base)]
    proc = subprocess.Popen(cli, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 20
        while not waiting.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert waiting.exists(), proc.communicate() if proc.poll() is not None else "wave three did not start"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        release.touch()
    recovered = subprocess.run([*cli, "--resume"], capture_output=True, text=True)
    assert recovered.returncode == 1 and "Interrupted dispatch" in recovered.stderr
    assert (base / "results").read_text().splitlines() == ["a", "b"]
    trees = list((base / ".sqrlly").glob("wt-*/results"))
    assert [path.read_text().splitlines() for path in trees] == [["a", "b", "a"]]
