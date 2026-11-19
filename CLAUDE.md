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
  grammar, eval and route, outputs, worktrees). Issues that build 1.0 cite
  its sections; the remaining 1.0 stages are tracked by issue.

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
YAML → Pydantic Graph → build_workflow_graph() → compiled LangGraph
                                                         │
                                                         ▼
                              Execution Node → ForemanExecutor → DispatchExecutor
                                    │         (queue +              │
                                    │          worktree             │
                                    │          pool)        ┌───────┼───────┐
                                    │                       ▼       ▼       ▼
                                    │                    Prompt  Script  Binary
                                    ▼                                Subgraph
                              Gate node (script or LLM)
                                    │        (one Command: record + outcome)
                                  ┌─┼─┐
                                  ▼ ▼ ▼
                                pass retry fail   (gate classifies + gotos in one node)
```

The three-layer breakdown is in "Project Layout" below; layer rules are
enforced by `tests/architecture/test_layers.py`.

## Build & Test

```bash
uv sync                                      # core deps
# `codex` and `claude` CLIs on PATH          # for cli tests

uv run pytest tests/ --ignore=tests/cli -v   # ~1k tests, ~50s
uv run pytest tests/cli -v                   # CLI tests, requires `codex` and `claude` on PATH
uv run pytest -m live -v                     # restored live-roundtrip (cli-only)
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
- `models.py` defines the graph, nodes, settings, gates, routing and fan-out.
- `v1.py` defines the agent, llm, run and workflow payloads.

**`src/sqrlly/compile/`** — YAML → LangGraph (no cli imports).
- `graph.py` wires execution nodes to a settlement node (`_eval_<id>`) for eval and route. The repeating node dispatches its items to a worker. A join collects completed children in manifest order.
- `nodes.py` builds execution context and settles ordered route cases. The settlement update records the attempt, measurement, destination and forwarded context.
- `dynamic.py` — fan-out via Send, inline retry loop.
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
- `foreman.py` — `ForemanExecutor` (worktree pool + worktree-CREATION
  throttle; the dispatch concurrency cap moved to `DispatchExecutor` so it
  applies off-git too — foreman gates only creation, no double-count).
- `memory_gate.py` — `wait_for_memory` (optional, default-off host-memory
  back-pressure; isolates the sole `psutil` dependency).
- `snapshots.py` and `promote.py` capture Git refs through a temporary index, compare base blobs and apply the settled delta.
- `settings_merge.py` — `merge_settings(parent, child)` for
  scope-aware inheritance.
- `worktree_share.py` prepares dependency links and runs setup commands in new worktrees.
- `executor/dispatch.py` — `DispatchExecutor` (node kind dispatch; owns
  the `max_parallel_jobs` dispatch semaphore — always applied, foreman or
  not, so a non-git fan-out is still throttled).
- `executor/prompt.py` — module functions `apply_preamble` +
  `execute_with_downgrade` (template render, eval preamble, model
  downgrade); flattened from the former `PromptExecutor` class.
- `executor/backends/cli.py` invokes Codex or Claude as a local subprocess.
- `executor/backends/_overload.py` — `maybe_raise_overload` +
  `OVERLOAD_SUBSTRINGS` (transient-error text mapped to
  `OverloadError`).

**`src/sqrlly/cli/`** — entry point + helpers.
- `main.py` — Click CLI (`init` / `validate` / `run` / `graph` / `view`);
  wires `AsyncSqliteSaver`, `ForemanExecutor`, `thread_id`,
  `JsonlLogger`.
- `view.py` — `view` command: self-contained HTML workflow viewer.
- `init.py` — `init` command: scaffolds a minimal workflow
  (`workflow.yaml` with a greeting command) for `pipx`-installed users
  with no repo on disk. `init --skill` instead installs the agent
  skill doc (repo-root `SKILLS.md`, `force-include`d into the wheel as
  `sqrlly/_skill.md`) into a working repo at
  `.agents/skills/sqrlly/SKILL.md` (repo-aware via git toplevel).

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
10. **Live-backend tests use `pytest.mark.live` + `pytest.mark.skipif(
    KEY is None, ...)`.** Self-skipping when the matching API key is
    absent on disk; opt-in for isolation via `pytest -m live`. Never
    fail loudly without a key — silent skip is the contract. The
    round-trip suite at `tests/e2e/test_live_backend_roundtrip.py`
    exercises the full CLI pipeline against `examples/jokes/` per
    backend.

