---
name: sqrlly
description: Author, validate, run, and debug sqrlly workflows. Use when the task involves creating or editing a sqrlly workflow YAML file, wiring LLM / script / subgraph nodes, quality gates, routing, or fan-out, or invoking the `sqrlly` CLI. Trigger words — sqrlly, workflow YAML, workflow orchestrator.
---

# Using sqrlly

sqrlly compiles a YAML workflow into a runnable LangGraph and executes
it. A workflow is a list of **nodes**; each node runs an LLM prompt, a
script, a binary, or a nested workflow. Quality **gates** retry failing
nodes with feedback; **routing** and **fan-out** handle branching and
parallelism.

## Prerequisites

Install the CLI: `pipx install sqrlly` (or `uv tool install sqrlly`).

LLM nodes drive a local agent CLI, so the chosen backend must be installed
and authenticated — auth is per-CLI, not sqrlly's job:

- `provider: openai` uses Codex (default).
- `provider: anthropic` uses Claude.

Script, binary, and subgraph nodes need no backend.

No repo checkout? `sqrlly init --example jokes` scaffolds a runnable
example on disk (`--list-examples` for the set: `jokes`,
`route_classify`, `explicit_join`, `pipeline_style`).

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

1. **Add a gate** to retry a node until its output is good enough:
   ```yaml
   evaluation:
     validator: gates/check.py   # .py / .js script, or .md LLM prompt
     threshold: 0.8
     blocking: true
     max_retries: 3
     # dimensions:               # optional multi-criteria gate
     #   - field: accuracy
     #     threshold: 0.8
     #   - field: clarity
     #     threshold: 0.7
   ```
   The validator's **output contract**: print to stdout a bare number
   (`0.85`) or a JSON object (`{"score": 0.6, "feedback": "..."}`); a
   script gate reads the node output on stdin, exits 0. With
   `dimensions`, the JSON carries one numeric field per dimension
   (`{"accuracy": 0.9, "clarity": 0.6}`) and each is checked against its
   own threshold. LLM (`.md`) gates return the same JSON — a surrounding
   ``` code fence or reasoning preamble is tolerated. A validator that
   **can't produce a score** (crashes, exits non-zero, or emits no
   parseable score) halts the run loudly — distinct from a valid score
   below threshold.
2. **Node options** (any node): `timeout: 30` (seconds; the node is
   killed and fails if it overruns) and `output_contract:` to assert a
   node wrote files —
   ```yaml
   output_contract:
     base_directory: "."                  # base for required_files (required)
     required_files: ["report.md"]        # node fails if missing after it runs
   ```
   In a git repo, files are checked in the node's worktree (where it
   wrote them), so write paths relative to the run, not absolute.
   `required_files` entries are paths or globs (`out_*.md`, `**/*.json`);
   each entry needs at least one match, and a literal path matches itself.
3. **Branch** with a `route:` block — an unconditional `goto:`, or a
   `cases:` / `else:` ladder of `when:` predicates:
   ```yaml
   route:
     cases:
       - when: "score('classify') >= 0.8"   # predicate (see below)
         goto: high_quality
       - when: "'urgent' in classify"        # `classify` = its output
         goto: escalate
     else:
       goto: default_path
   ```
   `when:` is a **Python expression** (sandboxed simpleeval) — use
   `True` / `False`, not the string `"true"`. In scope: each upstream
   dependency id (bound to its output), `state` (the full state dict),
   and `history` / `evals` (evaluation records); plus functions
   `passed(id)`, `score(id)`, and `scores(id)`.
4. **Parallelize** with a `fan_out:` block — one branch per manifest
   item, joined by `final_nodes`:
   ```yaml
   fan_out:
     manifest_path: items.json   # JSON: [{"id": "a", ...}, ...] or {"items": [...]}
     template:
       llm: item.md
       # evaluation: ...          # optional: gate each branch
     final_nodes:                 # inline nodes; run once after all branches join
       - id: summarize
         name: Summarize
         llm: summarize.md
   ```
   Each inline final node has an id and a kind. Optional fields include
   `evaluation` and `output_contract`. Each
   manifest item is a JSON **object**; its fields become template
   variables (`{{id}}`, `{{name}}`, …). A bare string item is treated
   as `{"id": "<string>"}`. The manifest may instead be produced at run
   time as this node's JSON output — a ``` code fence or surrounding
   prose around the JSON array/object is tolerated — with `manifest_path`
   as the static fallback (a *declared* manifest_path that's missing or
   invalid JSON halts the run; an empty-but-valid manifest warns and
   skips the fan-out).

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

**Controlling worktree isolation.** `worktree` (on `settings` or a
node) picks the isolation mode: `auto` (default — isolate per node iff
in a git repo), `isolated` (force a worktree), `off` (run in the shared
base workdir — use this when nodes must see each other's files, or so a
script gate can read the node's files directly). For a shared tree
across *some* nodes, give them the same `worktree_group: <name>` — they
write into one worktree (the feature-team pattern) with no merge step.
`worktree` and `worktree_group` are mutually exclusive on one node;
both inherit graph→subgraph and resolve by scope specificity (a node's
setting beats the subgraph's beats the graph's). **Footgun:** a
`worktree_group` on a *fan-out template* collapses all dynamic children
into one shared tree — keep fan-out children on `auto`/`isolated`
unless you intend that.

**Getting results out + cleanup.** An isolated/group node's files live in
its worktree, not the base workdir. Set `promote: true` on a top-level
node to apply its worktree's git delta (adds/edits/**deletes**) back to
the base workdir after a clean run — the change set is discovered from
git, so it works for open-ended edits (bugfixes/refactors) you can't
enumerate in advance; an `output_contract` (glob paths) narrows it.
Promotion copies **uncommitted** changes only: tell a promoting node's
agent not to `git commit`. A worktree holding commits fails the promote
step (error names the node, nothing is written, the worktree is kept).
Set `settings.worktree_gc: on_success` to remove worktrees after a clean
run (default `never` keeps them for inspection / `--resume`). The full
lifecycle is **fork → produce → read/share → promote → GC**.

A worktree-isolated **fan-out** merges its branch deltas back to base with
`fan_out.promote: true` (routed through `on_promote_conflict` /
`promote_exclude`, same as a top-level `promote: true`). Put `promote` on
the `fan_out:` block, not the parent node — `node.promote` on a fan-out
parent only promotes its manifest-only worktree (a `validate` lint warns).

**Sharing base deps into worktrees.** A fresh worktree is a clean
checkout — gitignored base dependencies (`node_modules`, generated
clients) are not in it, so a node that needs them must get them. Two
settings supply them without leaking them into the promote footprint.
`settings.worktree_share: [<path>, …]` makes a read-only whole-dir
**symlink** of each listed base path into every worktree (no install) —
the cheap path, correct for read-only in-branch gates (`tsc --noEmit`,
scoped tests). `settings.worktree_setup: [<cmd>, …]` runs ordered shell
commands in each fresh worktree so the package manager builds its
**own** real deps there (e.g. `["pnpm install --prefer-offline", "pnpm
exec prisma generate"]`) — the rehydrate path, for branches that mutate
deps or need write isolation; it is package-manager-agnostic,
sentinel-gated (runs once per base state, not per node), and **fatal for
that branch** on a non-zero exit (other branches proceed). Keep
`worktree_setup` to pure base hydration — a branch that mutates
`schema.prisma` should re-run `prisma generate` in its gate/build body,
not in setup. If a setup command needs the package store on the same
device as the worktree (pnpm's EXDEV/hardlink fix), pass the store
location through the tool's own flag with an ABSOLUTE path (setup
commands are exec'd without a shell), e.g.
`pnpm install --store-dir=/abs/path/.pnpm-store`.

**Keeping rehydrated deps out of promote.** Both mechanisms write the
shared paths to the repo's shared `info/exclude` so they stay out of
`git status` and therefore out of a `promote` delta. Rehydrate artifacts
that `worktree_setup` creates are NOT inferred — list them explicitly in
`settings.worktree_setup_exclude: [<path>, …]` (e.g. `node_modules`, an
in-tree generated-client dir like `src/generated/prisma`). As a
promote-layer backstop, `settings.promote_exclude: [<pathspec>, …]`
filters those pathspecs out of **every** promoting node's footprint even
if one slips past the `info/exclude` write — cheap defense-in-depth on
the one operation (promote) you most can't afford to get wrong.

To exclude a directory from a `promote` but keep one subpath, pair
`settings.promote_exclude: ["log/"]` with `settings.promote_include:
["log/phases/**"]` — the include is re-added after the exclude (git
pathspecs can't negate in-list).

**Subgraph fan-out isolation.** When a fan-out template is a subgraph
(`.yaml`), each Send branch gets its own worktree (keyed by the branch
id) and the subgraph's inner nodes all run *inside* that one branch tree
— the **branch is the isolation unit**. Inner nodes share the branch
tree (so a later inner node reads an earlier one's files for free; two
inner nodes writing the *same* path race — give them distinct paths). An
inner node's own `worktree`/`worktree_group` is neutralized — it can't
escape the branch tree or join a cross-branch group. A fan-out nested
*inside* a branch shares the outer branch tree (it isn't a second
isolation level).

A fan-out can override isolation per-block with `fan_out.template.worktree`
(`auto`/`isolated`/`off`, default = inherit `settings.worktree`). Use
`worktree: off` on the template when branches must write to the SHARED base
workdir (a join node reads what they wrote); use `isolated` for a parallel
build whose branch deltas you promote with `fan_out.promote`. Always give
each manifest item a unique `id` — an id-less item WARNs and maps onto the
single `<parent>::unknown` child, so two or more collide; a duplicate child
id (or an unreadable/invalid `manifest_path`) **fails the parent node**
before dispatch (run reports failure; fix the manifest and bare `--resume`
re-fans the parent).

`run` flags: `--workdir/-w <dir>`, `--dry-run` (trace topology without
executing),
`--resume` (skip completed nodes, retry failed ones — see Debug),
`--rerun-all` (full replay of every node), `--resume-from <node>`
(re-run node + downstream; implies `--resume`),
`--entry <node>` (cold-start: run `<node>` + downstream, no checkpoint —
see below), `--log <path>`.

Use `sqrlly run --entry <node>` to re-run a synthesis/integration tail against
a hand-prepared workdir with NO prior checkpoint: it freezes everything
upstream and runs `<node>` + its downstream. The entry node must READ the
upstream artifacts from disk — `{{upstream}}` template vars are EMPTY on a cold
start (upstream never ran). Distinct from `--resume-from`, which requires a
checkpoint.

On completion `run` prints a `where to find things` summary — the
produced `output_contract` files, the `--log` path (or how to capture
one), and run artifacts (checkpoint DB + foreman worktree pool, when
present). The live renderer shows only per-node status, so this epilogue
is where a terminal run tells you where output / logs / artifacts
landed. `--quiet` suppresses it.

`sqrlly graph <config>` prints a Mermaid topology diagram of the
**authored** workflow — the same schema reconstruction `sqrlly view`
uses, not the compiled LangGraph (whose gate/route/fan-out routing is
runtime `Command(goto=...)` and would render edge-less). It draws
`depends_on` edges, declared `route:` edges (dotted, labeled with the
`when` predicate), fan-out parents (hexagon) with a dashed `×N per item`
stand-in for their runtime children (entered `1→N`, fanning back in
`N→1` on the synthesis step / declared `final_nodes`), and gated nodes
(styled). Compiled synthetics (`_eval_` / `_fan_` / `_sub_`) never
appear. `sqrlly view <config>` renders that same topology as a
self-contained interactive HTML viewer with a per-node config panel
(and, with `--log`, a status overlay). Only the realized per-manifest
fan-out *children* — created at run time, varying per run and per re-fan
wave — are absent from both.

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
- A node that keeps retrying is failing its gate — inspect the
  `gate_evaluated` events for the `score` (and, for a `dimensions:`
  gate, per-dimension `scores` plus `dimension_thresholds` — the
  configured per-dimension floors, so you can see which dimension
  blocked without reading the YAML).
- `--dry-run` traces topology without calling backends or running
  scripts; use it to confirm the graph shape before a real run.
- `--resume` reseeds from `<workdir>/.sqrlly-checkpoint.db` and **skips
  nodes that completed cleanly** and aren't downstream of a failure —
  completed work is not re-billed. `--rerun-all` forces full replay of
  every node (pre-0.6 behavior). `--resume-from <node>` re-runs a
  specific node and everything downstream (implies `--resume`). When
  `<node>` is a fan-out parent with prior-run completed children, only
  the failed children re-run (completed siblings freeze, no re-bill);
  `--resume-from` on any non-fan-out node re-runs it and all downstream
  dependents as usual. **v1 limitation:** a subgraph re-runs in full
  unless its reference node completed cleanly; inner nodes aren't
  individually skippable.

Set `settings.defaults.agent.backend_max_retries` or `settings.defaults.llm.backend_max_retries` to absorb transient backend blips (a
`claude exited 1`, or an overload once the model-downgrade chain has no lower
model — at once for Codex models) by retrying the same dispatch (default two retries) with `retry_backoff` between attempts — separate from the gate
`max_retries`. A `.md` LLM gate dispatch gets the same retry and downgrade; if
it still fails, the node fails with kind `backend_error` / `overload` (no
score is recorded and the node body is not re-run). On `--resume`, a fan-out whose child failed re-runs ONLY that
child: the parent re-fans and completed siblings stay frozen (no re-bill), so a
large parallel build that loses one branch resumes cheaply.

## Footguns

- **Hyphens in node ids** — `{{my-id}}` parses as subtraction in a
  Jinja template. Always use underscores. `validate` warns about this.
- **`extra="forbid"`** — every model rejects unknown keys (hard
  validation error); typos halt at load instead of silently ignoring the
  key. `settings:` adopted this in v0.7.6, making it consistent with every
  other schema model. Confirm exact field names in `SCHEMA.md`.
- **Inline-route nodes are DAG leaves** — nothing may `depends_on` a
  node that has a `route:` block.
- **Subgraphs share the schema** — a subgraph `.yaml` is an ordinary
  workflow and must validate standalone.
- **Fan-out manifests need STABLE item ids for `--resume`** — a manifest
  generated at runtime must emit deterministic `id`s (not a uuid /
  per-run counter / positional index). On `--resume` a manifest that
  DROPS a prior branch id trips `settings.on_manifest_drift` (default
  `fail` halts before any `Send`; `warn` proceeds) — else completed
  siblings silently vanish and the failed child is orphaned. A stable-id
  or purely additive re-fan never trips it; `validate`/`run` flag any
  runtime-manifest fan-out with an advisory.
- Run trusted workflows only. A command runs with the host user's filesystem permissions.
- **`prisma generate` in `worktree_setup` needs a matching exclude** —
  a `prisma generate` command without its output path in
  `worktree_setup_exclude` lets the generated client leak into the
  promote footprint; `validate` warns about this. Add the output dir
  (e.g. `src/generated/prisma`) and `node_modules` to
  `worktree_setup_exclude`.
- **The worktree exclude is the base repo's shared `info/exclude`** —
  git has no per-worktree exclude file
  (`git rev-parse --git-path info/exclude` resolves to the common git
  dir), so the `worktree_share` / `worktree_setup_exclude` write mutates
  the one `.git/info/exclude` shared by the base repo and all worktrees.
  Those entries persist after the run and are not reclaimed by
  `worktree_gc`; harmless because `info/exclude` only affects
  **untracked** paths, so it can never mask edits to tracked files.

## Reference

- Install this skill into a repo: `sqrlly init --skill` → writes
  `.agents/skills/sqrlly/SKILL.md` at the repo root (repo-aware).
- Full schema: `SCHEMA.md`
- Worked examples: `examples/` — start with `examples/jokes/`.
