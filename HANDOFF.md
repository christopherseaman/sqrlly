# Handoff

Written 2026-11-19. Delete or rewrite this file when it goes stale. Rules for issues, claims and comments are in `CLAUDE.md` ("Work tracking") and `docs/reference/issues.md`.

## State

- PyPI holds v0.10.0. The CLI still loads 0.10 YAML. The independent 1.0 schema lives in `src/sqrlly/schema/v1.py` and loads draft workflows through `load_workflow(path)`.
- The 1.0 design is final in `docs/reference/grammar-1.0.md`. Its decision table lists no open decision.
- `CLAUDE.md`, `SCHEMA.md`, `SKILLS.md` and `README.md` describe 0.10.

## Next: the grammar changes

Work the issues in this order. Each issue body has `Known`, `Where` and `Done when`.

| Order | Issue | Work | Status |
|---|---|---|---|
| 1 | #36 | Spike the 1.0 schema models and a draft skill doc | complete |
| 1 | #37 | Delete dead surface, validate params at load (ships on 0.10.x) | ready |
| 2 | #38 | Replace execute and presets with kind keys | next engine change |
| 3 | #39 | Replace gates with eval and the route case list | blocked by #38 |
| 3 | #40 | Add `outputs.files` and `outputs.schema` | blocked by #38 |
| 3 | #41 | Snapshot refs, settle-time promote, new worktree defaults | blocked by #38 |
| 4 | #42 | Move fan-out to `for_each` | blocked by #38, #40 |

An agent given only `docs/reference/skills-1.0-draft.md` authored five workflows covering linear dependencies, fan-out, eval with escalation, schema routing and subgraphs. All five and the referenced child loaded with the spike. #38 switches the engine to this grammar and migrates 10 example files and about 53 test files.

## Grammar in one screen

```yaml
settings:
  prompts: prompts/            # default; prompt names resolve here

nodes:
  - id: plan
    llm: plan.md               # one prompt, no tools, no worktree
    outputs: {schema: schemas/plan.json}

  - id: build
    agent: build.md            # CLI agent, tools, own worktree
    for_each: plan.tasks
    depends_on: [plan]
    eval:
      run: [uv, run, pytest, -q]
    route:
      - when: eval.exit_code != 0
        then: retry
        max_attempts: 3
      - when: eval.exit_code != 0
        then: escalate
      - else: review

  - {id: lint, run: uv run ruff check ., depends_on: [build]}
  - {id: review, workflow: flows/review.yaml}
```

The spec sections that carry the rules: 3 (kinds and matrix), 4 (defaults), 5 (outputs), 5a (eval and route), 6 (worktrees and promote).

## Rules that are easy to get wrong

- Kind keys are `agent`, `llm`, `run`, `workflow` only. No `ask`, `wait`, `uses`, `with`, `gate`, `goto`, `execute` or presets.
- `eval` measures and has no pass field. `route` holds every consequence, retries included.
- `max_attempts` is part of a case's match and counts node runs from 1. A case at its limit falls to the next case.
- A node fails when no case matches and at least one case was skipped for `max_attempts`.
- `retry` and `fail` are reserved `then` words. `else` must be the last case.
- Extensions (keyword registration, the human pause) come after 1.0. `NodeExecutor` stays; no `Kind` Protocol.
- 1.0 breaks the YAML once. No compatibility shim and no migrator.
- Tests use real subprocesses and real CLIs, with no mocks or fake backends (`CLAUDE.md` "Testing principles").
- A minor or major release needs the owner's approval. Commits carry no assistant attribution.

## Unverified, check before relying on it

- `claude --json-schema` and `codex exec --output-schema` were read in help text and never run.
- The snapshot-ref promote (temporary index, base-blob check, ref pruning) is unprototyped.
- `then: retry` on a `for_each` child and `attempt` across `--resume` are unprototyped.
- "5 settings" for remote prompt URLs and "53 test files" are counts from a code map, not rechecked.

## Not now

- #34 (`claude --cloud` proof of concept) and #35 (Managed Agents through `ant`): the owner said not yet.
- #28 (Codex cloud): parked on openai/codex#50182. Branch `issue-26-cloud-preset-schema` stays unmerged.
- #14 and #16 wait for the owner.

## Loose ends in the checkout

- `error.log` in the repo root is untracked output from `codex cloud`. It blocks `scripts/release.sh`, which needs a clean tree.
- Local branches named `worktree-*` are leftovers from earlier agent runs. None is needed.
