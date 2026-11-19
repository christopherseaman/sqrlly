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

Validation rejects duplicate or reserved node ids, missing or self dependencies and unknown route destinations. A node cannot combine `for_each` and `route`.

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
| `id` | `str` | required | Unique id used by `depends_on`, routes and template references. |
| `name` | `str` | node id | Human-readable label. |
| `description` | `str \| None` | `None` | Free text. |
| `depends_on` | `list[str]` | `[]` | Static dependencies. |
| `eval` | mapping | omitted | Measure the node output before routing. |
| `outputs` | mapping | optional | Validate files and structured results after execution. |
| `for_each` | string or file mapping | omitted | Repeat this node once per item. |
| `route` | case list | omitted | Select the first matching destination. |
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

A child template reads caller inputs through `{{workflow_inputs.source}}` or the short form `{{source}}`.
The qualified form lets `sqrlly validate child.yaml` check the child independently.
Validating the parent also checks literal input keys against its `workflow.with` mapping.
At the top level, `workflow_inputs` is an empty mapping. An ancestor named `workflow_inputs` binds its output under that name.

A node with only an id and dependencies joins its predecessors without launching a process.

## Eval

An eval has exactly one of `run`, `llm` and `agent`. The command or prompt runs in the node's worktree. Pass the output explicitly to a command argument with `{{output}}`. A command exposes `eval.exit_code`, `eval.text` and fields of a single JSON object printed to stdout.

A prompt eval takes the usual prompt options, an optional `model`, `timeout` and `outputs.schema` (a workflow-relative JSON Schema file). Without a schema, a prompt eval returns numeric `score` and string `explanation`. Every measurement appends its attempt number and result to the node's history. The JSONL event `gate_evaluated` carries `node`, `attempt` and a nested `result`. For a command, read `result.exit_code`, `result.text` and any JSON fields. A prompt measurement carries `result.text` and its schema fields.

## Outputs

`outputs.files` lists file paths or globs relative to the execution worktree. Each entry must match a regular file. File and directory symlinks cannot satisfy a declaration. A pattern such as `r[1].txt` matches both the literal filename and `r1.txt`. The same patterns limit promotion to matching regular files, including deletions of tracked files. A matching directory does not include its contents. Without a file filter, promotion also carries symlink changes. Agents, commands and workflows can declare files.

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

## Repeating a node

`for_each` belongs on the node that runs for each item. All four node kinds support it. A repeating node can measure each result with `eval`. Put an explicit `route` on a separate node.

```yaml
nodes:
  - id: plan
    run: [python3, "{{workflow_dir}}/plan.py"]
  - id: build
    depends_on: [plan]
    for_each: plan
    run: [python3, "{{workflow_dir}}/build.py", "{{item.id}}"]
  - id: report
    depends_on: [build]
    run: [python3, "{{workflow_dir}}/report.py", "{{build}}"]
```

`for_each: plan.tasks` selects an array field from a producer with `outputs.schema`. Use `for_each: plan` for a structured array or text containing a JSON array or `{"items": [...]}`. Declare the producer in `depends_on`. A static source uses `for_each: {file: items.json}`. The file resolves relative to the workflow YAML.

Each child receives its value as `{{item}}`. For an object use `{{item.field}}`. The repeating node exposes a JSON list of child results in manifest order, including an empty list for zero items. A downstream node consumes the whole list. Accessing a field directly on that list (`{{build.field}}`) fails validation.

An explicit object `id` identifies the child. An item without an id uses a hash of its value, with a duplicate ordinal to distinguish identical values. On resume, newly inserted items run and completed children keep their existing results. An edited id-less value drops its previous child id. Dropped ids fail under the default `settings.on_manifest_drift: fail`; set `warn` to proceed.

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
