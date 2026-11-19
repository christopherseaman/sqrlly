---
name: sqrlly
description: Author workflow YAML for the sqrlly 1.0 schema spike and check it with the Python loader.
---

# Author a sqrlly 1.0 workflow

Start with a `nodes` list. Give each node a unique `id`.
Use `depends_on` to order work and reference an ancestor's text by its ID.

```yaml
nodes:
  - id: read_title
    run: [cat, title.txt]
  - id: summarize
    depends_on: [read_title]
    llm: {text: "Describe the book titled {{read_title}} in one sentence."}
```

| Kind | Value |
|---|---|
| `llm` | A prompt filename, or a mapping with `prompt` or inline `text`. Optional `provider` and `model` select the CLI and model. |
| `agent` | The same prompt forms, with tools for working on files. `tools` accepts `read`, `edit` or `full`. Named tool lists require `provider: anthropic`. Extra CLI arguments go in `cli_args`. |
| `run` | A command such as `uv run pytest`, an argv list, or a mapping with `cmd` and `env`. Use a list for templates or shell metacharacters. Each element supplies one argument. |
| `workflow` | A child workflow filename, or `{file: flows/report.yaml, with: {topic: "{{read_title}}"}}`. Supply the child workflow alongside the parent. |

Choose one kind per node. Omit the kind for a join or a standalone router.
Prompt files live under `prompts/` beside the YAML. Use `settings.prompts` to change that directory.
Other file references resolve relative to the workflow file.
The default provider is OpenAI. Omitting `model` uses the CLI default unless `settings.model` pins one.

## Pass structured data and repeat work

Declare `outputs: {schema: schemas/plan.json}` on a producer to expose fields such as `{{plan.title}}`.
Supply the referenced JSON Schema file. A `run` producer prints a single JSON document.
For a decision, have a router depend on the producer and test a field such as
`when: plan.priority == 'urgent'`. A routing destination names a node in the same workflow.

Put `for_each: plan.tasks` and `depends_on: [plan]` on the node that repeats.
Use `for_each: plan` for an array producer, or `for_each: {file: items.json}` for a local manifest.
Without a declared schema, the producer's text must encode a JSON array or an object with an `items` array.
Dependents receive the list of child results in manifest order. Put any route on a separate node.

Use `outputs.files` to require file globs in the node's working tree.
`agent` and `run` accept files, schema, `worktree` and `promote`.
`llm` accepts schema only among those four fields. `workflow` accepts files only.

## Evaluate and route

An `eval` runs a command, LLM or agent and records a measurement.
A command supplies `eval.exit_code` and `eval.text`. A model supplies text and schema fields,
or `score` and `explanation` with the default eval schema.
Give a model eval an explicit `route` with the criterion to act on.

```yaml
nodes:
  - id: edit
    agent: fix.md
    eval:
      run: [uv, run, pytest, -q]
    route:
      - when: eval.exit_code != 0
        then: retry
        max_attempts: 3
      - when: eval.exit_code != 0
        then: review_failure
      - else: publish_result
  - {id: review_failure, agent: diagnose.md}
  - {id: publish_result, run: [cat, result.txt]}
```

Cases run in order. The first node run counts as attempt 1.
At attempt 3 the first case above stops matching, so another failure reaches `review_failure`.
If every remaining case misses after a case hit its cap, the node fails.
An uncapped miss completes normally. A command eval without a route retries a nonzero exit up to three total runs.

`then` accepts a node ID, a list of IDs, or the reserved word `retry` or `fail`.
A case without `when` is unconditional. An `else` case must come last.
Use `context: all` to pass the full evaluation history to the destination.
The default `last` passes the latest measurement and attempt number.

## Check the draft grammar

The 0.10 CLI uses the published grammar. In a checkout containing the spike,
load a draft workflow and inspect its node IDs with this command.

```bash
uv run python -c 'from sqrlly.schema.v1 import load_workflow; import sys; print([n.id for n in load_workflow(sys.argv[1]).nodes])' path/to/workflow.yaml
```
