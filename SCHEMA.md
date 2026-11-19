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

Compile-time validation rejects: duplicate node ids; `depends_on`
references to a missing or self node; `route` targets that are neither a
real node id nor `__end__`; a node that `depends_on` an inline-route
node; a node that sets both `fan_out` and `route` (incompatible
dispatchers — route from a node downstream of the fan-out instead).

## Settings

`settings:` holds workflow-global configuration. Every field is optional.

Unknown keys in `settings:` are rejected with a `ValidationError` — no silent ignoring. To diagnose typos or removed fields, run `sqrlly validate` and check the error message.

### Retries, timeout, preamble

| Field | Type | Default | Effect |
|---|---|---|---|
| `max_retries` | `int` | `3` | Default retry budget when an evaluation fails. |
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

Inside a Git repository, agents and repeated commands start in an isolated worktree. Serial commands and LLM nodes run in the base directory. Outside Git, nodes run in the base directory. Agents and commands accept node-level `worktree: isolated|off` and `promote: true|false`. Set per-kind defaults under `settings.defaults.agent` or `settings.defaults.run`.

| Setting | Default | Effect |
|---|---|---|
| `on_promote_conflict` | `fail` | Handle a path changed by another branch or in the base since the snapshot. Use `skip` to keep the existing version, `warn` to apply with a warning, or `overwrite` to apply silently. |
| `promote_exclude` | `[]` | Git pathspecs omitted from promotion. Include generated directories when using setup commands. |
| `worktree_share` | `[]` | Link named base directories into each new worktree for shared dependencies. |
| `worktree_setup` | `[]` | Ordered shell commands to prepare an isolated worktree. |
| `on_manifest_drift` | `fail` | Fail when a resumed manifest drops an earlier child id. Use `warn` to accept removals. |

The engine snapshots the base workdir, including uncommitted files, before an isolated node starts. At settlement its committed and uncommitted changes become visible to dependents. A fan-out promotes its successful children together after all children succeed. The real Git index stays intact. `outputs.files` limits the promotion footprint when declared.

A successful tree is removed after capture. With `promote: false`, the run reports the output ref for inspection. Failed worktrees remain available for recovery. A clean run prunes refs from earlier inactive runs of the same workflow.

## Node

| Field | Type | Default | Effect |
|---|---|---|---|
| `id` | `str` | required | Unique id; referenced by `depends_on` and template `{{id}}`. |
| `name` | `str` | node id | Human-readable label. |
| `description` | `str \| None` | `None` | Free text. |
| `depends_on` | `list[str]` | `[]` | Static DAG edges. Cannot reference an inline-route node. |
| `evaluation` | `Evaluation \| None` | `None` | Quality gate. |
| `output_contract` | `OutputContract \| None` | `None` | Required-files check after execution. |
| `fan_out` | `FanOut \| None` | `None` | Manifest-driven `Send` fan-out. |
| `route` | `Route \| None` | `None` | Inline forward-edge routing block. |
| `timeout` | `float \| None` | `None` | Per-node timeout; falls back to `settings.default_timeout`. |
| `worktree` | `isolated` or `off` | per-kind default | Select the execution directory for an agent or command. |
| `promote` | boolean | true for isolated nodes | Apply the node’s snapshot delta when it settles. |

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

## Evaluation

A quality gate that scores a node's output and drives retries.

