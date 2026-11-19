# Handoff

Written 2026-11-19. Delete or rewrite this file when it goes stale. Rules for issues, claims and comments are in `CLAUDE.md` ("Work tracking") and `docs/reference/issues.md`.

## State

- PyPI holds v0.10.0. On main, the CLI loads kind-key workflows through `schema.models.load_workflow`. The spike in `schema/v1.py` also describes the remaining 1.0 grammar.
- The 1.0 design is final in `docs/reference/grammar-1.0.md`. Its decision table lists no open decision.
- `CLAUDE.md`, `SCHEMA.md`, `SKILLS.md` and `README.md` describe the current CLI.

## Next: the grammar changes

Work the issues in this order. Each issue body has `Known`, `Where` and `Done when`.

| Order | Issue | Work | Status |
|---|---|---|---|
| 1 | #36 | Spike the 1.0 schema models and a draft skill doc | complete |
| 1 | #37 | Delete dead surface, validate params at load (pending a 0.10.x release) | complete |
| 2 | #38 | Replace execute and presets with kind keys | complete |
| 3 | #39 | Replace gates with eval and the route case list | complete |
| 3 | #40 | Add `outputs.files` and `outputs.schema` | in progress |
| 3 | #41 | Snapshot refs, settle-time promote, new worktree defaults | in progress |
| 4 | #42 | Move fan-out to `for_each` | blocked by #40 |

An agent given only `docs/reference/skills-1.0-draft.md` authored five workflows covering linear dependencies, fan-out, eval with escalation, schema routing and subgraphs. All five and the referenced child loaded with the spike. The ten migrated examples use the engine’s kind keys.

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

  - id: integrate
    agent: integrate.md
    depends_on: [build]
    eval:
      run: [uv, run, pytest, -q]
    route:
      - when: eval.exit_code != 0
        then: retry
        max_attempts: 3
      - when: eval.exit_code != 0
        then: escalate
      - else: review

  - {id: escalate, agent: diagnose.md}
  - {id: review, workflow: flows/review.yaml}
```

The spec sections that carry the rules: 3 (kinds and matrix), 4 (defaults), 5 (outputs), 5a (eval and route), 6 (worktrees and promote).

## Rules that are easy to get wrong

- Kind keys are `agent`, `llm`, `run`, `workflow` only. No node-level `ask`, `wait`, `uses`, `with`, `gate`, `goto`, `execute` or presets. A subgraph takes inputs through `workflow.with`.
- Keep `for_each` and `route` on separate nodes.
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
- Route retries on fan-out children and persisted attempt numbers are covered by integration tests. The repeating-node grammar changes in #42.
- "5 settings" for remote prompt URLs and "53 test files" are counts from a code map, not rechecked.

## Not now

- #34 (`claude --cloud` proof of concept) and #35 (Managed Agents through `ant`): the owner said not yet.
- #28 (Codex cloud): parked on openai/codex#50182. Branch `issue-26-cloud-preset-schema` stays unmerged.
- #14 and #16 wait for the owner.

## Loose ends in the checkout

- `error.log` in the repo root is untracked output from `codex cloud`. It blocks `scripts/release.sh`, which needs a clean tree.
- Local branches named `worktree-*` are leftovers from earlier agent runs. None is needed.
