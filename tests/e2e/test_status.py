import json
from pathlib import Path
import subprocess
import sys

import yaml


def invoke(config, workdir, command, *flags):
    return subprocess.run(
        [str(Path(sys.executable).with_name("sqrlly")), command, str(config), "-w", str(workdir), *flags],
        capture_output=True, text=True, timeout=20,
    )


def test_status_lists_completed_and_failed(tmp_path):
    workdir = tmp_path / "work #1"
    workdir.mkdir()
    config = tmp_path / "workflow.yaml"
    config.write_text(yaml.safe_dump({
        "name": "release-check",
        "nodes": [
            {
                "id": "build",
                "run": [sys.executable, "-c", "print('built')"],
                "eval": {"run": [sys.executable, "-c", 'print(\'{"score": {{ attempt }}}\')']},
                "route": [{"when": "eval.score < 2", "then": "retry", "max_attempts": 2}],
            },
            {
                "id": "publish",
                "depends_on": ["build"],
                "run": [
                    sys.executable, "-c",
                    "from pathlib import Path; assert Path('approved').exists(), 'publish rejected'",
                ],
            },
        ],
    }))
    missing = invoke(config, workdir, "status")
    assert missing.returncode == 1
    assert "no checkpoint" in missing.stderr
    assert list(workdir.iterdir()) == []

    failed = invoke(config, workdir, "run", "--quiet")
    assert failed.returncode == 1, failed.stdout + failed.stderr
    db = workdir / ".sqrlly-checkpoint.db"
    saved = db.read_bytes(), db.stat().st_mtime_ns

    readable = invoke(config, workdir, "status")
    assert readable.returncode == 0, readable.stderr
    assert "Completed: build" in readable.stdout
    assert "Failed: publish" in readable.stdout
    assert "Retries for build: 1" in readable.stdout
    assert "Gate for build, attempt 2:" in readable.stdout
    assert "publish rejected" in readable.stdout

    machine = invoke(config, workdir, "status", "--json")
    assert machine.returncode == 0, machine.stderr
    report = json.loads(machine.stdout)
    assert report["workflow"] == "release-check"
    assert report["completed_nodes"] == ["build"]
    assert report["failed_nodes"] == ["publish"]
    assert report["retries"] == {"build": 1}
    assert [entry["result"]["score"] for entry in report["evaluations"]["build"]] == [1, 2]
    assert [(err["node"], err["kind"]) for err in report["errors"]] == [("publish", "node_error")]
    assert saved == (db.read_bytes(), db.stat().st_mtime_ns)

    (workdir / "approved").touch()
    newer = invoke(config, workdir, "run", "--quiet")
    assert newer.returncode == 0, newer.stderr
    current = json.loads(invoke(config, workdir, "status", "--json").stdout)
    assert current["run_id"] != report["run_id"]
    assert current["checkpoint_id"] != report["checkpoint_id"]
    assert current["completed_nodes"] == ["build", "publish"]
    resumed = invoke(config, workdir, "run", "--resume", "--quiet")
    assert resumed.returncode == 0, resumed.stderr
    assert "skipping 2 completed nodes" in resumed.stdout

    unrelated = tmp_path / "other.yaml"
    unrelated.write_bytes(config.read_bytes())
    missing = invoke(unrelated, workdir, "status")
    assert missing.returncode == 1
    assert "no checkpoint" in missing.stderr