The `feedback_no_fake_backends.md` memory expands on (1): no fake
`PromptBackend` doubles; real subprocess only. The one
sanctioned exception is orchestration instrumentation (e.g. patching
`factory.shutil.which` to choose which environment-shape branch the
resolver sees) — distinct from faking what an external system returns.

## Known limitations

- **`--entry <node>` is a COLD start, not a resume** — `sqrlly run
  --entry <node>` seeds FRESH state (no checkpoint read), freezes everything
  upstream of `<node>`, and runs `<node>` + its downstream
  (`cli/main.py::_execute_workflow` reuses `compile/resume.py::compute_skip_set`
  with `prior_completed = {all node ids}`, `prior_failed = set()`,
  `rerun_targets = {entry}`). Mutually exclusive with `--resume` /
  `--resume-from` / `--rerun-all`. **The entry node trusts ON-DISK inputs:**
  upstream never ran this session, so `node_outputs` is empty and a `{{upstream}}`
  Jinja var resolves to nothing — an `--entry` node must READ FILES, not
  interpolate upstream output. Rejects `::` fan-out child ids (name the
  top-level parent). v1: like `--resume`, subgraph inner nodes are not
  individually addressable as the entry.
- **CLI backend kills the whole process group on timeout** — the CLI backend
  spawns the provider CLI with `start_new_session=True` (own process group) and, on
  timeout/cancel, SIGTERM→SIGKILLs the process GROUP
  (`runtime/executor/backends/cli.py::_kill_process_group`), so descendants
  (MCP servers, test runners, headless browsers) are reaped too. The CLI
  parent is still alive (the group leader) when we signal, so a single
  `killpg` reaches the whole tree.
- **Hyphenated node IDs in Jinja templates** — `{{research-phase}}`
  parses as subtraction; use underscores. `validate`/`run` emit an
  advisory warning (`compile/lint.py`).
- `graph` and `view` render authored dependencies and routes. A ×N stand-in beside each repeating node shows where its workers run. Realized child ids appear in the run log.
- Successful isolated worktrees are removed after their output ref is captured. Failed trees stay available. A clean run prunes earlier inactive runs’ refs for that workflow.
- **`--resume` skips completed nodes (0.6.0)** — bare `--resume` reseeds
  the prior checkpoint and skips nodes that completed cleanly and aren't
  downstream of a failure (`compile/resume.py::compute_skip_set` → the
  `_resume_skip` frozen snapshot channel; guards in `compile/nodes.py` /
  `compile/dynamic.py` read it, never the live `completed_nodes`).
  `--rerun-all` forces the pre-0.6 full replay; `--resume-from <node>`
  re-runs a node + its downstream. For a fan-out parent target: only
  non-completed children re-run (completed siblings are frozen, not
  re-billed); failed children always re-run regardless. **v1 limitation:**
  subgraph *inner*
  nodes aren't individually skippable — a subgraph re-runs in full unless
  its reference node completed cleanly.
- **Subgraph event prefix nests hierarchically** — child events are prefixed
  with their parent's id (`parent::child`), and nested subgraphs compose
  naturally into deeper hierarchies (`parent::child::grandchild`). No
  collision risk; child ids need only be unique within their own subgraph.
- **Checkpointer migration** — pre-refactor `.sqrlly-state.json` is
  ignored on `--resume`; re-run from scratch.
