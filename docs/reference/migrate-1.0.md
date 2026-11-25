# Migrate a workflow from 0.10 to 1.0

Finish any in-progress runs with 0.10. The 1.0 CLI uses a new checkpoint
format. Keep the original workflow path, working directory, and checkpoint
database together if you need to finish a saved run.

```bash
uvx --from 'sqrlly==0.10.0' sqrlly run old.yaml --workdir old-work --resume
```

Convert a copy of the workflow and validate it with 1.0.

| In 0.10 | In 1.0 |
| --- | --- |
| Prompt URL in `execute` | Use `agent` for a prompt with tools or `llm` for text. Set `settings.prompts` relative to the workflow YAML if prompts live outside `prompts/`. |
| Script or binary URL in `execute` | Use `run` with argv. Put the interpreter first for scripts and pass working directory paths or `{{workflow_dir}}` explicitly. |
| YAML URL in `execute` | Use `workflow` with a path relative to the declaring YAML. Pass child inputs with `workflow.with`. |
| `execute: {type: join}` | Keep the node's `id` and `depends_on`. |
| `settings.presets`, `params.preset`, and `--preset` | Move model defaults to `settings.model` or `settings.defaults.agent` and `settings.defaults.llm`. Use a node's `model` or `provider` to override its defaults. |
| `evaluation` | Use `eval.run` or `eval.llm` to measure output. Use `route` to define thresholds, retries, and failure. |
| `route: {goto: next}` | Use `route: [{then: next}]` for an unconditional destination. Use `when` for a condition and `else` for a fallback. |
| `fan_out` | Put `for_each` on the repeating node. Reference a producer or use `{file: items.json}` for a static manifest. |

Remove the top level `version` key if present. Review worktree behavior
before running: agents and repeated commands default to isolated Git
worktrees. Serial commands run in the base directory. Isolated changes
promote when the node settles unless you set `promote: false`.

A command and its unconditional route in 0.10:

```yaml
nodes:
  - id: build
    execute:
      url: /bin/sh
      params: {args: [-c, "printf hello > greeting.txt; cat greeting.txt"]}
    route: {goto: report}
  - id: report
    execute:
      url: /usr/bin/printf
      params: {args: ["%s\\n", "{{build}}"]}
```

The 1.0 equivalent adds a required file and checks it before routing.

```yaml
nodes:
  - id: build
    run: [sh, -c, "printf hello > greeting.txt; cat greeting.txt"]
    outputs: {files: [greeting.txt]}
    eval:
      run: [test, -s, greeting.txt]
    route:
      - {when: "eval.exit_code != 0", then: fail}
      - {else: report}
  - id: report
    run: [printf, "%s\\n", "{{build}}"]
```

Save the migrated YAML as `workflow.yaml`. Validate it and start in a new
working directory while checking the upgraded workflow's output.

```bash
sqrlly validate workflow.yaml
mkdir new-work
sqrlly run workflow.yaml --workdir new-work --log run.jsonl
sqrlly status workflow.yaml --workdir new-work
```

See the [schema reference](../../SCHEMA.md) and
[authoring guide](../../SKILLS.md) for all fields and examples.
