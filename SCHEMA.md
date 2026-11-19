# sqrlly — Workflow Schema Reference

The complete field-by-field reference for a sqrlly workflow YAML. For an
orientation and quickstart, see the [README](README.md).

---

## Graph

The top-level document.

| Field | Type | Default | Effect |
|---|---|---|---|
| `name` | `str` | optional | Workflow name. Part of the checkpoint thread id. |
| `nodes` | `list[Node]` | required | The workflow's nodes. |
| `settings` | `Settings` | `Settings()` | Global settings (see below). |

Validation rejects duplicate or reserved node ids, missing or self dependencies and unknown route destinations. A fan-out parent can measure output, but an explicit route belongs on a downstream node.

## Settings

`settings:` holds workflow-global configuration. Every field is optional.

Unknown keys in `settings:` are rejected with a `ValidationError` — no silent ignoring. To diagnose typos or removed fields, run `sqrlly validate` and check the error message.

### Retries, timeout, preamble

| Field | Type | Default | Effect |
|---|---|---|---|
| `safe_mode` | `bool` | `false` | Run Claude with `--safe-mode` (`provider: anthropic` only): operator customizations — output styles, CLAUDE.md, skills, MCP, hooks — are disabled. It has no effect on Codex. The `--safe-mode` / `--no-safe-mode` CLI flag overrides this per run. |
| `default_timeout` | `float \| None` | `None` | Per-node timeout (seconds); `None` = no timeout. |
| `preamble_file` | `str \| None` | `None` | Prepended to every prompt before Jinja rendering. A missing file is a hard error. |
| `retry_backoff` | `list[float]` | `[]` | `asyncio.sleep` seconds before each retry; clamps to the last value past the list length. |

### Models and backends

`settings.model` supplies a shared model for prompt nodes. Without a model selection the provider CLI chooses.
`settings.defaults.agent` and `settings.defaults.llm` can select a provider and model per kind.
Prompt kinds each default to two backend retries. Set `settings.defaults.agent.backend_max_retries` or `settings.defaults.llm.backend_max_retries` to change that budget.
Node options override kind defaults. Each of the four kinds accepts a default timeout.
`settings.prompts` sets the prompt directory relative to the YAML file.

### Concurrency

| Field | Type | Default | Effect |
|---|---|---|---|
| `max_parallel_jobs` | `int` | `4` | Global cap on concurrently-dispatched nodes (enforced in `DispatchExecutor`, so it applies whether or not the git-worktree foreman is active). |
| `memory_threshold_pct` | `float \| None` | `None` | Foreman blocks new dispatches while host memory percent is above this (0–100). `None` disables. |

### Worktree isolation

