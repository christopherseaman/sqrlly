# sqrlly

**A high-level interface to and extension of [LangGraph](https://github.com/langchain-ai/langgraph) for agents in local environments.** Declare a workflow in YAML; sqrlly compiles it to a runnable `StateGraph` and adds the runtime extensions agent workflows need:

- **Nodes** execute one of: an LLM prompt (agent-with-tools or text-only), an interpreted script (`.py` / `.js` / `.ts` / `.sh`), a binary, or a recursive subgraph.
- **Local context** — when run inside a git repo, every node gets its own isolated worktree; agents read dep outputs, edit files on disk, and run tools.
- **Quality gates** — a script or LLM scorer judges output; failing gates re-run the node with feedback injected, up to a configurable retry budget.
- **Branching** — `route:` sends flow conditionally; `fan_out:` spawns parallel branches over a JSON manifest.
- **Fault-recovery resume** — state checkpoints to SQLite; `--resume` re-runs seeded with the prior run's state and clears failures to retry.

## Install

For CLI use (recommended — isolated venv, doesn't touch your project's environment):

```bash
pipx install sqrlly             # or: uv tool install sqrlly
uvx sqrlly --help               # run once, no install
```

As a project dependency:

```bash
uv add sqrlly                   # or: pip install sqrlly
```

LLM dispatch uses a local authenticated agent CLI. Codex is the default:

- `provider: openai` — `codex exec` subprocess-per-call
  (no warm process, real `asyncio` parallelism). Requires `codex` on PATH.
- `provider: anthropic` — the opt-in `claude -p`
  subprocess backend. Requires `claude` on PATH.

**Auth is per-CLI, not sqrlly's job.** Each CLI you point sqrlly at
handles its own credentials independently. Authenticate Codex or Claude
with that CLI's normal login flow; sqrlly never reads or stores API keys.

Python 3.11+. From source: `git clone` then `uv sync`.

**Using sqrlly from a coding agent?** `sqrlly init --skill` installs the
agent skill into your repo at `.agents/skills/sqrlly/SKILL.md`
(repo-aware) so the agent auto-discovers how to author and run
workflows.

## Quickstart

Bootstrap a runnable workflow in two commands — no git clone needed:

```bash
sqrlly init my-workflow
cd my-workflow && sqrlly run workflow.yaml
```

`sqrlly init` writes a workflow with one `run` node that prints a greeting.

The jokes example generates candidates, scores them and selects a winner.

```yaml
name: "Joke Generator"
settings: {prompts: .}
nodes:
  - id: generate
    name: "Generate Jokes"
    llm: generate.md
    evaluation:
      validator: "gates/validate_jokes.py"
      threshold: 1.0
      blocking: true
      max_retries: 2

  - id: select
    name: "Select Best Joke"
    depends_on: ["generate"]
    llm: select.md
```

```bash
sqrlly validate examples/jokes/workflow.yaml
sqrlly run examples/jokes/workflow.yaml --log run.jsonl
```

- `validate` — compiles the graph and reports the node count.
- `run` — executes the workflow; with `--log`, writes a JSONL event stream (`workflow_start`, `node_model`, `node_completed`, `node_failed`, `gate_evaluated`, `node_retried`, `workflow_end`).

## How it works

- A node's kind selects its operation. Use `agent` for tools, `llm` for text, `run` for argv and `workflow` for a child graph.
- **Jinja2 templates** — prompt files are full Jinja2; `{{generate}}` interpolates an upstream node's output; `{% if %}` / `{% for %}` / filters all work.
- **Retry feedback loop** — when a gate scores below `threshold`, the next attempt's context gets `{{_retry_reason}}` auto-populated with the previous score, per-dimension thresholds, and feedback.
- **Worktree isolation** — inside a git repo, each node runs in its own `git worktree` under `<workdir>/.sqrlly/`, reused across retries so prompt nodes can iterate on prior files.
- **Checkpointed state** — runs persist to `<workdir>/.sqrlly-checkpoint.db` (LangGraph `AsyncSqliteSaver`); `--resume` re-runs seeded with that state, clearing failed nodes to retry. Completed nodes skip by default (fault-recovery re-run, not full replay) unless `--rerun-all` is set. For fan-out branches: failed children re-run, but completed siblings stay frozen (no re-billing).
- **Recursive subgraphs** — a `.yaml` URL runs another sqrlly workflow; the same file works standalone or as a subgraph reference.
- Child workflow paths resolve beside the declaring YAML. Prompt filenames resolve under its `prompts/` directory.

## Models and defaults

Codex runs `llm` in a read-only sandbox with its shell and configured MCP servers disabled.
Claude disables tools for `llm`.
The provider defaults to Codex. Set `provider: anthropic` to use Claude.
Without a model selection each local CLI uses its own default.
Use `settings.model` to pin a shared model or `settings.defaults.<kind>` to configure a kind.
A node's explicit options take precedence.

```yaml
settings:
  defaults:
    agent: {tools: read}
nodes:
  - {id: review, agent: review.md}
  - {id: print_review, depends_on: [review], run: [echo, "{{review}}"]}
```

## CLI

| Command | Purpose |
|---|---|
| `sqrlly init [<dir>]` | Scaffold a minimal runnable workflow into `<dir>` (default `.`). |
| `sqrlly init --example <name>` | Scaffold a bundled example into `./<name>` (`--list-examples` to see them). |
| `sqrlly validate <config>` | Compile the workflow; report node count and lint warnings. |
| `sqrlly run <config>` | Execute the workflow. |
| `sqrlly graph <config>` | Print the topology as a Mermaid diagram. |
| `sqrlly view <config>` | Render a self-contained interactive HTML viewer. |

`run` flags:

- `--workdir / -w <dir>` — working directory (default `.`).
- `--dry-run` — trace topology without executing.
- `--resume` — re-run seeded with the prior run's state, clearing failures to retry. Completed nodes skip by default; use `--rerun-all` to re-execute every node.
- `--entry <node>` — cold-start at `<node>`: run it and everything downstream WITHOUT a checkpoint (the upstream artifacts must already be on disk). Mutually exclusive with `--resume` / `--resume-from` / `--rerun-all`.
- `--log <path>` — write a JSONL event log.
- `--quiet / -q` — suppress the live terminal renderer (use in CI / piped runs).
- `--safe-mode / --no-safe-mode` — run Anthropic's CLI provider with Claude operator customizations (output styles, CLAUDE.md, skills, MCP, hooks) disabled. It has no effect on Codex. Overrides `settings.safe_mode`; absent, the setting applies.

`run` shows a live per-node status grid + a clock-driven aliveness spinner when stdout is a TTY. Non-TTY runs (piped, redirected, CI) fall through to the plain summary output automatically. Workflow events only — no LLM-token streaming. On completion `run` prints a `where to find things` summary — the produced output files, the log path (or how to capture one with `--log`), and run artifacts (checkpoint DB + worktree pool); `--quiet` suppresses it.

`init --example <name>` scaffolds a runnable bundled example (`jokes`, `route_classify`, `explicit_join`, `pipeline_style`, `absurd-paper`) — handy after `uv tool install sqrlly` when you have no repo checkout. `init --list-examples` lists them.

## Examples

Render any example's interactive view with `sqrlly view <workflow.yaml>`; `examples/regenerate_views.sh` rebuilds views (and, for the deterministic script-only examples, debug views from a captured log) locally.

| Workflow | What it shows |
|---|---|
| [`examples/jokes/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/jokes/workflow.yaml) | Minimal: prompt + script gate + select. Start here. |
| [`examples/route_classify/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/route_classify/workflow.yaml) | Inline `route:` case ladder over structured state. |
| [`examples/pipeline_style/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/pipeline_style/workflow.yaml) | `route: { goto: <next> }` forward-edge authoring; a linear pipeline. |
| [`examples/absurd-paper/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/absurd-paper/workflow.yaml) | Full pipeline: prompts, multi-dim quality gates, a subgraph, and a rendered PDF. Try `sqrlly init --example absurd-paper`. |
| [`examples/wave_planner/`](https://github.com/christopherseaman/sqrlly/blob/main/examples/wave_planner/workflow.yaml) | Wave-driven dynamic-task pattern — `goto:` loop-back to a fan-out parent. |

## Documentation

- [SCHEMA.md](SCHEMA.md) documents the four kinds, settings, route predicates and quality gates.
- **[SKILLS.md](https://github.com/christopherseaman/sqrlly/blob/main/SKILLS.md)** — agent skill doc: instructions for an AI coding agent authoring and running sqrlly workflows.

## Contributing

- **Three-layer split** — `schema/` (Pydantic models) → `compile/` (YAML → LangGraph) → `runtime/` (executors, backends). Layer rules enforced at CI time by an AST import check.
- **No mocks of external systems** — tests run real subprocesses, real `git worktree`, and real backends.
- **Layer rules** — `tests/architecture/test_layers.py` enforces the import boundaries; `CLAUDE.md` has the architecture sketch + contributor reading order.

```bash
uv run pytest tests/ --ignore=tests/cli     # ~1k tests
uv run pytest tests/cli                     # CLI backend integration (needs `codex` and `claude` on PATH)
```

## License

Apache-2.0 — see [LICENSE](https://github.com/christopherseaman/sqrlly/blob/main/LICENSE).
