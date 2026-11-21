# SMB quote builder

This workflow turns a customer and currency into a small local quote app.
The plan is schema-checked, quote lines run through `for_each` in isolated Git
worktrees, verification checks promoted files, and the final node writes a
static preview plus an artifact manifest.

## Fresh disposable run

Run these commands from the repository root. Output goes to a disposable directory.

```bash
set -euo pipefail
scratch=$(mktemp -d)
git init -q "$scratch"
git -C "$scratch" config user.email test@example.invalid
git -C "$scratch" config user.name sqrlly-test
cp -R examples/experimental/smb_app/. "$scratch"/
git -C "$scratch" add .
git -C "$scratch" commit -qm seed
uv run sqrlly validate "$scratch/workflow.yaml"
uv run sqrlly run "$scratch/workflow.yaml" --workdir "$scratch" \
  --var customer="Northwind Bikes" --var currency=USD --log "$scratch/run.jsonl"
test "$(jq -r .quote_count "$scratch/build/check.json")" = 2
test -f "$scratch/preview/index.html"
test "$(jq -r '.artifacts | length' "$scratch/artifact-manifest.json")" = 3
git -C "$scratch" status --short | grep -E '^\?\? (artifact-manifest.json|build/|preview/|run.jsonl)$'
```

Expected output includes `Valid: smb-quote-builder (4 nodes)`, six completed
runtime nodes including two quote workers, `quote_count` equal to `2`, three
manifest artifacts, and four generated untracked paths from `git status --short`.
The run log contains `workflow_start`, six `node_completed`, and `workflow_end`.

Open `$scratch/preview/index.html` locally to inspect the generated preview.

The offline example uses `--var key=value` to select the customer and currency.

## Optional agent app run

`agent_workflow.yaml` asks a local Codex agent to generate the same narrow app
from `brief.md`, then validates its files and behavior without network access.
Run it in a second disposable Git repository after authenticating Codex:

```bash
agent_scratch=$(mktemp -d)
git init -q "$agent_scratch"
git -C "$agent_scratch" config user.email test@example.invalid
git -C "$agent_scratch" config user.name sqrlly-test
cp -R examples/experimental/smb_app/. "$agent_scratch"/
git -C "$agent_scratch" add . && git -C "$agent_scratch" commit -qm seed
uv run sqrlly validate "$agent_scratch/agent_workflow.yaml"
uv run sqrlly run "$agent_scratch/agent_workflow.yaml" --workdir "$agent_scratch" \
  --log "$agent_scratch/agent-run.jsonl"
jq '{required_files,browser_shape,quote_shape,test_exit_code,passed}' "$agent_scratch/app/acceptance.json"
test "$(jq -r .passed "$agent_scratch/app/acceptance.json")" = true
test -f "$agent_scratch/preview/index.html"
test -f "$agent_scratch/preview/app.js"
```

Expected acceptance output has `required_files: 4`, `browser_shape: true`,
`quote_shape: true`, `test_exit_code: 0`, and `passed: true`. The generated app
stays in the disposable repository, and `artifact-manifest.json` lists the
preview HTML, its `app.js` asset, and the acceptance report.

Serve the preview on loopback and open `http://localhost:8000/` to load its module.

```bash
python3 -m http.server 8000 --bind 127.0.0.1 --directory "$agent_scratch/preview"
```
