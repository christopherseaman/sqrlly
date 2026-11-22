"""Compare selective refresh with a fresh CLI run on the same item revisions."""
import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

WORKFLOW = """
name: measure-manifest-refresh
settings: {max_parallel_jobs: 6}
nodes:
  - id: work
    for_each: {file: items.json}
    worktree: "off"
    run: [sh, -ec, 'echo "$1" >> dispatched; echo "$1=$2"', worker, '{{item.id}}', '{{item.value}}']
  - id: collect
    depends_on: [work]
    run: [sh, -ec, 'printf "%s" "$1" > edition.json', join, '{{work}}']
    outputs: {files: [edition.json]}
"""


def run(cli, workflow, workspace, rows, mode):
    calls = workspace / "dispatched"
    previous = calls.stat().st_size if calls.exists() else 0
    start = time.monotonic()
    result = subprocess.run([cli, "run", str(workflow), "--workdir", str(workspace), "--quiet", *mode],
                            capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    seconds = time.monotonic() - start
    expected = [f"{row['id']}={row['value']}" for row in rows]
    actual = json.loads((workspace / "edition.json").read_text())
    if [value.rstrip("\n") for value in actual] != expected:
        raise ValueError("Joined output differs from the current ordered input")
    with calls.open("rb") as stream:
        stream.seek(previous)
        dispatched = stream.read().decode().splitlines()
    return dispatched, {"workers": len(dispatched), "elapsed_seconds": round(seconds, 3),
                        "checkpoint_bytes": (workspace / ".sqrlly-checkpoint.db").stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--items", type=int, default=128)
    args = parser.parse_args()
    if args.items < 2:
        parser.error("--items must be at least 2")
    cli = shutil.which("sqrlly")
    if not cli:
        parser.error("run with uv run python tools/bench-manifest-refresh.py")
    with tempfile.TemporaryDirectory(prefix="sqrlly-refresh-") as scratch:
        root = Path(scratch)
        workflow = root / "workflow.yaml"
        workflow.write_text(WORKFLOW)
        reused = root / "continued"
        reused.mkdir()
        rows = [{"id": f"item-{index}", "value": index} for index in range(args.items)]
        for stage in ("initial", "unchanged", "reordered", "one_revision", "one_insert"):
            changed = set()
            if stage == "initial":
                changed = {row["id"] for row in rows}
            elif stage == "reordered":
                rows.reverse()
            elif stage == "one_revision":
                rows[0]["value"] += 1000
                changed.add(rows[0]["id"])
            elif stage == "one_insert":
                rows.insert(0, {"id": "new", "value": -1})
                changed.add("new")
            (root / "items.json").write_text(json.dumps(rows))
            calls, selective = run(cli, workflow, reused, rows, [] if stage == "initial" else ["--refresh-items"])
            if sorted(calls) != sorted(changed):
                raise ValueError(f"{stage}: unexpected selective dispatches {calls}")
            fresh = root / stage
            fresh.mkdir()
            calls, rebuilt = run(cli, workflow, fresh, rows, [])
            if sorted(calls) != sorted(row["id"] for row in rows):
                raise ValueError(f"{stage}: full rebuild missed or duplicated a dispatch")
            print(json.dumps({"stage": stage, "selective": selective, "fresh": rebuilt}), flush=True)


if __name__ == "__main__":
    main()