| Field | Type | Default | Effect |
|---|---|---|---|
| `worktree` | `Literal["auto","isolated","off"]` | `"auto"` | Graph-level isolation **mode**, inherited graph→subgraph. `auto` = isolate each node in its own git worktree iff the workdir is a git repo (else no worktree). `isolated` = force a per-node worktree. `off` (alias `none`) = no worktree; the node runs in the shared base workdir. Bare YAML `off`/`on` booleans are accepted (`off`→`off`, `on`→`isolated`). A non-mode value is rejected (a shared-tree **name** goes in `worktree_group`, not here). Per-node override: `Node.worktree`. |
| `worktree_group` | `str \| None` | `None` | Names a **shared worktree** so multiple nodes write into one tree (the "feature team" pattern). Mutually exclusive with an explicit `worktree` mode (`isolated`/`off`) — setting both is a validation error; `worktree` left at `auto` is fine. Inherited graph→subgraph; a child authoring either field clears the inherited sibling. Resolution is by scope specificity (node → subgraph → graph). |
| `worktree_gc` | `Literal["never","on_success"]` | `"never"` | Opt-in worktree cleanup. `on_success` removes every allocated worktree (per-node and shared group trees, each once) after a **clean** run (no `failed_nodes`); `never` keeps them for inspection / `--resume`. GC is end-of-run only — never mid-run — so retry-reuse and resume rehydrate are unaffected. A failed run never GCs. |
| `on_promote_conflict` | `"fail" \| "warn" \| "overwrite" \| "skip"` | `"warn"` | Resolution when two same-wave promoting nodes touch the same path. `warn` logs the overlap and applies last-write-wins (run stays green); `fail` aborts before any write; `overwrite` is silent last-write-wins; `skip` keeps the first promoting node's version (by `nodes` order) and drops the path from later nodes (their other paths still promote). Detection runs discover-first, so `fail` never half-promotes. |
| `on_manifest_drift` | `"fail" \| "warn"` | `"fail"` | Fan-out `--resume` manifest-drift policy. On resume a fan-out parent re-reads its manifest and re-fans; if that manifest DROPS a prior branch `id` (the dispatcher minted non-deterministic ids), the completed siblings silently vanish and the failed child is orphaned while all N re-run. `fail` (default) halts the parent before any `Send`, naming the vanished/orphaned ids (mirrors `on_promote_conflict: fail`); `warn` logs and proceeds with the new manifest (opt-in for an author who intends a changed manifest on resume). A stable-id or purely additive re-fan never trips it; no-op on fresh / `--entry` / `--rerun-all`. |
| `promote_exclude` | `list[str]` | `[]` | Git pathspecs filtered out of every promoting node's footprint (promote layer). Defense-in-depth keeping generated artifacts (node_modules, build caches) out of base. |
| `promote_include` | `list[str]` | `[]` | Git pathspecs RE-INCLUDED into every promoting node's footprint after `promote_exclude` removes them — the allow-list half. `promote_exclude: ["log/"]` + `promote_include: ["log/phases/**"]` promotes `log/phases/**` while dropping the rest of `log/`. (Git `:(exclude)` has no in-list negation, so this is a second pass; `include` overrides `exclude`.) |
| `worktree_share` | `list[str]` | `[]` | Read-only whole-dir symlinks of these base paths into each worktree (no install). For read-only in-branch gates (`tsc --noEmit`, scoped tests). Each is also written to the repo's shared `.git/info/exclude` (git has no per-worktree exclude) so it stays out of the promote footprint. |
| `worktree_setup` | `list[str]` | `[]` | Ordered shell commands run in each fresh worktree (e.g. `pnpm install --prefer-offline`, `pnpm exec prisma generate`). Sentinel-gated (runs once per base state); fatal-per-branch on failure. PM-agnostic. |
| `worktree_setup_exclude` | `list[str]` | `[]` | Paths written to the repo's shared `.git/info/exclude` (git has no per-worktree exclude) before promote, so rehydrate artifacts (node_modules, in-tree generated clients) stay out of the footprint. Explicit (sqrlly does not infer them). |

The memory gate composes (AND) with the semaphores — every gate must
allow dispatch. In-flight jobs are never aborted by the gate; only new
acquisitions wait.

## Node

| Field | Type | Default | Effect |
|---|---|---|---|
| `id` | `str` | required | Unique id; referenced by `depends_on` and template `{{id}}`. |
| `name` | `str` | node id | Human-readable label. |
| `description` | `str \| None` | `None` | Free text. |
| `depends_on` | `list[str]` | `[]` | Static dependencies. |
| `eval` | mapping | omitted | Measure the node output before routing. |
| `outputs` | mapping | optional | Validate files and structured results after execution. |
| `fan_out` | `FanOut \| None` | `None` | Manifest-driven `Send` fan-out. |
| `route` | case list | omitted | Select the first matching destination. |
| `timeout` | `float \| None` | `None` | Per-node timeout; falls back to `settings.default_timeout`. |
| `worktree` | `Literal["auto","isolated","off"] \| None` | `None` | Per-node isolation **mode** override; `None` inherits. Values: `auto` / `isolated` / `off` (alias `none`); bare YAML `off`/`on` booleans accepted. `off` runs the node in the shared base workdir. See `settings.worktree`. |
| `worktree_group` | `str \| None` | `None` | Per-node shared-tree name; mutually exclusive with an explicit `worktree` mode. Nodes sharing a name share one worktree. See `settings.worktree_group`. |
| `promote` | `bool` | `False` | After a **clean** run, apply this node's worktree git delta (adds/edits/deletes vs the fork point) to the base workdir. Discovers the footprint from git — no need to predict which files changed — so it handles open-ended work (bugfixes/refactors) including deletions. If `outputs.files` is set, those entries (git pathspec; `*`/`**`/`?`/`[…]`) filter what's promoted; otherwise the whole delta is promoted. Only UNCOMMITTED changes are promoted: a node that commits inside its worktree fails the promote step with an error naming the node, before any file is written. Runs before GC. Top-level nodes only. An `off` node already writes to the base workdir, so `promote` is a no-op there. Multi-source reconciliation of *overlapping* isolated trees (3-way merge) is out of scope. |

