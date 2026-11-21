"""Interrupt and resume a workflow through the installed CLI."""

import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import psutil
import pytest
import yaml


def _is_live(process):
    with suppress(psutil.NoSuchProcess):
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    return False


@pytest.mark.parametrize("worker_mode", ["wait", "exit"])
def test_sigint_reaps_child_and_resume_continues(tmp_path, worker_mode):
    script = tmp_path / "worker.sh"
    script.write_text(
        """if ! test -e resume; then
    trap 'wait "$child"; exit 143' TERM
    sleep 60 &
    child=$!
    if ! printf '%s %s\\n' "$$" "$child" > started; then
        kill "$child"
        wait "$child"
        exit 1
    fi
    if test "$2" = exit; then exit 0; fi
    wait "$child"
fi
printf '%s\\n' "$1"
"""
    )
    config = tmp_path / "workflow.yaml"
    config.write_text(yaml.safe_dump({"nodes": [
        {"id": "first", "run": ["sh", "-ec", "echo first >> calls; printf seed"]},
        {"id": "worker", "depends_on": ["first"], "run": ["sh", str(script), "{{first}}", worker_mode]},
        {"id": "finish", "depends_on": ["worker"], "run": [
            "sh", "-ec", 'printf "finished %s" "$1" > result', "finish", "{{worker}}",
        ]},
    ]}))
    run_log = tmp_path / "run.jsonl"
    cli = [str(Path(sys.executable).with_name("sqrlly")), "run", str(config), "--log", str(run_log)]
    started = tmp_path / "started"
    started.touch()
    worker = sleeper = None
    with (tmp_path / "interrupt.log").open("w+") as log:
        with subprocess.Popen(
            cli, cwd=tmp_path, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        ) as proc:
            try:
                startup_deadline = time.monotonic() + 20
                while not (pids := started.read_text()).endswith("\n"):
                    if proc.poll() is not None or time.monotonic() >= startup_deadline:
                        log.seek(0)
                        pytest.fail(f"Sleep did not start:\n{log.read()}")
                    time.sleep(0.02)
                worker_pid, sleep_pid = map(int, pids.split())
                sleeper = psutil.Process(sleep_pid)
                pgid = os.getpgid(sleep_pid)
                assert pgid in {worker_pid, proc.pid} and pgid != os.getpgrp()

                if worker_mode == "exit":
                    while psutil.pid_exists(worker_pid) and time.monotonic() < startup_deadline:
                        time.sleep(0.02)
                    assert not psutil.pid_exists(worker_pid), "Worker did not exit before SIGINT"
                else:
                    worker = psutil.Process(worker_pid)
                assert _is_live(sleeper), "Sleep exited before SIGINT"

                deadline = time.monotonic() + 5
                proc.send_signal(signal.SIGINT)
                exit_code = proc.wait(timeout=max(0, deadline - time.monotonic()))
                log.seek(0)
                assert exit_code == 130, log.read()
                if worker_mode == "exit":
                    # The orphan's new parent controls when it gets reaped.
                    while _is_live(sleeper) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    assert not _is_live(sleeper), "Orphaned sleep survived SIGINT"
                else:
                    while time.monotonic() < deadline:
                        try:
                            os.killpg(pgid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.02)
                    assert not sleeper.is_running(), "Sleep PID remained after SIGINT"
                    with pytest.raises(ProcessLookupError):
                        os.killpg(pgid, 0)
                assert not (tmp_path / "result").exists()
            finally:
                if proc.poll() is None:
                    proc.kill()
                for process in (sleeper, worker):
                    if process is not None and _is_live(process):
                        with suppress(psutil.NoSuchProcess):
                            process.terminate()

    (tmp_path / "resume").touch()
    resumed = subprocess.run([*cli, "--resume"], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert "skipping 1 completed nodes" in resumed.stdout
    assert (tmp_path / "calls").read_text() == "first\n"
    assert (tmp_path / "result").read_text() == "finished seed\n"
    events = [json.loads(line) for line in run_log.read_text().splitlines()]
    starts = [event["invocation_id"] for event in events if event["event"] == "workflow_start"]
    ends = [(event["invocation_id"], event["status"]) for event in events if event["event"] == "workflow_end"]
    assert len(set(starts)) == 2
    assert ends == [(starts[0], "interrupted"), (starts[1], "succeeded")]
