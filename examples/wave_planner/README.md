# Wave-driven research planner

Demonstrates the **wave-driven dynamic-task pattern** — discover new
tasks mid-run without restarting upstream work. Each "wave" is one
trip around the dispatcher → workers → reconcile → gate loop. A
wave that finds new pending tasks routes back to the dispatcher;
a wave that finds none completes the workflow.

## Topology

`planner` seeds questions in `state.json`. `dispatcher` reads the file and emits pending questions as JSON. The `workers` node uses `for_each: dispatcher` and passes `{{item.id}}` to each worker. A worker locks the shared state file while marking its question done.

After all workers finish, `reconcile` checks for follow-up questions. The route returns to `dispatcher` when `reconcile` discovers a new question, up to ten waves. On the second wave, workers see only the newly queued question.

## Running

The workers use `worktree: off` to update one shared file in Git and plain directories. Use a fresh workdir for the example.

```bash
mkdir -p /tmp/wave-demo
uv run sqrlly validate examples/wave_planner/workflow.yaml
uv run sqrlly run examples/wave_planner/workflow.yaml --workdir /tmp/wave-demo
```

You'll see two waves run:

```
Note: workdir is not a git repo — running without worktree isolation (foreman disabled).
Completed: 8 nodes
  Nodes: dispatcher, gate, planner, reconcile, workers, workers::q_competitor_share,
         workers::q_growth_rate, workers::q_market_size
```

The runner lists each completed node ID once. The second wave adds
`workers::q_competitor_share` after reconcile queues that question.

`/tmp/wave-demo/state.json` has the final state with all three
questions marked `done`.

## Real workflows

The deterministic Python scripts in `scripts/` exist so the
example runs without any backend keys and the e2e test stays
stable. Real workflows replace them with LLM-driven nodes:

- A "research" worker that invokes Claude/DeepSeek to answer
  the question, emitting both the answer and any follow-up
  questions in its output.
- A reconcile node that runs an LLM gate over partial results
  and decides whether to enqueue follow-ups.

When combining the wave pattern with worktree isolation in a
real workflow, thread state through sqrlly's `node_outputs`
mechanism (each node emits state JSON on stdout, downstream
nodes template-read `{{upstream_id}}`) instead of a shared
file. sqrlly's state is automatically visible across
worktrees because it's tracked in the LangGraph WorkflowState,
not on disk.