The node schema rejects unknown keys.

Choose one kind for an executable node. Joins, standalone routers and standalone gates omit the kind.
Use underscores in node ids interpolated in Jinja because hyphens parse as subtraction.

## Node kinds

| Kind | Scalar or list | Mapping |
|---|---|---|
| `agent` | Prompt filename | Exactly one of `prompt` or `text`, plus optional `model`, `provider`, `tools`, `cli_args`. |
| `llm` | Prompt filename | Exactly one of `prompt` or `text`, plus optional `model` and `provider`. |
| `run` | Command string or argv list | `cmd` and optional `env` mapping. |
| `workflow` | Workflow filename | `file` and optional `with` mapping of child inputs. |

Prompt files resolve under `settings.prompts`, defaulting to `prompts/` beside the declaring YAML.
Child workflow filenames resolve relative to the declaring YAML.
Use `{text: "Summarize {{draft}}"}` for an inline prompt.
For `llm`, Claude disables tools and MCP servers. Codex uses a read-only sandbox with the shell and configured MCP servers disabled. Codex 0.170.0 can still expose built-in resource discovery.
An agent accepts `tools: read`, `edit` (default), or `full`.
A list of tool names requires `provider: anthropic`.

A `run` command starts a process directly. The scalar form uses shell-style quoting to split argv.
Use the list form for templates or literal shell metacharacters. Each list item becomes one argument.
Use `{{workflow_dir}}` to locate a script beside the YAML when its working directory differs.

```yaml
nodes:
  - {id: draft, agent: draft.md}
  - {id: label, depends_on: [draft], llm: {text: "Label {{draft}}"}}
  - {id: check, depends_on: [label], run: [python3, "{{workflow_dir}}/check.py", "{{label}}"]}
  - {id: review, depends_on: [draft], workflow: {file: review.yaml, with: {source: "{{draft}}"}}}
  - {id: complete, depends_on: [check, review]}
```

A node with only an id and dependencies joins its predecessors without launching a process.

## Eval

An eval has exactly one of `run`, `llm` and `agent`. The command or prompt runs in the node's worktree. Pass the output explicitly to a command argument with `{{output}}`. A command exposes `eval.exit_code`, `eval.text` and fields of a single JSON object printed to stdout.

A prompt eval takes the usual prompt options, an optional `model`, `timeout` and `outputs.schema` (a workflow-relative JSON Schema file). Without a schema, a prompt eval returns numeric `score` and string `explanation`. Every measurement appends its attempt number and result to the node's history. The JSONL event `gate_evaluated` carries `node`, `attempt` and a nested `result`. For a command, read `result.exit_code`, `result.text` and any JSON fields. A prompt measurement carries `result.text` and its schema fields.

## Outputs

`outputs.files` lists file paths or globs relative to the execution worktree. Each entry must match a file. The same patterns limit which files the node promotes. Agents, commands and workflows can declare files.

`outputs.schema` names a JSON Schema file relative to the workflow YAML. Agents and LLMs pass it to their provider CLI. A command must print a single matching JSON document. Workflows can declare files, and LLMs can declare a schema.

A missing file or invalid result reruns the node with the validation error as feedback. After three unsuccessful attempts the node fails. Each execution counts toward the node’s persisted attempt number. Its eval runs after output validation succeeds, so the first measurement can record attempt two or three. Commands can include `{{_output_retry_reason}}` in an argv token to receive the feedback.

Templates can use ancestor results. `{{plan}}` renders its text, and `{{plan.count}}` reads a field when the ancestor declares a schema. Validation rejects references to a node outside the dependency or route path, field access on a node without a schema, and a route that compares a schema-less prompt node's entire text.

## Route

Cases run in order. The first matching case sends execution to its `then` destination. A destination is a node id, a list of node ids, `retry` or `fail`. The two reserved words cannot name nodes. A final `else` case is unconditional.

