"""Interrupt and resume a workflow through the installed CLI."""

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

import pytest
import yaml


@pytest.mark.parametrize("worker_mode", ["wait", "exit"])
def test_sigint_reaps_child_and_resume_continues(tmp_path, worker_mode):
    script = tmp_path / "worker.sh"
    script.write_text(
        """if ! test -e resume; then
    trap 'wait "$child"; exit 143' TERM
    sleep 60 &
    child=$!
    printf '%s %s\\n' "$$" "$child" > started
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
    cli = [str(Path(sys.executable).with_name("sqrlly")), "run", str(config)]
    started = tmp_path / "started"
    started.touch()
    pgid = None
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
                sleep_group = os.getpgid(sleep_pid)
                assert sleep_group in {worker_pid, proc.pid} and sleep_group != os.getpgrp()
                pgid = sleep_group

                if worker_mode == "exit":
                    while time.monotonic() < startup_deadline:
                        try:
                            os.kill(worker_pid, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.02)
                    else:
                        pytest.fail("Worker did not exit before SIGINT")

                deadline = time.monotonic() + 5
                proc.send_signal(signal.SIGINT)
                exit_code = proc.wait(timeout=max(0, deadline - time.monotonic()))
                log.seek(0)
                assert exit_code == 130, log.read()
                while time.monotonic() < deadline:
                    try:
                        os.killpg(pgid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.02)
                with pytest.raises(ProcessLookupError):
                    os.kill(sleep_pid, 0)
                with pytest.raises(ProcessLookupError):
                    os.killpg(pgid, 0)
                pgid = None
                assert not (tmp_path / "result").exists()
            finally:
                if pgid is not None:
                    with suppress(ProcessLookupError):
                        os.killpg(pgid, signal.SIGTERM)
                if proc.poll() is None:
                    proc.kill()

    (tmp_path / "resume").touch()
    resumed = subprocess.run([*cli, "--resume"], cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert resumed.returncode == 0, resumed.stdout + resumed.stderr
    assert "skipping 1 completed nodes" in resumed.stdout
    assert (tmp_path / "calls").read_text() == "first\n"
    assert (tmp_path / "result").read_text() == "finished seed\n"
