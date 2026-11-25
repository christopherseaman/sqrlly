"""Exercise a run's checkpoint ownership through competing CLI processes."""

import os
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("mode", [
    "finish",
    pytest.param("interrupt", marks=pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGINT")),
    "fresh",
])
def test_competing_resume_cannot_dispatch(tmp_path, mode):
    interrupt = mode == "interrupt"
    database = tmp_path / "effects.db"
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE effects (attempt TEXT)")

    worker = tmp_path / "worker.py"
    worker.write_text("""
import os
import sqlite3
import time
from pathlib import Path

attempt = os.environ["ATTEMPT"]

def wait_for_release():
    Path("ready").touch()
    deadline = time.monotonic() + 30
    while not Path("release").exists() and time.monotonic() < deadline:
        time.sleep(0.025)
    assert Path("release").exists(), "test did not release the worker"

if attempt == "initial":
    raise SystemExit("initial failure")
if attempt == "interrupt":
    wait_for_release()
with sqlite3.connect("effects.db") as db:
    db.execute("INSERT INTO effects VALUES (?)", (attempt,))
if attempt == "finish":
    wait_for_release()
""")
    config = tmp_path / "workflow.yaml"
    config.write_text(yaml.safe_dump({"nodes": [{"id": "write", "run": [sys.executable, str(worker)]}]}))
    executable = Path(sys.executable).with_name("sqrlly.exe" if os.name == "nt" else "sqrlly")
    cli = [str(executable), "run", str(config), "--workdir", str(tmp_path), "--quiet"]

    def run(attempt, *flags):
        return subprocess.run(
            [*cli, *flags], env=os.environ | {"ATTEMPT": attempt},
            capture_output=True, text=True, timeout=15,
        )

    def effects():
        with closing(sqlite3.connect(database)) as db:
            return db.execute("SELECT attempt FROM effects ORDER BY rowid").fetchall()

    holder_command = cli
    if mode != "fresh":
        initial = run("initial")
        assert initial.returncode == 1 and "initial failure" in initial.stderr
        assert effects() == []
        holder_command = [*cli, "--resume"]
    holder = subprocess.Popen(
        holder_command, env=os.environ | {"ATTEMPT": "interrupt" if interrupt else "finish"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        ready = tmp_path / "ready"
        deadline = time.monotonic() + 15
        while not ready.exists() and holder.poll() is None and time.monotonic() < deadline:
            time.sleep(0.025)
        assert ready.exists()
        expected = [] if interrupt else [("finish",)]
        assert effects() == expected
        for flags in (("--resume",), ("--resume-from", "write"), ("--resume", "--rerun-all")):
            contender = run("contender", *flags)
            assert contender.returncode == 1, contender.stdout + contender.stderr
            assert "This workflow run is already active" in contender.stderr
            assert effects() == expected
        if mode == "fresh":
            fresh = run("fresh")
            assert fresh.returncode == 0, fresh.stdout + fresh.stderr
            assert holder.poll() is None
            assert effects() == [("finish",), ("fresh",)]
    finally:
        if interrupt and holder.poll() is None:
            holder.send_signal(signal.SIGINT)
        else:
            (tmp_path / "release").touch()
        try:
            stdout, stderr = holder.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            (tmp_path / "release").touch()
            holder.kill()
            holder.communicate(timeout=15)
            raise

    assert holder.returncode == (130 if interrupt else 0), stdout + stderr
    recovered = run("recovered", "--resume")
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    final_effects = {
        "finish": [("finish",)],
        "interrupt": [("recovered",)],
        "fresh": [("finish",), ("fresh",)],
    }
    assert effects() == final_effects[mode]
    # SQLite can have a rollback journal while the holder is open.
    leases = list((tmp_path / ".sqrlly" / "run-locks").iterdir())
    assert len(leases) == (2 if mode == "fresh" else 1)
    assert all(len(lease.name) == 64 for lease in leases)