| Field | Type | Default | Effect |
|---|---|---|---|
| `validator` | `str` | required | Path to a `.py` / `.js` script or `.md` LLM-prompt gate. |
| `threshold` | `float` | `0.0` | Minimum passing score (`0`–`1`). |
| `blocking` | `bool` | `false` | If `true`, exhausted retries fail the workflow; if `false`, they pass with a warning. A gate with a positive `threshold` but `blocking: false` is advisory only (a below-threshold score won't halt) — `validate`/`run` emit an advisory warning so a hollow "green" run isn't mistaken for a real pass. The `gate_evaluated` JSONL event carries `passed` / `threshold` / `blocking` so a consumer can tell a pass from a warn-continue without recomputing. For a `dimensions:` gate it also carries `scores` (per-dimension values) and `dimension_thresholds` (the configured per-dimension floors), so a consumer can attribute which dimension blocked without reading the YAML. |
| `max_retries` | `int \| None` | `None` | Overrides `settings.max_retries`. |
| `model` | `str \| None` | `None` | Model for `.md` LLM gates. |
| `dimensions` | `list[DimensionCheck] \| None` | `None` | Per-dimension score gates. |

### DimensionCheck

| Field | Type | Default | Effect |
|---|---|---|---|
| `field` | `str` | required | Dimension name. |
| `threshold` | `float` | required | Minimum for this dimension (`0`–`1`). YAML key `min:` is accepted as a back-compat alias. |

### Validator shapes

**Script validators (`.py`, `.js`)** read the node output on stdin and
print to stdout. Environment: `NODE_ID`, `WORKFLOW_NAME`,
`ATTEMPT_NUMBER`, `WORKDIR`. Output is accepted as a bare float
(`0.85`), `{"score": 0.6}`, or a full feedback object
(`{"score", "feedback", "pass_criteria_met", "pass_criteria_unmet"}`).

**LLM validators (`.md`)** are rendered as Jinja2 templates with
`{{output}}`, `{{node_id}}`, `{{attempt}}`, then dispatched through the
node's backend. The response must be JSON with at least a `score`.
Malformed output fails loudly (`score=0.0` + diagnostic) — it never
silently passes.

### Routing

| Outcome | Result |
|---|---|
| `score >= threshold` | pass — dependents continue. |
| `score < threshold`, retries left | retry — re-execute the node. |
| `score < threshold`, retries exhausted, `blocking: true` | fail — dependents skipped. |
| `score < threshold`, retries exhausted, `blocking: false` | pass with warning — dependents continue. |

On retry, the previous score, per-dimension thresholds and reasons, and
gate feedback are formatted into a neutral preamble. Reference it in a
prompt template as `{{_retry_reason}}`.

---

## OutputContract

| Field | Type | Default | Effect |
|---|---|---|---|
| `base_directory` | `str` | required | Directory checked after the node runs. |
| `required_files` | `list[str]` | `[]` | Paths or globs (`*`/`**`/`?`/`[…]`) checked under `base_directory` in the node's run dir (its worktree in a git repo). Each entry needs at least one match for the node to succeed; a literal path matches itself. `*` stays within one directory, `**` spans directories. |

> `required_files` are interpreted relative to `base_directory` for **both** the existence check and the promote glob filter. A node with `base_directory: reference` and `required_files: [x.json]` validates and promotes `reference/x.json`; `required_files: ["*.json"]` validates when at least one `reference/*.json` exists and promotes every changed match.

---

## Route — inline forward-edge dispatch

`Node.route` declares where flow goes next. A node with `route:` and no
kind is a standalone router. An executable node finishes its body and gate before routing.

### Shapes

**Goto shorthand** — unconditional:

```yaml
route:
  goto: write              # str → Command(goto="write")
```

```yaml
route:
  goto: [draft_a, draft_b] # list → static fan-out (concurrent edges)
```

**Conditional ladder** — first-match-wins:

```yaml
route:
  cases:
    - when: "passed('judge') and score('judge') >= 0.8"
      goto: ship
    - when: "len(history['judge']) >= 3"
      goto: __end__
  else: produce
```

| Field | Type | Default | Effect |
|---|---|---|---|
| `goto` | `str \| list[str] \| None` | `None` | Unconditional target(s). Mutually exclusive with `cases`. |
| `cases` | `list[RouteCase]` | `[]` | Predicate ladder. Requires `else`. |
| `else` | `RouteElse \| str \| list` | `None` | Fallback when no case matches. A bare string/list is auto-promoted. |
| `include_eval` | `bool` | `false` | (goto shorthand only) opt the target into the eval preamble. |

**RouteCase**: `when: str` (required), `goto: str | list` (required),
`include_eval: bool` (default `false`).
**RouteElse**: `goto: str | list` (required), `include_eval: bool`.

`__end__` halts the workflow (LangGraph `END`). All other targets must
resolve to a real node id. Inline-route nodes are leaves in the
`depends_on` DAG — nothing may `depends_on` them.

### `include_eval`

By default a goto target does **not** receive the previous node's
evaluation feedback (success paths usually do a different task). Set
`include_eval: true` on a case/else to auto-prepend the neutral eval
preamble to the target's prompt — no template syntax needed.

### Route predicate namespace

Case `when:` expressions evaluate in a sandboxed namespace (no dunders,
no imports, no statements):

| Name | Type | Meaning |
|---|---|---|
| `<dep_id>` | structured-or-raw | Each dep's structured output if present, else raw. |
| `history` | `dict[str, list[dict]]` | Full `state.evaluations`. |
| `state` | `dict` | Full state dict. |
| `evals` | `dict[str, dict]` | `evals[id]` → latest eval result, or `{}`. |
| `passed(id)` | `bool` | In `completed_nodes`, not in `failed_nodes`. |
| `score(id)` | `float` | Latest top-level score (0.0 if absent). |
| `scores(id)` | `dict[str, float]` | Latest per-dimension scores. |
| `len`, `any`, `all`, `min`, `max`, `sum` | builtins | Safe functions. |

---

## FanOut

A `fan_out:` block activates manifest-driven `Send` fan-out — its
presence is the activation; there is no enable flag.

| Field | Type | Default | Effect |
|---|---|---|---|
| `manifest_path` | `str \| None` | `None` | On-disk JSON manifest fallback. |
| `template` | `FanOutTemplate \| None` | `None` | What each Send branch runs. |
| `final_nodes` | `list[FanOutFinalNode]` | `[]` | Nodes that run after all branches, consuming aggregated output. |

The manifest is read from the parent node's JSON output, falling back to
`manifest_path`. Each Send branch runs the template's kind (and
`template.evaluation`, with its own inline retry loop). Each branch's
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
| `evaluation` | `Evaluation \| None` | `None` | Optional gate applied to each branch (with its own inline retry loop). |
| `worktree` | `isolated` or `off` | per-kind default | Select the execution directory for an agent or command. |

Final nodes take an id, an optional name and description, one kind, an optional evaluation and an optional output contract.
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
| `gate_failure` | A **blocking** evaluation scored below threshold after `max_retries` — a real quality verdict on work that ran. | **deterministic** |
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