```yaml
eval:
  run: [uv, run, pytest, -q]
route:
  - when: eval.exit_code != 0
    then: retry
    max_attempts: 3
  - when: eval.exit_code != 0
    then: escalate
    context: all
  - else: review
```

A case with `max_attempts: 3` can match on attempts 1 and 2. At attempt 3 it falls through to the next case. If no case matches, the node completes and releases its dependents, unless a matching case exhausted its attempt limit. In that case the node fails. Attempts continue from the checkpoint on resume. A case may return to an ancestor. Set `max_attempts` to bound the loop.

Predicates can read the current node's `eval`, `attempt` and `history`, plus dependency outputs by node id. A route can branch on those outputs without an eval. Route context controls what the destination prompt receives. The default `last` includes the latest measurement and attempt. Use `all` for the full record list, `none` for no preamble, or supply a Jinja template.

With no explicit route, a command eval retries nonzero exits up to three total attempts. A prompt eval requires an explicit route.

## FanOut

A `fan_out:` block activates manifest-driven `Send` fan-out — its
presence is the activation; there is no enable flag.

| Field | Type | Default | Effect |
|---|---|---|---|
| `manifest_path` | `str \| None` | `None` | On-disk JSON manifest fallback. |
| `template` | `FanOutTemplate \| None` | `None` | What each Send branch runs. |
| `final_nodes` | `list[FanOutFinalNode]` | `[]` | Nodes that run after all branches, consuming aggregated output. |
| `promote` | `bool` | `false` | When true, each Send branch's worktree delta is promoted back to base at the end of a clean run (top-level nodes only, before GC), through `reconcile_promotions` + `on_promote_conflict` + `promote_exclude` — the native merge-back for an isolated parallel build. Distinct from `Node.promote` (which promotes the parent's own manifest-only worktree delta, not the branch deltas); setting `node.promote: true` on a fan-out parent is a no-op, and `sqrlly validate` warns to steer you to `fan_out.promote` instead. |