- Route context is keyed by destination. A selected case forwards its measurement according to `context`.
- Promote compares the current base blob with the input snapshot before applying the result. The default conflict policy fails the node. `promote: false` retains the output ref and leaves the base alone.
- A malformed item collection or duplicate explicit id fails the repeating node before children run.
- **JSONL `gate_evaluated` precedes `node_completed`/`node_failed`** — the collapsed gate emits its EvaluationRecord and its outcome (completed / failed) in ONE super-step update, so `runtime/logging.py::_emit_update_events` orders `gate_evaluated` BEFORE both `node_completed` and `node_failed` ("gate ran, then the node settled"). For the pre-collapse split top-level pair the record already landed a super-step earlier, so this preserves that order; it is a VISIBLE JSONL order change for fan-out children and dynamic gated parents (whose single merged update previously emitted `node_completed` first). In-repo consumers (`terminal.py`, `view.py`) are order-insensitive.
- `settings.defaults.agent.backend_max_retries` and `settings.defaults.llm.backend_max_retries`
  (default `2`) control retries of the SAME backend dispatch on a backend exception
  (a `claude exited 1` blip) up to N times with `retry_backoff` between
  attempts, wrapped around the `OverloadError`→downgrade loop in
  `runtime/executor/prompt.py::execute_with_downgrade`. Distinct from the
  route case’s `max_attempts`. An overload walks the model-downgrade
  chain first (no retry consumed); an exhausted chain (immediate for a
  model outside it, e.g. every Codex model) then consumes a retry and
  re-dispatches from the original model. `0` = terminal on first backend
  error or exhausted chain. `.md` LLM gates dispatch through the same
  function (`runtime/gates.py::run_evaluation`); a gate dispatch that
  still fails raises `EvaluationBackendError`, which
  `compile/nodes.py::settle` turns into a node failure carrying the
  dispatch's kind (no evaluation record, no node retry).
- **`settings:` rejects unknown keys (`extra=forbid`, 0.7.6)** — a typo'd or stale `settings` key now raises a `ValidationError` at load (previously silently ignored). Every other schema model already enforced this; `settings` was the last to adopt it. Run `sqrlly validate` to surface the offending key.
- A repeating node reconstructs its ordered output on resume. Stable explicit ids or value hashes with duplicate ordinals preserve completed children when new items appear. Dropped ids trigger `settings.on_manifest_drift`; an edited id-less item counts as dropped.
- **No first-class managed-team / oversight node** — there is no supervised
  coordinator primitive, and none is planned near-term. The supported approach
  is an authoring pattern: a prompt node granted the `Task` tool
  (`agent.tools: [Task]` with `provider: anthropic`) runs a coordinator prompt and spawns/supervises
  sub-agent members via Claude's own agent loop (oversight inside one node).
  Members are sub-agents within that single node, NOT isolated sqrlly nodes
  (no per-member worktree/gate/promote). See `SKILLS.md` for the full pattern.

## Environment quirks

- **Python 3.14**: `pydantic.v1` deprecation warning from
  `langchain_core` is harmless. The system runs on 3.14.2 in this
  env; `python` binary is unavailable — use `sys.executable` for
  subprocess calls in tests.
- **`tests/__init__.py` must NOT exist** — its presence breaks
  `from helpers import ...` and `from mock_executor import ...` in
  tests. Removing it is the supported state.

## Quick references

| Task | Where to look |
|---|---|
| New schema field | `src/sqrlly/schema/models.py` (mind layer rules) |
| New backend | `src/sqrlly/runtime/executor/backends/` + factory case |
| New CLI flag | `src/sqrlly/cli/main.py` `@click.option` decorators |
| New node type | `src/sqrlly/compile/graph.py` (registration) + `src/sqrlly/compile/nodes.py` (factory) |
| New gate validator shape | `src/sqrlly/runtime/gates.py::run_evaluation` |
| New WorkflowState field | `src/sqrlly/runtime/state.py` (TypedDict + REDUCERS) — beware of parity invariant in `compile/dynamic.py::_merge_updates` |
| Inline routing (route block, sender bindings, case context) | `src/sqrlly/schema/v1.py::Case`, `src/sqrlly/compile/nodes.py::settle`, `src/sqrlly/runtime/gates.py::build_eval_preamble` |
| Layer rule violation | `tests/architecture/test_layers.py` errors point to the offending file |
| Transient-error → `OverloadError` mapping | `src/sqrlly/runtime/executor/backends/_overload.py::maybe_raise_overload` (`OVERLOAD_SUBSTRINGS`) |

When in doubt before changing compile or runtime layer code, re-read the
"Known limitations" section above and the `tests/architecture/test_layers.py`
rules — several non-obvious invariants there bite contributors who
haven't seen them before.
