# Recover an agent workflow after a child fails

The `edit` agent creates `message.txt` in an isolated Git worktree.
Sqrlly promotes the file before the `publish` children read it in the
base directory. The workers use `worktree: off` so they can read a
repaired destination file on resume. Alpha has a destination file at
the start. Beta gets one before resume.

Use Git, Python 3, an authenticated Codex CLI, and sqrlly with the
1.0 grammar. For an installed CLI, start in the directory created by
`sqrlly init --example agent_recovery`. From a source checkout, use
this setup first:

```bash
cd examples/agent_recovery
sqrlly() { uv run sqrlly "$@"; }
```

Create a disposable repository and run the workflow from the example directory.

```bash
scratch=$(mktemp -d)
repo="$scratch/repo"
git init -q "$repo"
printf '%s\n' Alpha > "$repo/alpha.txt"
git -C "$repo" add alpha.txt
git -C "$repo" -c user.name=Demo -c user.email=demo@example.invalid commit -qm "Add alpha destination"

sqrlly validate workflow.yaml
sqrlly run workflow.yaml --workdir "$repo" --log "$scratch/first.jsonl"
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
sqrlly run workflow.yaml --workdir "$repo" --log "$scratch/resume.jsonl" --resume
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