The manifest is read from the parent node's JSON output, falling back to
`manifest_path`. Each Send branch runs the template's kind (and
`template.eval`, with its own inline retry loop). Each branch's
template renders against a per-item Jinja context: dep outputs,
`{{<parent_id>}}` (the parent's output), and every key on the manifest
item.

**Manifest structure and validation:**

- Manifest items without an `id` field log a WARNING — each collapses onto the single `<parent_id>::unknown` child id, so two or more of them produce a duplicate child id and fail the parent (below) instead of silently collapsing N→1.
- Duplicate child ids in the manifest **fail the parent node** before dispatch (the error lists the duplicate ids; the run reports failure, and a bare `--resume` re-fans the parent once the manifest is fixed). Each item must have a unique `id` to produce one branch per item.
- An unreadable, invalid-JSON, or mis-shaped `manifest_path` fails the parent node the same way — a recoverable node failure, not an aborting exception.

**FanOutTemplate** fields:

| Field | Type | Default | Effect |
|---|---|---|---|
| `agent`, `llm`, `run`, `workflow` | kind | | Operation for each branch. |
| `eval`, `route` | measurement and case list | omitted | Measure each branch and route its result. |
| `worktree` | `"auto" \| "isolated" \| "off"` | `null` (inherit `settings.worktree`) | Per-fan-out isolation override. Lets one workflow run an isolated build fan-out AND a shared-base planner fan-out under one top-level `settings.worktree`: this value overrides `settings.worktree` for every branch of THIS fan-out. `off` runs branches in the shared base workdir (their writes are visible to a downstream join node); `isolated`/`auto` give each branch its own git worktree. Same optional-override semantics as `Node.worktree`. Applies to both workflow and executable templates. |

Final nodes take an id, an optional name and description, one kind, an optional eval and an optional output contract.
A `workflow` template loads a child graph per item.

## Fan-out resume behavior

On `--resume` after a failed run, fan-out children that failed are re-run; siblings that completed successfully are frozen (not re-billed). This asymmetry means a workflow that fans out over a manifest can efficiently re-test only the broken items without re-running good ones. The parent re-reads its manifest on re-fan; if prior branch ids are dropped, the run halts or warns per `settings.on_manifest_drift` (see the `on_manifest_drift` row) — a stable-id or purely additive re-fan never trips it.

When using `--resume-from <fan_out_parent>`, the parent re-fans over its manifest, but only non-completed children from that fan-out are dispatched — completed siblings remain frozen.

---

## Failure kinds (JSONL `node_failed.kind`)

Every `node_failed` event carries a `kind` — the classification sqrlly
already computes internally — so a consumer can decide *retry-with-resume vs
halt* on a real field instead of pattern-matching the free-text `error` (or,
worse, the node name). `workflow_end` also carries a `failed_kinds` map
(`{node_id: kind}`) built from the same records, for a post-run summary read.

| kind | meaning | disposition |
|---|---|---|
| `overload` | API 529/overloaded; the opus→sonnet→haiku downgrade chain and `backend_max_retries` were both exhausted before output. | **transient** |
| `backend_error` | Backend `send_prompt` raised a non-overload exception surviving `backend_max_retries` (e.g. a `claude exited 1` blip) — in a prompt node or in a `.md` LLM gate dispatch (the gate fails its node; no evaluation record, no node retry). | **transient** |
| `timeout` | `TimeoutError` on node execution or on a gate/evaluation await. | **transient** |
| `infra` | A worktree/setup abort during dispatch — `git worktree add`, a `worktree_share` path, or a `worktree_setup` command failed. Caught at the foreman (or, for an isolated subgraph fan-out branch, the fan-out invoker) and returned as this node's failure instead of a raw traceback. | **transient** |
| `gate_failure` | A route failed the node or exhausted a matching case — a real quality verdict on work that ran. | **deterministic** |
| `node_error` | The node (or its dispatch/config/manifest/output-contract) definitively failed; re-running unchanged reproduces it. Also the **fail-safe default** for any unclassified failure. | **deterministic** |
| `upstream_failed` | This node did **not** do the failing work: a `depends_on` id already failed, or an inner subgraph/fan-out branch node failed. The real failure is elsewhere in the same JSONL stream (a bare sibling id, or a `parent::child`-prefixed inner id). | **not-this-node** |

**Guidance, not policy.** The `disposition` column is advice, and the
`transient` label has a **deterministic tail** on all four transient kinds —
a *persistent* `overload` is a capacity crisis, a persistent `backend_error`
is a missing binary, a persistent `timeout` is a task that is simply too big,
and a persistent `infra` is a structurally-absent path or a permanently-failing
setup. So a transient kind is worth a **bounded** retry, never an unbounded one:
consumers must cap retries (worst case: waste ≤ N attempts, then halt). sqrlly
does not emit a `retryable` boolean — that would freeze a settings-dependent
policy into the wire contract; the
consumer maps `kind` → retry over this stable vocabulary.

**Fan-out granularity.** `failed_kinds` keys a fan-out child at
`<parent>::<item>` and reports *that child's* kind. For a subgraph-template
fan-out, a failure **inside** the branch surfaces here as `upstream_failed` —
the child collapses the inner outcome — and the inner root-cause kind survives
only in the `node_failed` stream, under a `<parent>::<item>::<inner>` id. A
branch-acquisition abort (an `infra` worktree/setup failure, caught *before*
the branch subgraph runs) is the exception: it never enters the branch, so it
surfaces its true `infra` kind here.

**`--resume` accumulates the log, not the state.** A reused `--log <path>`
opens append, so `node_failed` lines from every `--resume` attempt — each a
separate `sqrlly run` process emitting its own `workflow_start` /
`workflow_end` — pile up in one file, and a naive whole-file scan double-counts
a stale earlier-attempt kind. Read the **last** `workflow_end.failed_kinds`:
run *state* does not accumulate (each resume reseeds `errors` / `failed_nodes`
and wipes the checkpoint — `cli/main.py::_seed_state`), so the final
`workflow_end` is a self-contained per-node resolution of that attempt.

**Known coverage boundary.** A `kind` only rides failures that reach
`node_failed`. The main escape is a broken validator / unevaluable route
predicate: it raises and aborts the run as a clean `ClickException`, with no
`node_failed` event, and a genuinely unexpected exception can likewise abort
with a raw traceback and a `0/0` `workflow_end`. (Worktree/setup aborts are
**not** in this class — they surface as `infra`; see the table above.)
Distinguish a halted run from a clean one by the CLI exit code, not by
`workflow_end` counts.
