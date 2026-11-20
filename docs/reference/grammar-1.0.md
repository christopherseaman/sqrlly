# sqrlly 1.0 grammar

This design records the decisions implemented by issues #36 through #42, and the extension work deferred beyond 1.0. For the current grammar and runnable examples, see [SCHEMA.md](../../SCHEMA.md) and [SKILLS.md](../../SKILLS.md).

The first draft came from an 11-agent pass (a map of the 0.10 authoring surface, four prior-art surveys, three proposals, a judge, a critic that checked it against the code). The owner's later decisions are in the table below.

## Decisions
| Date | Decision |
|---|---|
| 2026-11-14 | LangGraph stays at `>=1.2,<2`. Codex cloud (#28) stays parked until environments are manageable from the CLI. |
| 2026-11-18 | Node kinds are separate keys: `agent`, `llm`, `run`, `workflow`. |
| 2026-11-18 | Extensions register their own keywords, Ansible style. `uses` and `with` are rejected. The framework is built after 1.0. |
| 2026-11-18 | The human pause (`ask`, `wait`) is out of 1.0 and returns as the first bundled extension. |
| 2026-11-19 | `gate` is replaced by `eval` (measures) plus `route` (decides). `eval` has no pass field. |
| 2026-11-19 | A route case is `when`, `then`, `max_attempts`, `context`. `max_attempts` is part of the match, and a case at its limit falls to the next case. |
| 2026-11-19 | `retry` and `fail` are reserved `then` words. `else` is the documented catch-all. A bare `then` is valid. |
| 2026-11-19 | A node fails when no case matches and at least one case was skipped for `max_attempts`. |
| 2026-11-19 | Prompt names resolve under `settings.prompts`. |
| 2026-11-19 | Decision models in 1.0 are `llm` plus schema plus `route`. A table kind waits for extensions. |
| 2026-11-19 | Claude cloud proof of concept uses `claude --cloud` (#34). Managed Agents through `ant` follows (#35). Neither is in 1.0. |
| 2026-11-19 | An isolated node's work promotes to the base by default, and a conflict fails the run (section 4). |
| 2026-11-19 | A workflow that names no model uses the CLI's own default (section 4). |
| 2026-11-19 | Remote prompt URLs are removed: 5 settings, `runtime/url.py`, `runtime/secrets.py` (section 2). |

No decision is open.

## 1. Recommendation

Adopt the author-first grammar on a reworked worktree engine. A node is `id` plus at most one of 4 kind keys: `agent`, `llm`, `run`, `workflow`. Extensions will register further keywords (section 7, decided 2026-11-18, built after 1.0). The human pause is out of 1.0 and returns as an extension. Eight envelope fields attach where the section 3 matrix allows. `nodes` stays a list and `depends_on` keeps its name. Every `agent` keeps its own worktree, and its delta lands in the base when the node settles. LangGraph stays at `>=1.2,<2`: PyPI's latest release is 1.2.22 and no 2.x exists. Codex cloud stays parked. 1.0 breaks the YAML once, and 10 example files migrate by hand.

## 2. Carry forward, change, delete

| Area | Verdict | Detail |
|---|---|---|
| LangGraph substrate | Carry | `>=1.2,<2`. `interrupt()` is unused: resume calls `adelete_thread`, which deletes a pending interrupt. |
| CLI backend | Carry, 3 edits | `--model` becomes optional. A schema flag is added. `llm` disables tools. |
| Presets | Delete | Both preset models and `--preset`. `settings.model` and `settings.defaults.<kind>` replace them. |
| `Execute`/url dispatch | Delete | `execute.url`, `mode`, `type: join`, the interpreter table, `_MODE_TO_PARAMS`. |
| Remote prompt URLs | Delete | `http(s)://` prompt fetch, its 5 settings, `runtime/url.py`, `runtime/secrets.py`. Prompts are files in the repo, which section 9 needs for `host:`. |
| Gates | Replace | `eval` measures and `route` decides (section 5a). `Evaluation`, `threshold`, `blocking` and `max_retries` go. The eval runs in the node's tree; `nodes.py:519` passes the base workdir today. |
| Routes | Change | One shape: a first-match list of `when`, `then`, `max_attempts`, `context` (section 5a). simpleeval stays. Predicates gain `node.field` and `eval.field`. `goto` becomes `then`. |
| Fan-out and manifest | Change | `for_each` on the repeating node. `FanOutTemplate`, `FanOutFinalNode` and `manifest_path` go. |
| Worktrees | Change | 16 fields become 8. `agent` stays isolated in a git repo, as today. |
| Promote | Change | Reads `in..out`, applies at settle, checks the base blob first, conflict default `fail`. |
| Output contracts | Change | `outputs: {files, schema}`. `base_directory` goes. A miss retries. |
| Resume | Carry | Skip set, failed-child rerun, drift guard. 0.10 checkpoints do not resume. `--answer` and `--state-dir` wait for the human-pause extension. |
| `view` and terminal UX | Carry | Authored topology, labelled by kind key. |
| Lint | Change | Matrix errors (section 3), 3 reference errors (section 5), 2 warnings (section 6). |

## 3. The grammar

One `model_validator(mode="before")` on `Node` counts the kind keys present. Two or more raise one message listing the 4 legal keys. Zero keys is the 5th variant, a join or router. Any other key is a load error naming the closest legal key.

```yaml
nodes:
  - {id: build,  agent: build.md}                # CLI agent, tools, own worktree
  - {id: label,  llm: label.md}                  # one prompt, no tools, no worktree
  - {id: test,   run: uv run pytest -q}          # argv, no model, no shell
  - {id: review, workflow: flows/review.yaml}    # subgraph
```

- An `agent`, `llm` or `workflow` scalar is a file, checked at load. The not-found error names the inline form `{text: "..."}`.
- A `run` scalar is shlex-split and runs without a shell. A scalar containing `{{` or a shell metacharacter (`&&`, `|`, `;`, `>`, `$`) is a load error naming the list form: `run: [echo, "{{ plan.title }}"]`. Each list item is one Jinja-rendered argv token.
- Mapping forms: `agent: {prompt, text, model, provider, tools, cli_args}`, `llm: {prompt, text, model, provider}`, `run: {cmd, env}`, `workflow: {file, with}`.
- `agent.tools` is `read`, `edit` or `full`. The levels map to Codex `--sandbox` modes and Claude `--permission-mode` values. A tool-name list needs `provider: anthropic`. MCP servers come from each CLI's own config.
- Prompt names resolve under `settings.prompts`, default `prompts/` beside the workflow file. Other paths resolve from the workflow file.

Envelope by kind. Each `no` is a load error.

| Kind | `eval` | `for_each` | `route` | `outputs.files` | `outputs.schema` | `worktree`, `promote` |
|---|---|---|---|---|---|---|
| `agent`, `run` | yes | yes | yes | yes | yes | yes |
| `llm` | yes | yes | yes | no | yes | no |
| `workflow` | yes | yes | yes | yes | no | no |

`depends_on` and `timeout` are valid on every kind. `for_each` plus `route` on one node stays a load error.

## 4. Defaults an author can trust

| Omitted | Engine picks |
|---|---|
| `name`, `version`, `settings` | `id`, dropped, optional |
| `provider` | `openai` (Codex), as today |
| `model` | the CLI's own default. `settings.model` pins one |
| tools | `edit` for `agent` (Claude `acceptEdits`, Codex `workspace-write`). None for `llm` (Claude `--tools ""`, Codex `read-only`) |
| `worktree` | `isolated` for every `agent` in a git repo and for `for_each` children of `run`. `off` for `llm`, serial `run`, and outside git |
| `promote` | `true` on isolated nodes, applied at settle (0.10: `false`) |
| `on_promote_conflict` | `fail` (0.10: `warn`) |
| `route` on a node with a `run` eval | retry on nonzero exit, 3 attempts, then the node fails (0.10: non-blocking gate, threshold `0.0`) |
| `route` on a node with an `agent` or `llm` eval | required, because no default criterion exists |
| `context` on a route case | `last`: the latest eval output plus the attempt number |
| `settings.prompts` | `prompts/` beside the workflow file |
| `backend_max_retries` | 2 for `agent` and `llm`, 0 for `run` (0.10: 0), rerun in the node's tree |
| `for_each` child id | `item.id`, else a hash of the item value plus an ordinal for byte-identical duplicates |
| `timeout`, `max_parallel_jobs` | none, 4 |

Three layers set a value: kind default, `settings.defaults.<kind>`, node. The node wins.

## 5. Inputs, outputs and the output contract

```yaml
outputs:
  files: [docs/plan.md, "reports/*.json"]   # each glob must match
  schema: schemas/plan.json                 # result is JSON, validated
```

- Text is always present and undeclared: `{{plan}}`.
- `schema` exposes `{{plan.field}}` to templates, `route` and `for_each`. The engine passes it to `claude --json-schema` or `codex exec --output-schema`. The backend populates `structured_output` for both providers. A `run` node must print one JSON document.
- `files` globs are relative to the node's tree and narrow the promote footprint.
- A failed `files` or `schema` check reruns the node with the miss as context, 3 attempts, then fails it. 0.10 fails the node at once.
- A `for_each` node's output is a JSON list of child results in manifest order. `{{build.field}}` is a load error.
- `for_each: plan.tasks` reads a schema field. `for_each: plan` reads the structured value when `plan` has a schema, which must be an array. Without a schema it parses the text as a JSON array or `{"items": [...]}`, as `_read_manifest` does. `for_each: {file: items.json}` reads a static file.
- An edited id-less item hashes to a new id. The old id counts as dropped and trips `on_manifest_drift: fail`.
- Load errors: `{{x}}` where `x` is no ancestor, `{{x.field}}` where `x` has no schema, a `route` predicate comparing the whole text of a schema-less `agent` or `llm`.

## 5a. Eval and route

Decided with the owner on 2026-11-19. `eval` measures. `route` holds every consequence, retries included. No pass or fail idea exists outside the `when` expressions.

```yaml
- id: build
  agent: build.md
  eval:
    run: [uv, run, pytest, -q]
    timeout: 600
  route:
    - when: eval.exit_code != 0
      then: retry
      max_attempts: 3
    - when: eval.exit_code != 0        # same test, reached after 3 attempts
      then: escalate
    - else: review                     # catch-all
```

Eval:

- `eval` holds one kind key (`run`, `llm` or `agent`) and takes that kind's options: `model`, `timeout`, `outputs.schema`.
- A `run` eval exposes `eval.exit_code` and `eval.text`, plus fields when stdout is one JSON object.
- An `llm` or `agent` eval exposes `eval.text`, plus its schema fields. With no schema it gets `{score, explanation}`.
- Every eval run appends a record to the node's history, with the attempt number. `state.evaluations` holds this today.

Route:

- `route` is a list of cases, checked in order. The first case that matches is taken.
- A case has `then` and optionally `when`, `max_attempts` and `context`. A case with no `when` always passes its test.
- `else: <destination>` is the documented catch-all. It equals `then: <destination>` with no `when`, takes no `when`, and must be the last case.
- `then` names a node, a list of nodes, or a reserved word. `retry` reruns this node. `fail` fails it. A node cannot use either word as its id.
- `max_attempts: N` is part of the match. The case matches only while the node has run fewer than N times. The first run counts as 1, so `max_attempts: 3` allows 3 runs.
- A case past its `max_attempts` does not match, and checking continues with the next case. A later case may repeat the same `when` as an escalation step.
- When no case matches and no case was skipped for `max_attempts`, the node completes and its dependents run.
- When no case matches and at least one case was skipped for `max_attempts`, the node fails. A used-up retry never counts as success.
- `then` may name an upstream node. `max_attempts` on that case bounds the loop. A `then` to an upstream node with no `max_attempts` is a `validate` warning.
- `context` sets what the destination receives: `last` (default: the latest eval output plus the attempt number), `all` (every record), `none`, or a template string. `build_eval_preamble` renders the 0.10 equivalent.
- Predicates read `eval` (latest output), `attempt` (this node's run count), `history` (all records) and upstream node outputs.
- `route` without `eval` stays legal for branching on outputs.

Command eval retries apply within each repeated child. Resume restores attempt numbers from the checkpoint.

## 6. Worktree and promote model

Kept (8): node `worktree: isolated|off`, node `promote`, `on_promote_conflict`, `promote_exclude`, `worktree_share`, `worktree_setup`, `on_manifest_drift`, `outputs.files` as filter. Deleted (8): `settings.worktree`, both `worktree_group`, `worktree_gc`, `promote_include`, `worktree_setup_exclude`, `fan_out.promote`, `template.worktree`.

1. Every `agent` in a git repo runs in its own tree. An agent edits the live checkout only with an explicit `worktree: off`.
2. Before an isolated node starts, the engine commits a snapshot of the base workdir to `refs/sqrlly/<workflow>/<run>/<node>/in` through a temporary index. The tree is cut from that commit.
3. At node end the tree is committed to `.../<node>/out`. A node may commit, which removes `PromoteHeadMovedError`.
4. When the node or the whole fan-out settles, the engine compares each path's current base blob with its `in` blob. A mismatch is a conflict under `on_promote_conflict`, like two nodes writing one path. The engine then applies `in..out` to the base, so the next node sees the merged tree. 0.10 checks only node-versus-node overlap (`plan_promotions`) and promotes at end of run.
5. A promoted tree is removed and its ref stays. A succeeded tree with `promote: false` is removed too, and its `out` ref is listed at end of run. A failed tree stays.
6. A run that ends clean deletes the refs of earlier runs of the same workflow.

`validate` gains 2 warnings. The first fires when two `agent` or `run` nodes with `worktree: off` have no dependency path between them. The second fires on `worktree_setup` without `promote_exclude`, because `in..out` would commit generator output that 0.10 kept out through `info/exclude`.

## 7. Extensions

Decided by the owner on 2026-11-18: an extension registers its own keyword. Nothing in this section is built for 1.0. The 1.0 key set is closed at 4, and `NodeExecutor` stays.

The model is Ansible's: the module name is the task key, each module declares an argument spec, and collections namespace the names. `uses:` and `with:` (GitHub Actions) are dropped. Archon's plugins (workflow packs, provider, chat and forge executables) add no node keyword, so they are not the model.

```yaml
extensions: [approval, jira]        # enabled by name

nodes:
  - {id: build,  agent: build.md}
  - {id: ok,     approval: "Ship it?"}
  - {id: ticket, jira: {project: OPS, summary: "{{build}}"}}
```

- An extension registers one keyword and a Pydantic params model (`extra="forbid"`) through a Python entry point.
- A workflow enables extensions in a top-level `extensions:` list. An installed package adds no keyword until a workflow lists it.
- The node's value under the keyword validates against the params model at load.
- An unknown key fails at load and names the closest core or enabled keyword.
- Two enabled extensions claiming one keyword fail at load. A dotted name such as `acme.jira` resolves it.
- An extension declares whether it mutates the workdir, which sets its row in the section 3 matrix. Eval, route, fan-out and resume stay in the engine.

Deferred with the framework:

1. The human pause. It ships as the first bundled extension, under a keyword the owner picks then. No `ask` or `wait` key exists in 1.0. The earlier design stands as a starting point: an unanswered pause fails with kind `waiting`, and `sqrlly run --resume --answer <id>=<value>` reruns it through `prior_failed`. `--state-dir` arrives with it.
2. A deterministic decision-table kind, if one is wanted.

Decision model in 1.0, with zero new kinds: `llm` plus `outputs.schema` plus `route`. It ships as the rewritten `examples/route_classify`.

## 8. Capabilities this enables

Local dev workflows:

- Plan, parallel build, verify, where `verify` tests the merged tree.
- Write, review, revise: `llm` plus `eval: {llm: ...}` plus one `route` case.
- Escalation: retry twice, then send the same failure to a stronger model or a planner.
- Agents that commit, and runs from a dirty checkout.

Serial pipelines:

- Fetch, clean, classify, load with no git and no `settings` block.
- Typed hand-offs: `{{classify.label}}` in argv tokens and route predicates.
- Failed-item-only reruns keyed by item value, so an inserted item re-bills nothing.

## 9. Multi-host as v2: what v1 must reserve

Design: `settings.hosts: {gpu: {ssh, repo}}` and node `host: gpu`. The engine pushes the `in` ref, runs the same CLI argv over `ssh` in a remote worktree, and fetches the `out` ref.

v1 reserves:

- The names `host` and `settings.hosts`. `extra=forbid` rejects them today.
- Promote from refs (section 6).
- Every path a node reads resolves inside the repo.
- Three return channels only: stdout, `outputs.files`, the git delta.
- `host` plus `worktree: off` as a validate error. `host` on an extension keyword becomes one when extensions exist: an extension runs in-process Python, with no argv to send over ssh.
- An eval runs on its node's host.
- Placement in `settings`, one global `max_parallel_jobs`, no per-host cap.

## 10. Cribbed from prior art

| Idea | Source | Lands in |
|---|---|---|
| One type key per node | Archon, Argo, Ansible | section 3 |
| `run` | GitHub Actions | section 3. Actions `run` is a shell; sqrlly `run` is an argv. `uses` and `with` rejected |
| Extension name as the node key, per-extension argument spec, dotted namespaces | Ansible modules and collections | section 7, after 1.0 |
| "YAML coordinates. Code computes. Agents judge." | Archon AGENTS.md | admission test for new fields, `SKILLS.md` |
| Worktree by default for agents | Archon | section 6. Archon merges nothing back; sqrlly promotes at settle |
| Schema-forced output with bounded retry | Claude workflows, Pydantic AI | `outputs.schema` |
| Reject whole-text comparison on schema-less AI nodes | Archon | load error |
| Hash-of-item child keys with duplicate ordinal | Archon | `for_each` |
| Durable human pause | Archon `wait` (core there) | deferred to the first bundled extension |
| If/else on typed output | OpenAI Agent Builder | `route` on `outputs.schema`. Nothing else: shutdown is November 30, 2026 |
| One-key tasks, `use.extensions` | CNCF Serverless Workflow | Rejected as a spec (opinion): same node shape, but a second expression language |
| Tree removed once its delta lands | own design | section 6 |

Skipped: budgets, unknown keys that warn, PR-as-merge, variable-mutation actions, a visual canvas.

## 11. Roadmap

| # | Title | Effort | Done when |
|---|---|---|---|
| 1 (#36) | Spike the 1.0 schema models and a draft SKILLS.md | S | Every matrix `no` raises. An agent given only the draft writes 5 workflows that pass `validate`. |
| 2 (#37) | Delete dead surface and validate params at load | S | `PromptParams.agent` and 4 `_MODE_TO_PARAMS` rows are gone. `validate` fails on a param typo. Ships on 0.10.x. |
| 3 (#38) | Replace execute and presets with kind keys | XL | `Execute` and both preset models are deleted. 10 example files and 53 test files migrate. Worktree defaults stay at 0.10 values. `SKILLS.md`, `SCHEMA.md`, `init` match. |
| 4 (#39) | Replace gates with eval and the route case list | M | A `run` eval reads an isolated node's files. A retry case at its `max_attempts` falls to the next case with the same `when`. A node whose capped case has no later match fails. |
| 5 (#40) | Add outputs.files and outputs.schema with retry | M | A live `llm` node fills `structured_output` on both CLIs. A schema miss retries with feedback. The 3 reference errors raise. |
| 6 (#41) | Snapshot refs, settle-time promote, new worktree defaults | L | A child sees upstream uncommitted files. A node that commits promotes. A base path changed after `in` is a conflict. `--resume` after one failed child promotes each sibling delta once. |
| 7 (#42) | Move fan-out to for_each with value-hash child ids | M | Inserting a manifest item on `--resume` re-runs 0 completed children. |

1.0 releases after issue 7. After 1.0, not scheduled: the extension framework (keyword registration, `extensions:` list), then the human-pause extension with `--answer` and `--state-dir`. `host:` is the first 2.0 issue.

## 12. Unverified claims

All 11 critic findings were applied, and the code-related ones were re-read in the source first. None was rejected. Three carry a variation: `waiting` is a failure kind (finding 1), a `run` scalar containing `{{` is a load error (finding 7), and the old issues 7 and 8 merged to fit the spike (finding 9).

The following were open verification questions at the time of the design review. Issues #38 through #42 record the implementation results.

- The 5 CLI flags appear in the help text of Claude Code 2.1.325 and codex-cli 0.169.0. None was run. The `read` to `plan` mapping is a guess.
- Codex shows no zero-tool mode. `llm` on Codex is probably a multi-turn read-only agent.
- The temporary-index snapshot, the base-blob check, ref pruning and a fan-out that settles across a `--resume` are unprototyped.
- The `workflow` matrix row was not checked against `compile/subgraph.py`, and the fan-out child eval cwd was not traced past `dynamic.py:197`.
- Archon's plugin kinds come from archon.diy/plugins and its publishing guide, read 2026-11-18. The last 190k characters of its workflow-language reference were not read.
- Other Archon and Agent Builder claims rest on the critic's 3 searches and the research notes. The Serverless Workflow row is from memory.

