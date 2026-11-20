---
name: sqrlly
description: Author, validate, run, and debug sqrlly workflows. Use when the task involves creating or editing a sqrlly workflow YAML file, wiring LLM / script / subgraph nodes, quality gates, routing, or fan-out, or invoking the `sqrlly` CLI. Trigger words — sqrlly, workflow YAML, workflow orchestrator.
---

# Using sqrlly

sqrlly compiles a YAML workflow into a runnable LangGraph and executes
it. A workflow is a list of **nodes**; each node runs an LLM prompt, a
script, a binary, or a nested workflow. An `eval` measures results.
Ordered route cases handle branching, retries, and escalation.
`for_each` repeats a node across items.

## Prerequisites

This guide requires the 1.0 grammar. Before the 1.0 release, run
`uv run --project /path/to/sqrlly sqrlly` in place of `sqrlly` in the
commands below. Use the [0.10.0 guide](https://github.com/christopherseaman/sqrlly/blob/v0.10.0/SKILLS.md)
for the PyPI 0.10.0 release.

LLM nodes drive a local agent CLI, so the chosen backend must be installed
and authenticated — auth is per-CLI, not sqrlly's job:

- `provider: openai` uses Codex (default).
- `provider: anthropic` uses Claude.

Command nodes need no prompt backend. A child workflow needs a backend
if it runs a prompt.

`sqrlly init --example jokes` scaffolds a runnable example on disk.
`sqrlly init --list-examples` prints the catalog.

## Author a workflow

`sqrlly init [dir]` writes a workflow with a single `run` node that prints a greeting.
Choose a unique id for each node. Add `depends_on` to consume an upstream result.

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

Use `settings.model` for a shared model and `settings.defaults.<kind>` for per-kind options.
The node can override its kind's defaults. Without a model selection the CLI chooses.
For a Claude coordinator that spawns workers inside one node, grant its Task tool with
`agent: {prompt: coordinator.md, provider: anthropic, tools: [Task]}`.
For per-worker worktrees, gates and logs, use fan-out.

1. Measure output with `eval`. A command runs in the node's worktree. Pass its input through argv.
   ```yaml
   eval:
     run: [python3, "{{workflow_dir}}/check.py", "{{output}}"]
   ```
   With no route, a nonzero exit retries the node for up to three total attempts. A prompt eval requires an explicit route. Its default result has `score` and `explanation`; use `outputs.schema` for other fields.
2. Declare the results each node must produce. Every glob must match a file in its worktree. A command with a schema prints one JSON document. LLM and agent nodes pass their schema to the provider CLI.

   ```yaml
   outputs:
     files: ["reports/*.md"]
     schema: schemas/plan.json
   ```

   Downstream nodes can render the text with `{{plan}}` or read a schema field with `{{plan.title}}`. A missing file or invalid result gives the node feedback and another attempt, up to three runs.
3. Branch with ordered route cases. Every predicate can read the current measurement, attempt, history and dependency outputs.
   ```yaml
   route:
     - when: eval.exit_code != 0
       then: retry
       max_attempts: 3
     - when: eval.exit_code != 0
       then: escalate
       context: all
     - else: review
   ```
   Attempt 3 falls through to escalation. Without an escalation case, an exhausted retry fails the node. When no predicate matches and no matching case exhausted its limit, dependents run normally. The default context carries the latest measurement and attempt. Use `none` to suppress it.
4. Repeat a node with `for_each` and consume its ordered output from a dependent.
   ```yaml
   nodes:
     - {id: plan, run: [python3, "{{workflow_dir}}/plan.py"]}
     - id: build
       depends_on: [plan]
       for_each: plan
       run: [python3, "{{workflow_dir}}/build.py", "{{item.id}}"]
     - id: report
       depends_on: [build]
       run: [python3, "{{workflow_dir}}/report.py", "{{build}}"]
   ```
   Here `plan` emits a JSON array or an object with an `items` array. Select a schema field with `for_each: plan.tasks`. Read a static array from beside the YAML with `for_each: {file: items.json}`. A scalar item is available as `{{item}}`; object fields use `{{item.field}}`. The report receives child results in the original order. Put any explicit route on a separate node.

Do not guess field names. Every model is `extra="forbid"` — a typo'd
key is a hard validation error. The exhaustive field reference is
`SCHEMA.md` (online:
https://github.com/christopherseaman/sqrlly/blob/main/SCHEMA.md).

## Validate and run

Always validate before running:

```bash
sqrlly validate path/to/workflow.yaml
sqrlly run path/to/workflow.yaml --log run.jsonl
```

`validate` checks graph wiring and local prompt and child workflow paths.
Run commands start in their node's working directory.

**Worktree isolation.** In Git, an agent and each repeated command get separate worktrees. A serial command runs in the base directory. Dependents see promoted changes as soon as the producing node or fan-out settles. A node may commit its work. Use `worktree: off` on an agent or command that must share the base directory, and order shared writers with `depends_on`.

Set `promote: false` to keep a successful node’s result only in its output ref. The run prints that ref. Failed worktrees stay on disk. A changed base path fails promotion by default. File patterns in `outputs.files` narrow the changes a node promotes.

Use `settings.worktree_share` for shared dependency directories and `settings.worktree_setup` for installation commands in each new tree. List generated directories in `settings.promote_exclude`.

`run` flags: `--workdir/-w <dir>`, `--dry-run` (trace topology without
executing),
`--resume` (skip completed nodes, retry failed ones — see Debug),
`--rerun-all` (full replay of every node), `--resume-from <node>`
(re-run node + downstream; implies `--resume`),
`--entry <node>` (cold-start: run `<node>` + downstream, no checkpoint —
see below), `--log <path>`.

Use `sqrlly run path/to/workflow.yaml --entry <node>` to run a workflow tail against
a hand-prepared workdir with NO prior checkpoint: it freezes everything
upstream and runs `<node>` + its downstream. The entry node must READ the
upstream artifacts from disk — `{{upstream}}` template vars are EMPTY on a cold
start (upstream never ran). Distinct from `--resume-from`, which requires a
checkpoint.

On completion `run` prints a `where to find things` summary — the
produced `outputs` files, the `--log` path (or how to capture
one), and run artifacts (checkpoint DB + foreman worktree pool, when
present). The live renderer shows only per-node status, so this epilogue
is where a terminal run tells you where output / logs / artifacts
landed. `--quiet` suppresses it.

`sqrlly graph <config>` prints the authored dependencies and route destinations as Mermaid. Each repeating node has a ×N stand-in labeled with its item source. `sqrlly view <config>` renders the same topology as interactive HTML. Supply `--log` to overlay execution status.

## Debug a run

- Read the `--log` JSONL stream — events: `workflow_start`,
  `node_model` (LLM nodes: the model that ran the node),
  `node_completed`, `node_failed`, `gate_evaluated`, `node_retried`,
  `workflow_end`. The node id is the `node` field (not `node_id`).
  Subgraph events are prefixed with the parent chain (`parent::child`,
  nesting to `parent::child::grandchild` for deeper subgraphs); child
  ids need only be unique within their own subgraph, not across
  siblings. Note: events carry status/score, not the node's
  full output text — capture that from the node itself if you need it.
- A failed `run` exits non-zero and lists the failed nodes. Each
  `node_failed` event carries a `kind` — `overload` / `backend_error` /
  `timeout` / `infra` (transient — worth a *bounded* retry, e.g. via
  `--resume`; `infra` is a worktree/setup blip),
  `gate_failure` / `node_error` (deterministic — halt until fixed), and
  `upstream_failed` (this node was blocked; the real failure is elsewhere
  in the stream). `workflow_end` also carries a `failed_kinds` map. Branch
  retry-vs-halt on `kind`, not on the free-text `error`. See the "Failure
  kinds" section in `SCHEMA.md`.
- Read the node and attempt in each `gate_evaluated` JSONL event. Compare its nested `result.exit_code`, `result.text` or schema fields with the route predicate.
- `--dry-run` traces topology without calling backends or running
  scripts; use it to confirm the graph shape before a real run.
- `--resume` reseeds from `<workdir>/.sqrlly-checkpoint.db` and **skips
  nodes that completed cleanly** and aren't downstream of a failure —
  completed work is not re-billed. `--rerun-all` forces full replay of
  every node (pre-0.6 behavior). `--resume-from <node>` re-runs a
  specific node and everything downstream (implies `--resume`). When
  `<node>` is a fan-out parent with prior-run completed children, only
  new and failed children run (completed siblings freeze, no re-bill);
  `--resume-from` on any non-fan-out node re-runs it and all downstream
  dependents as usual. **v1 limitation:** a subgraph re-runs in full
  unless its reference node completed cleanly; inner nodes aren't
  individually skippable.

Set `settings.defaults.agent.backend_max_retries` or `settings.defaults.llm.backend_max_retries` to absorb transient backend blips (a
`claude exited 1`, or an overload once the model-downgrade chain has no lower
model — at once for Codex models) by retrying the same dispatch (default two retries) with `retry_backoff` between attempts — separate from the route case’s
`max_attempts`. An `agent` or `llm` eval uses the same backend retry policy.
If that budget runs out, the node fails with `backend_error` or `overload`
before recording a measurement.

## Footguns

- **Hyphens in node ids** — `{{my-id}}` parses as subtraction in a
  Jinja template. Always use underscores. `validate` warns about this.
- **`extra="forbid"`** — every model rejects unknown keys (hard
  validation error); typos halt at load instead of silently ignoring the
  key. `settings:` adopted this in v0.7.6, making it consistent with every
  other schema model. Confirm exact field names in `SCHEMA.md`.
- Route cases may send execution to an ancestor. Bound those loops with `max_attempts`.
- Child workflows use the same schema. Use `{{workflow_inputs.source}}` for
  caller-supplied inputs so the child can validate standalone.
- A stable `item.id` preserves child identity when the object changes.
  Resume rereads a static manifest or uses the producer's saved output.
  Use `--resume-from <producer>` to refresh a producer from changed inputs.
  New items run and completed children keep their results. Editing an item
  without an explicit id drops its old value-based id. The default drift
  policy fails when an id disappears. Set `settings.on_manifest_drift: warn`
  to accept removals.
- Run trusted workflows only. A command runs with the host user's filesystem permissions.

## Reference

- Install this skill into a repo: `sqrlly init --skill` → writes
  `.agents/skills/sqrlly/SKILL.md` at the repo root (repo-aware).
- Full schema: `SCHEMA.md`
- Worked examples: `examples/` — start with `examples/jokes/`.
