# November 22, 18:00 UTC checkpoint on transport and recovery

Issue #55, branch baseline `ff2e581`. The previous checkpoint recorded two SQLite rows after an unkeyed replay, one row with a destination-enforced key, and an independent lookup of an existing GitHub Actions run. It also showed that bare resume accepted an edited output file. This checkpoint separates the surviving job handle, the checkpoint, and the output bytes by replacing the work directory. The four application scenarios supplied requirements only. [Previous checkpoint](2026-11-22-baseline.md), [research program](../engine-research-program.md)

## What the current CLI owns

- `DispatchExecutor` renders a command's argv and environment and runs it locally in the selected working directory. It inherits the process environment and overlays authored `run.env` values. In Git, `ForemanExecutor` can isolate work and settle file changes through Git snapshots. The isolated trials ran outside Git. Neither a local subprocess exit nor a Git revision establishes the outcome of an external write. [Dispatch](../../../src/sqrlly/runtime/executor/dispatch.py), [Foreman](../../../src/sqrlly/runtime/foreman.py)
- The backend factory translates prompt policies into arguments for local Codex and Claude CLI processes. These subprocesses have a local cwd and process group. Timeout or cancellation of a process group gives the runner evidence about its local process, while an independently accepted operation can outlive it. The internal `PromptBackend` and `NodeExecutor` protocols return `ExecutionResult`; neither defines remote submission, durable handle registration, or reattachment. [Factory](../../../src/sqrlly/runtime/executor/backends/factory.py), [Provider process](../../../src/sqrlly/runtime/executor/backends/cli.py), [Result protocols](../../../src/sqrlly/runtime/result.py)
- `outputs.schema` checks the shape of structured stdout. `outputs.files` checks for matching regular files in the execution worktree. A failed output check can execute the body up to three times even with zero command backend retries. A downstream command can consume a structured ID, and `workflow.with` can pass a value into a child workflow. A schema-validated ID remains an assertion until a lookup verifies it in the right scope. [Outputs](../../../src/sqrlly/compile/outputs.py), [File checks](../../../src/sqrlly/runtime/gates.py), [Bindings](../../../src/sqrlly/compile/nodes.py)
- The CLI stores `.sqrlly-checkpoint.db` in the workdir. Its latest-run lookup uses a workflow key derived from the workflow path, name and resolved workdir. Resume reads the selected thread, reseeds saved output and variables, and skips eligible completed nodes. It deletes the old thread before running the reseeded graph. A crash during that transition was outside this experiment. `--resume-from` reruns the named node and downstream work. `--entry` starts cold without upstream bindings. [CLI](../../../src/sqrlly/cli/main.py)

## Commands and observations

The tests run the public `sqrlly run CONFIG --workdir WORKER` command in fresh processes and temporary workdirs outside Git. The HTTP fixture listens on loopback and uses SQLite. Separate processes submit, finish and query jobs in a second SQLite registry. The job launcher exits deliberately before emitting structured output. State outside the worker directory survives directory replacement. The temporary stores do not model host or power loss.

```bash
export TMPDIR="$(mktemp -d /dev/shm/sqrlly-transport.XXXXXX)"
uv run --offline pytest -q -s -p no:cacheprovider tests/e2e/test_transport_recovery.py
uv run --offline pytest tests --ignore=tests/cli -m 'not live' -q -p no:cacheprovider
```

The focused public CLI run passed all four tests on Python 3.14.4 with the normal runtime. The full offline suite passed 872 tests with one deselected in 99.78 seconds. The scheduled host's second regression run recorded the same count using the full command above with this corrected test staged. Its first run recorded 871 passed, one failed and one deselected. The fixture sent HTTP 200 before calling `json.dumps` without a local import, which produced an empty response body. The corrected fixture serializes the JSON before sending 200 and checks for HTTP 409 on duplicate matches. Earlier editor diagnostics used an asyncio timer to get past a stalled SQLite connection and could not open a loopback socket. The host runs required neither workaround. [E2E scenarios](../../../tests/e2e/test_transport_recovery.py)

| Trial | Observed result |
| --- | --- |
| HTTP POST commits before closing its connection without a reply | The original row survived. Resume in the empty replacement directory returned `No saved state`. A fresh GET by the adapter-owned match key returned ID 1. A downstream command imported ID 1 using schema-validated stdout. |
| Replay of a failed POST in a retained workdir | `--resume` committed a second row for the same match key. The later GET returned HTTP 409. The import table still contained only ID 1 from the unique lookup. |
| Job accepted, launcher exits 1 without saving the handle | The job owner advanced the one job to complete. Resume from the empty replacement workdir returned `No saved state`. A fresh collector lacked the handle and produced no file. The test harness read the registry as an independent witness. The collector's interface accepts an opaque handle. |
| Same trial with a separately saved handle | A fresh collector queried the existing job and printed schema-valid JSON. The next node wrote the matching handle to `collected`. The registry still contained exactly one completed job. |
| Restore only the checkpoint at the original absolute path | Bare resume exited 0, skipped both completed nodes and left `collected` absent. `--resume-from collect` queried again and rebuilt the file. Copying the same database to another workdir produced `No saved state`. |
| SQLite commit followed by invalid structured stdout | The workflow failed output validation after three committed rows with IDs 1, 2 and 3. Resume in the replacement workdir returned `No saved state`. All three rows survived. |

