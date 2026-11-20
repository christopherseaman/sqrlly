# sqrlly — Operator Notes for Claude

Workflow orchestrator using LangGraph for graph topology and local agent
CLIs (Codex by default, Claude opt-in) / scripts for execution.

This file is operational guidance for Claude Code working inside this
repo. Narrative documentation lives elsewhere:

- **`README.md`** — what the project is, install, quickstart, concept
  tour, CLI overview, examples gallery (PyPI-facing front page).
- **`SCHEMA.md`** — the exhaustive field-by-field schema reference
  for node kinds, settings, routing and evaluation.
- **`SKILLS.md`** — agent skill doc: instructions for an AI coding
  agent authoring and running sqrlly workflows (Codex skill format).
- **[GitHub issues](https://github.com/christopherseaman/sqrlly/issues)** — all
  open work. Pick, claim, hand off, ask, park and close issues as
  **`docs/reference/issues.md`** says; file a new issue for any finding or
  decision. No backlog lives in a file.
- **`CHANGELOG.md`** — release history.
- **`docs/reference/grammar-1.0.md`** — the spec for the 1.0 line (node
  grammar, eval and route, outputs, worktrees). Issues cite the relevant
  sections when they change the grammar.

Read those when narrative context is needed; this file stays focused on
day-to-day operator concerns (build, test, layout, limitations,
environment quirks).

## Work tracking

Read this before starting any work. The full rules are in
`docs/reference/issues.md`.

- **Start at `HANDOFF.md`** when it exists: it names the current work and
  the order. Delete or rewrite it when it goes stale.
- **Every piece of work is a GitHub issue.** An issue makes an artifact or
  builds something, and has a "Done when" that names a test or an exact
  output. File one before starting work that has none.
- **A spec is a file in the repo**, under `docs/reference/`, committed to
  main. An issue cites the file and its section. An issue body never serves
  as the spec for another issue. A decision is recorded in the file it
  changes, with its date.
- **Labels are the state.** Each open issue has one `status:` label
  (`untriaged`, `ready`, `in-progress`, `feedback`, `blocked`, `parked`).
  The [project board](https://github.com/users/christopherseaman/projects/4)
  mirrors them; `tools/issue-queue.py board` syncs it.
- **Pick up:** list `status:ready` issues, skip any with a live claim,
  comment `Claim: <agent> <branch>`, set `status:in-progress`.
- **While working:** comment `Progress:` at least every 4 hours. Work on a
  branch named for the issue (`issue-<n>-<slug>`) and push it.
- **Stop:** `Handoff:` (what is done, the branch, what remains) then
  `status:ready`. `Question:` plus `status:feedback` asks the owner.
  `Blocked by #N` in the body plus `status:blocked` waits on another issue.
- **Finish:** merge to main when tests pass, comment `Result:` with the
  commit and the test result, close the issue.
- **Every agent comment** starts with one of `Claim:`, `Progress:`,
  `Question:`, `Result:`, `Handoff:`, `Takeover:`, `Triage:`, `Note:`. A
  comment with none of them counts as the owner's.
- **Owner only:** a minor or major release, a schema removal outside an
  agreed spec, a direction call. Issues carrying `needs:owner` are not
  claimed; add evidence to them.
- **Never** open an issue, pull request or comment on another project.

## Architecture (30-second sketch)

```
Workflow YAML → Pydantic Graph → build_workflow_graph() → LangGraph
  agent / llm → provider CLI via DispatchExecutor
  run         → command via DispatchExecutor
  workflow    → recursively compiled subgraph
  for_each    → Send workers → join outputs in manifest order

Top-level eval or route → _eval_<id> runs any measurement and settles the route.
for_each workers run their eval and retry loop before the join.
ForemanExecutor manages worktrees and settles Git snapshots for promotion.
```

The three-layer breakdown is in "Project Layout" below; layer rules are
enforced by `tests/architecture/test_layers.py`.

## Build & Test

```bash
uv sync
# Put Node.js on PATH for JavaScript eval tests.
uv run pytest tests/ --ignore=tests/cli -m 'not live' -v
# Install and authenticate Codex and Claude before running tests/cli.
uv run pytest tests/cli -v
uv run pytest tests/e2e/test_live_backend_roundtrip.py -m live -v  # authenticated Codex on PATH
uv run pytest tests/architecture/test_layers.py  # layer rule enforcement

uv run sqrlly validate config.yaml
uv run sqrlly run config.yaml
uv run sqrlly run config.yaml --resume    # resume from checkpoint
uv run sqrlly run config.yaml --entry <node>  # cold-start: run <node> + downstream, no checkpoint
uv run sqrlly run config.yaml --safe-mode  # Claude-only clean mode; no effect on Codex
uv run sqrlly run config.yaml --log out.jsonl
uv run sqrlly graph config.yaml           # Mermaid topology
uv run sqrlly view config.yaml            # self-contained HTML viewer
```

CLI commands: `init`, `validate`, `run`, `graph`, `view`. There is no
`migrate` subcommand — the pre-Stage-4 legacy-YAML migrator was removed
entirely (no published release ever accepted the legacy shapes; recover
via `git show <pre-cutover-sha>:src/sqrlly/cli/migrate.py` if ever needed).

## Versioning

**Push to main and release are different operations.** Commits land
on main freely as work completes and tests pass. A *release*
(`scripts/release.sh ...`) happens only when there's something worth
surfacing to users — typically a batch of commits since the last
tag, not every individual push.

When releasing:

- Default to **patch**. Bug fixes, docs, polish, small additions
  (e.g., a CLI subcommand on the scale of `init`) bundle into a
  patch. Multiple commits per patch is normal.
- **Minor** (`0.x.0`) is reserved for substantial new features or
  surface changes that warrant a changelog heads-up — the kind of
  thing a user tracking releases should be alerted to, not folded
  into routine maintenance. Examples that earned minor: the
  `transport: api` strip (0.2.0), `transport: cli` addition (0.3.0).
- **Always confirm with the operator before a `minor` bump.** The
  operator decides what constitutes "worth a heads-up."

Mechanically: `scripts/release.sh patch` is the only no-confirmation
default; `minor` and `major` need explicit operator approval.

## Project Layout

Three-layer split (enforced by `tests/architecture/test_layers.py`):

**`src/sqrlly/schema/`** — Pydantic models (no langgraph imports).
- `models.py` defines the graph, nodes, settings and fan-out constraints.
- `v1.py` defines kind payloads, defaults, output contracts, eval and route cases.

**`src/sqrlly/compile/`** — YAML → LangGraph (no cli imports).
- `graph.py` wires execution nodes to a settlement node (`_eval_<id>`) for eval and route. The repeating node dispatches its items to a worker. A join collects completed children in manifest order.
- `nodes.py` builds execution context and settles ordered route cases. The settlement update records the attempt, measurement, destination and forwarded context.
- `dynamic.py` runs fan-out workers, retries items and joins their outputs.
- `subgraph.py` — recursive composition, cycle detection, depth cap.
- `route.py` — simpleeval namespace + predicate evaluation
  (langgraph-free).
- `evaluation.py` selects the first matching case and detects exhausted attempt limits.
- `lint.py` — `collect_warnings()` — pure, langgraph-free advisory
  footgun checks (non-fatal); surfaced by `validate` + `run`.

**`src/sqrlly/runtime/`** — executors, backends, gates, foreman
(no compile/langgraph imports).
- `state.py` — `WorkflowState` TypedDict + `REDUCERS`.
- `result.py` — `ExecutionResult`, `NodeExecutor` Protocol,
  `PromptBackend` Protocol, `OverloadError`.
- `runner.py` — `astream` loop, state-diff event detection.
- `logging.py` — `JsonlLogger`, `SubgraphLogger` (prefix decorator).
- `terminal.py` — `TerminalRenderer`, `TeeLogger` — live TTY event
  renderer (JsonlLogger-shaped `emit`/`log_update`/`close`).
- `gates.py` runs command and prompt evals, builds route context and checks output files.
- `foreman.py` manages isolated worktrees, throttles creation and settles snapshots.
- `memory_gate.py` — `wait_for_memory` (optional, default-off host-memory
  back-pressure; isolates the sole `psutil` dependency).
- `snapshots.py` and `promote.py` capture Git refs through a temporary index, compare base blobs and apply the settled delta.
- `settings_merge.py` — `merge_settings(parent, child)` for
  scope-aware inheritance.
- `worktree_share.py` prepares dependency links and runs setup commands in new worktrees.
- `executor/dispatch.py` dispatches commands and prompts. Its `max_parallel_jobs`
  semaphore applies in both Git and non-Git directories.
- `executor/prompt.py` renders templates and handles prompt retries and model downgrades.
- `executor/backends/cli.py` invokes Codex or Claude as a local subprocess.
- `executor/backends/_overload.py` — `maybe_raise_overload` +
  `OVERLOAD_SUBSTRINGS` (transient-error text mapped to
  `OverloadError`).

**`src/sqrlly/cli/`** — entry point + helpers.
- `main.py` — Click CLI (`init` / `validate` / `run` / `graph` / `view`);
  wires `AsyncSqliteSaver`, `ForemanExecutor`, `thread_id`,
  `JsonlLogger`.
- `view.py` — `view` command: self-contained HTML workflow viewer.
- `init.py` scaffolds a minimal `workflow.yaml` or a bundled example
  (`init --example NAME`, `init --list-examples`). `init --skill` installs
  `SKILLS.md`, bundled as `sqrlly/_skill.md`, into the working repository
  at `.agents/skills/sqrlly/SKILL.md`.

## Testing principles

These guide all new tests; violations should be flagged in review.

1. **No mocks of external systems.** Tests use real subprocess / real
   validators. `MockExecutor` (`tests/mock_executor.py`) is
   a custom test double implementing the `NodeExecutor` Protocol —
   NOT `unittest.mock`.
2. **Tests validate output, not just absence-of-error.** Every test
   asserts a specific value: an output string, a state key, a file's
   contents, a graph shape. "Did not raise" is not a passing
   assertion.
3. **Known-good AND known-bad fixtures for function-level tests.**
   Every helper gets a success path AND a failure path. Use
   `@pytest.mark.parametrize` for routing tables.
4. **Multi-function E2E tests use simple, scenario-scoped workflows.**
   Linear / diamond / fan-out / resume each get a fixture
   tailored to the scenario.
5. **No separate test codepaths in source.** Functions must not have
   `if testing:` branches. If a function can't be tested without
   special-casing, redesign it.
6. **No skip-around-the-bug.** No `try/except: pytest.skip(...)` to
   mask missing dependencies.
7. **If testing is impossible** (missing auth, unreliable env): STOP
   and raise the question. Do not paper over.
8. **Quality > count.** A few tests that pin meaningful output beat
   many tests that only check for absence of exceptions.
9. **Layer boundary tests** (`tests/architecture/test_layers.py`)
   enforce the three-layer split via AST walking. New source files
   must respect the import rules.
10. **Provider tests use authenticated local CLIs.** Tests in `tests/cli/`
    require both `codex` and `claude` on PATH. The `live` round-trip in
    `tests/e2e/test_live_backend_roundtrip.py` runs `examples/jokes/` through
    Codex and skips if `codex` is missing from PATH. Use `-m 'not live'`
    to exclude the round-trip from ordinary test runs.

The `feedback_no_fake_backends.md` memory expands on (1). Use real
subprocesses for provider responses. Tests may instrument orchestration
without supplying fake provider responses.

## Known limitations

- **`--entry <node>` starts from fresh state.** `cli/main.py::_seed_state`
  uses `compile/resume.py::compute_skip_set` to freeze nodes outside the entry's
  downstream closure. The entry runs with empty upstream template bindings.
  It can read upstream artifacts already on disk. `--entry` is mutually
  exclusive with `--resume`, `--resume-from` and `--rerun-all`. Name a
  top-level node, including the parent of fan-out children or subgraph nodes.
- **CLI backend timeout and cancellation signal the process group.** The backend
  starts each provider CLI in its own session with `start_new_session=True`.
  `runtime/executor/backends/cli.py::_kill_process_group` sends SIGTERM,
  waits briefly, then sends SIGKILL if the group still exists.
- **Hyphenated node IDs in Jinja templates** — `{{research-phase}}`
  parses as subtraction; use underscores. `validate`/`run` emit an
  advisory warning (`compile/lint.py`).
- `graph` and `view` render authored dependencies and routes. A ×N stand-in beside each repeating node shows where its workers run. Realized child ids appear in the run log.
- Successful isolated worktrees are removed after their output ref is captured. Failed trees stay available. A clean run prunes earlier inactive runs’ refs for that workflow.
- **`--resume` skips completed nodes (0.6.0)** — bare `--resume` reseeds
  the prior checkpoint and skips nodes that completed cleanly and aren't
  downstream of a failure (`compile/resume.py::compute_skip_set` → the
  `_resume_skip` frozen snapshot channel; guards in `compile/nodes.py` /
  `compile/graph.py` read the frozen set).
  `--rerun-all` forces the pre-0.6 full replay; `--resume-from <node>`
  re-runs a node + its downstream. For a fan-out parent target: only
  non-completed children re-run (completed siblings are frozen, not
  re-billed); failed children always re-run. Resume skips or re-runs a subgraph as a
  unit using its top-level reference node.
- **Subgraph event prefix nests hierarchically** — child events are prefixed
  with their parent's id (`parent::child`), and nested subgraphs compose
  naturally into deeper hierarchies (`parent::child::grandchild`). No
  collision risk; child ids need only be unique within their own subgraph.
- **Checkpointer migration** — pre-refactor `.sqrlly-state.json` is
  ignored on `--resume`; re-run from scratch.
- Route context is keyed by destination. A selected case forwards its measurement according to `context`.
- Promote compares the current base blob with the input snapshot before applying the result. The default conflict policy fails the node. `promote: false` retains the output ref and leaves the base alone.
- A malformed item collection or duplicate explicit id fails the repeating node before children run.
- **JSONL `gate_evaluated` precedes `node_completed` or `node_failed`.**
  `runtime/logging.py::_emit_update_events` emits the measurement before
  the outcome when the settlement update contains both.
- `settings.defaults.agent.backend_max_retries` and
  `settings.defaults.llm.backend_max_retries` default to `2`.
  `runtime/executor/prompt.py::execute_with_downgrade` tries model downgrades
  on overload before spending a backend retry. Each backend retry restarts
  from the original model after a delay from `settings.retry_backoff`.
  A route case with `max_attempts` stops matching once the node reaches that attempt.
  Prompt evals also use the dispatch retry budget. An exhausted eval dispatch
  raises `EvaluationBackendError`, and `compile/nodes.py::settle` records
  the dispatch failure kind.
- Commands default to zero backend retries. Set
  `settings.defaults.run.backend_max_retries` to change that budget.
- **`settings:` rejects unknown keys.** `sqrlly validate` reports the offending key.
- A repeating node reconstructs its ordered output on resume. Stable explicit ids or value hashes with duplicate ordinals preserve completed children when new items appear. Dropped ids trigger `settings.on_manifest_drift`; an edited id-less item counts as dropped.
- A Claude agent node can coordinate subagents with
  `agent: {prompt: coordinator.md, provider: anthropic, tools: [Task]}`.
  The coordinator and its subagents share one sqrlly node's worktree and
  settlement. See `SKILLS.md` for the pattern.

## Environment quirks

- **Python 3.14** emits a Pydantic V1 compatibility warning from
  `langchain_core`. Tests use `sys.executable` for Python subprocesses.
- **`tests/__init__.py` must NOT exist** — its presence breaks
  `from helpers import ...` and `from mock_executor import ...` in
  tests. Removing it is the supported state.

## Quick references

| Task | Where to look |
|---|---|
| New schema field | `src/sqrlly/schema/models.py` or the payload in `src/sqrlly/schema/v1.py` |
| New backend | `src/sqrlly/runtime/executor/backends/` + factory case |
| New CLI flag | `src/sqrlly/cli/main.py` `@click.option` decorators |
| New node type | `src/sqrlly/compile/graph.py` (registration) + `src/sqrlly/compile/nodes.py` (factory) |
| New eval payload or execution | `src/sqrlly/schema/v1.py::Eval`, `src/sqrlly/runtime/gates.py::run_evaluation` |
| New WorkflowState field | `src/sqrlly/runtime/state.py` (TypedDict + REDUCERS) — beware of parity invariant in `compile/dynamic.py::_merge_updates` |
| Inline routing (route block, sender bindings, case context) | `src/sqrlly/schema/v1.py::Case`, `src/sqrlly/compile/nodes.py::settle`, `src/sqrlly/runtime/gates.py::build_eval_preamble` |
| Layer rule violation | `tests/architecture/test_layers.py` errors point to the offending file |
| Transient-error → `OverloadError` mapping | `src/sqrlly/runtime/executor/backends/_overload.py::maybe_raise_overload` (`OVERLOAD_SUBSTRINGS`) |

When in doubt before changing compile or runtime layer code, re-read the
"Known limitations" section above and the `tests/architecture/test_layers.py`
rules — several non-obvious invariants there bite contributors who
haven't seen them before.
