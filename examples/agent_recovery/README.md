# Recover an agent workflow after a child fails

The `edit` agent creates `message.txt` in an isolated Git worktree.
Sqrlly promotes the file before the `publish` children read it in the
base directory. The workers use `worktree: off` so they can read a
repaired destination file on resume. Alpha has a destination file at
the start. Beta gets one before resume.

Use Git, Python 3, an authenticated `codex` CLI on PATH, and a version
of sqrlly that supports the 1.0 workflow grammar. The commands below
use `uv` from the root of the sqrlly checkout. For a scaffold made
with `sqrlly init --example agent_recovery`, run from the scaffold
directory, set `workflow=workflow.yaml`, and use `sqrlly` in place of
`uv run sqrlly`.

```bash
workflow=examples/agent_recovery/workflow.yaml
scratch=$(mktemp -d)
repo="$scratch/repo"
git init -q "$repo"
printf '%s\n' Alpha > "$repo/alpha.txt"
git -C "$repo" add alpha.txt
git -C "$repo" -c user.name=Demo -c user.email=demo@example.invalid commit -qm "Add alpha destination"

uv run sqrlly validate "$workflow"
uv run sqrlly run "$workflow" --workdir "$repo" --log "$scratch/first.jsonl"
```

The run exits with status 1 because `publish::beta` cannot find
`beta.txt`. The disposable repository already contains the agent's
promoted file. The report waits for both children.

```bash
cat "$repo/message.txt"    # release notes ready
test ! -e "$repo/report.json"
```

Create Beta's missing destination file in the same repository. Resume
with the same workflow path and workdir to reuse the checkpoint.

```bash
printf '%s\n' Beta > "$repo/beta.txt"
uv run sqrlly run "$workflow" --workdir "$repo" --log "$scratch/resume.jsonl" --resume
cat "$repo/report.json"
```

The report contains both results in manifest order.

```json
["Alpha: release notes ready", "Beta: release notes ready"]
```

The two JSONL logs show one successful Alpha execution in the first run
and one failed Beta execution followed by one successful Beta execution.
This check also verifies one agent completion across both runs.

```bash
python3 - "$scratch" <<'PY'
import json
import sys
from collections import Counter
from pathlib import Path

root = Path(sys.argv[1])
report = json.loads((root / "repo" / "report.json").read_text())
assert report == ["Alpha: release notes ready", "Beta: release notes ready"], report
for filename, expected in [
    ("first.jsonl", {
        ("edit", "node_completed"): 1,
        ("publish::alpha", "node_completed"): 1,
        ("publish::beta", "node_failed"): 1,
    }),
    ("resume.jsonl", {
        ("publish::beta", "node_completed"): 1,
        ("report", "node_completed"): 1,
    }),
]:
    events = (json.loads(line) for line in (root / filename).read_text().splitlines())
    actual = Counter(
        (event.get("node"), event["event"])
        for event in events
        if event.get("node") in {"edit", "publish::alpha", "publish::beta", "report"}
        and event["event"] in {"node_completed", "node_failed"}
    )
    assert actual == expected, (filename, actual)
    print(f"{filename}: verified")
PY
```