### Previous branch checkpoint

The local public CLI created the checkpoint with source from `0f57d46` and loaded it with `ff2e581`. Both invocations exited 0. The current branch skipped the one completed node, leaving the missing file absent. This covers one non-Git workflow across those revisions using the same installed dependencies and original absolute workdir.

```bash
set -euo pipefail
d=$(mktemp -d "$TMPDIR/transport-old.XXXXXX")
mkdir "$d/old" "$d/worker"
git archive 0f57d46 src | tar -xf - -C "$d/old"
cat > "$d/workflow.yaml" <<'YAML'
name: older-checkpoint
nodes:
  - id: write
    run: [sh, -c, 'printf ready > artifact.txt']
    outputs:
      files: [artifact.txt]
YAML
PYTHONPATH="$d/old/src" timeout 30s .venv/bin/sqrlly run "$d/workflow.yaml" --workdir "$d/worker"
test "$(cat "$d/worker/artifact.txt")" = ready
mv "$d/worker" "$d/retired"
mkdir "$d/worker"
cp "$d/retired/.sqrlly-checkpoint.db" "$d/worker/"
timeout 30s .venv/bin/sqrlly run "$d/workflow.yaml" --workdir "$d/worker" --resume
test ! -e "$d/worker/artifact.txt"
```

## Two host boundaries

| Responsibility | Persistent workspace as recovery authority | Replaceable worker with independent recovery state |
| --- | --- | --- |
| Credentials | The operator supplies credentials to the long-lived host, which makes them available to local adapters through the execution environment. | Every worker obtains scoped credentials from its execution host. The same saved reference can become unverifiable if access is missing or revoked. |
| External IDs | Successful command output can be checkpointed in the workspace. A handle received before settlement needs adapter-owned persistence on that durable mount. | External IDs and opaque job handles live with the adapter or service in an independently persistent store. Save service and account scope alongside the ID. Pass only references and nonsecret metadata through workflow state. |
| Artifacts | Keep SQLite, workdir and required files together. In Git, keep the corresponding repository and required snapshot refs. Path identity remains part of recovery. | Publish required bytes to independently durable storage. Record an artifact reference and digest and fetch into a new scratch workdir. The host can keep a persistent workspace for selected nodes while cloud workers come and go. |
| Earlier checkpoints | Resume may reuse earlier completed nodes if the saved state and addressing remain compatible. Restore output bytes separately. | Start a fresh recovery workflow from externally durable references. An old workdir-bound checkpoint cannot serve as a portable run manifest. A future portable run identity would require an explicit import or migration contract and validation before reuse. |

Keep the service rules in the command adapter. GitHub repository scope with a resource ID can support lookup. Jira needs site scope and a way to recover the issue ID or key. Clarify's match rules vary by object type, so a generic retry rule cannot assume an upsert is available. A database can enforce uniqueness in the same transaction as its effect. Existing `run`, schema-checked stdout, downstream bindings and `workflow.with` can compose submission, lookup and collection without a new executor API. A reusable host API would earn its cost when it can bind a stable run to an independent checkpoint and artifact store, or when it can persist accepted work and expose a waiting state across replacement. A method that wraps submit and returns an ID still has the acceptance-to-persistence gap. The prior program records the vendor contracts used in this analysis. [Vendor contract notes](../engine-research-program.md#the-boundary-between-a-tool-and-an-execution-environment)

## Decisions for #57 and #58

- **#57** Start with adapter-supplied operation keys and destination-enforced deduplication or reconciliation. Count committed effects across backend retry, output-contract replay, resume and deliberate rerun. Test whether an engine-provided identity improves a concrete case with caller keys, particularly fan-out identity and intentionally distinct runs. An invocation ID identifies a log stream and changes on resume. A potential engine improvement is independently disabling output-triggered execution retries for effects that lack a safe retry contract. Preserve current behavior by default and verify that any switch applies on both top-level and repeating execution paths. The current checkpoint documents the observed retry behavior and keeps the documented runtime contract. Separating mutation from read-only validation or supplying an enforced key already works where the adapter has those options.
- **#58** Compare a read-only verification command using an artifact reference and digest with a portable receipt tied to the producer and its inputs. Require distinct observations for accepted, completed, and collected or independently verified. The relocated checkpoint and missing-file recovery show where an artifact receipt can add evidence. A receipt containing only a remote ID still requires an authorized, scoped read. Existing checkpoints can supply historical results, with independent verification where durable evidence exists.
- **Counterexample** An unkeyed POST commits, its reply is lost, and the adapter has no usable way to correlate the write with a resource. Checkpoint retention or a stable runner operation ID alone cannot recover the resource ID. Reissuing the POST can create a duplicate. The loopback control offers a match field and rejects the lookup after replay makes it ambiguous. The SQLite output validation case shows repeated commits within one CLI invocation.

The schema reference documents output-triggered replay and the verification boundary. Vendor writes, real credentials, cloud workers, connector SDKs, Git snapshot portability and abrupt termination during checkpoint reseeding remain unverified.
