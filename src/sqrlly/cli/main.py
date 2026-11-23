from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import uuid
from contextlib import AsyncExitStack, closing
from pathlib import Path
from typing import Any

import click

from sqrlly.cli.init import (
    init_command, init_example, init_list_examples, init_skill,
)
from sqrlly.compile.graph import build_workflow_graph
from sqrlly.compile.lint import collect_warnings
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.gates import matching_output_files
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.lease import acquire_sqlite_lease
from sqrlly.runtime.runner import run_workflow
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph, Settings, load_workflow

CHECKPOINT_DB = ".sqrlly-checkpoint.db"
APPROVAL_PENDING = 3


def _is_git_repo(workdir: str) -> bool:
    """True if workdir is inside a git working tree."""
    import subprocess
    try:
        r = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "--is-inside-work-tree"],
            capture_output=True, check=False,
        )
        return r.returncode == 0 and r.stdout.strip() == b"true"
    except FileNotFoundError:
        return False


def _effective_safe_mode(flag: bool | None, settings: Settings) -> bool:
    """Resolve the run's safe-mode: the ``--safe-mode``/``--no-safe-mode``
    flag (tri-state) overrides ``settings.safe_mode`` when given, else the
    setting applies."""
    return flag if flag is not None else settings.safe_mode


def load_config(config_file: str) -> Graph:
    if not Path(config_file).exists():
        raise click.BadParameter(f"File not found: {config_file}")
    try:
        return load_workflow(config_file)
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e))


def _thread_id_for(config: Graph, workdir: str) -> str:
    """Deterministic thread_id for a (workflow, workdir) pair."""
    key = f"1.0:{config.source_path or config.settings.workflow_dir}:{config.name}:{Path(workdir).resolve()}"
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _db_path(workdir: str) -> str:
    return str(Path(workdir) / CHECKPOINT_DB)


async def _latest_run_id(conn: Any, workflow_key: str) -> str | None:
    async with conn.execute(
        "SELECT run FROM sqrlly_runs WHERE workflow = ?", (workflow_key,),
    ) as cursor:
        row = await cursor.fetchone()
    return row[0] if row is not None else None


def _run_thread_id(workflow_key: str, run_id: str) -> str:
    return f"{workflow_key}:{run_id}"


def _run_artifact_summary(
    config: Graph, workdir: str, log_file: str | None,
) -> list[str]:
    """Lines telling the user where this run left its output, log, and
    artifacts. The live renderer shows only per-node status, so this
    epilogue is the one place a normal terminal run surfaces locations.

    - output: promoted files declared via top-level `outputs`s that
      actually exist on disk; otherwise a pointer to run state + `--log`.
    - log: the resolved `--log` path, or how to capture one.
    - artifacts: the checkpoint DB and the run files directory, when present.
    """
    wd = Path(workdir)
    label_w = 12

    def row(label: str, value: str) -> str:
        return f"  {label:<{label_w}}{value}"

    # output — declared contract files that were actually produced
    seen: set[Path] = set()
    produced: list[Path] = []
    for node in config.nodes:
        if node.outputs is None:
            continue
        for rel in node.outputs.files:
            for path in sorted(matching_output_files(rel, str(wd))):
                p = path.resolve()
                if p not in seen:
                    seen.add(p)
                    produced.append(p)
    lines = ["where to find things:"]
    if produced:
        lines.append(row("output:", ", ".join(str(p) for p in produced)))
    else:
        lines.append(row(
            "output:",
            "in run state (node outputs) — pass --log <file>.jsonl to capture",
        ))

    # log
    if log_file:
        lines.append(row("log:", str(Path(log_file).resolve())))
    else:
        lines.append(row("log:", "not captured — re-run with --log run.jsonl"))

    # Artifacts include the checkpoint and any run files on disk.
    artifacts: list[str] = []
    db = (wd / CHECKPOINT_DB).resolve()
    if db.exists():
        artifacts.append(f"checkpoint {db}")
    run_files = (wd / ".sqrlly").resolve()
    if run_files.exists():
        artifacts.append(f"run files {run_files}/")
    if artifacts:
        lines.append(row("artifacts:", "; ".join(artifacts)))
    return lines


def _emit_warnings(config: Graph) -> None:
    """Print advisory lint warnings to stderr (non-fatal)."""
    for warning in collect_warnings(config):
        click.echo(click.style(f"warning: {warning}", fg="yellow"), err=True)


@click.group()
@click.version_option(package_name="sqrlly", prog_name="sqrlly")
def cli():
    """sqrlly — workflow orchestrator."""
    pass


@cli.command()
@click.argument("directory", default=None, required=False, type=click.Path())
@click.option(
    "--skill", is_flag=True,
    help="Install the agent skill into <repo>/.agents/skills/sqrlly/ "
         "instead of scaffolding a workflow.",
)
@click.option(
    "--example", "example", default=None, metavar="NAME",
    help="Scaffold a bundled example (see --list-examples).",
)
@click.option(
    "--list-examples", "list_examples", is_flag=True,
    help="List the bundled examples available to --example.",
)
def init(
    directory: str | None, skill: bool, example: str | None,
    list_examples: bool,
):
    """Scaffold a runnable workflow into DIRECTORY.

    Bare: a minimal workflow (DIRECTORY default `.`). With --skill: install
    the agent skill doc into the working repo. With --example NAME: scaffold
    a bundled example (DIRECTORY default `./NAME`). --list-examples prints the
    available examples.
    """
    if sum(bool(x) for x in (skill, example, list_examples)) > 1:
        raise click.ClickException(
            "--skill, --example, and --list-examples may not be combined."
        )
    if list_examples:
        init_list_examples()
    elif example:
        init_example(example, directory or example)
    elif skill:
        init_skill(directory or ".")
    else:
        init_command(directory or ".")


@cli.command()
@click.argument("config_file", type=click.Path())
def validate(config_file: str):
    """Validate a workflow configuration file."""
    try:
        config = load_config(config_file)
        build_workflow_graph(config)
    except Exception as e:
        raise click.ClickException(str(e))
    _emit_warnings(config)
    click.echo(
        f"Valid: {config.name} ({len(config.nodes)} nodes)"
    )


def _checkpoint_status(run_id: str, checkpoint: dict, *, details: bool) -> dict[str, Any]:
    channels = checkpoint["channel_values"]
    if not isinstance(channels, dict):
        raise ValueError("Invalid checkpoint state")
    report = {
        "run_id": run_id,
        "checkpoint_id": checkpoint["id"],
        "saved_at": checkpoint["ts"],
        "completed_nodes": sorted(channels.get("completed_nodes", ())),
        "failed_nodes": sorted(channels.get("failed_nodes", ())),
    }
    if details:
        report.update(
            retries=channels.get("retries", {}),
            evaluations=channels.get("evaluations", {}),
            errors=channels.get("errors", []),
        )
    if approval := channels.get("pending_approval"):
        report["pending_approval"] = {
            key: approval[key] for key in ("node", "message", "request_id") if key in approval
        }
        if decision := channels.get("approval_decision"):
            report["pending_approval"]["decision"] = decision
    return report


async def _read_saved_status(
    config: Graph, workdir: str, selected_run_id: str | None = None, history: int | None = None,
) -> dict[str, Any]:
    import aiosqlite
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    path = Path(_db_path(workdir)).resolve()
    missing = f"no checkpoint for this workflow at {path}"
    if not path.exists():
        raise click.ClickException(missing)

    workflow_key = _thread_id_for(config, workdir)
    async with aiosqlite.connect(f"{path.as_uri()}?mode=ro", uri=True) as conn:
        await conn.execute("BEGIN")
        tables = await conn.execute_fetchall(
            "SELECT name FROM sqlite_schema "
            "WHERE type = 'table' AND name IN ('sqrlly_runs', 'checkpoints')",
        )
        if len(tables) != 2:
            raise click.ClickException(missing)
        latest_run_id = await _latest_run_id(conn, workflow_key)
        run_id = selected_run_id or latest_run_id
        if history is not None:
            rows = await conn.execute_fetchall(
                """WITH recent AS (
                    SELECT thread_id, MAX(checkpoint_id) AS last_id
                    FROM checkpoints
                    WHERE thread_id LIKE ? AND checkpoint_ns = ''
                    GROUP BY thread_id
                    ORDER BY last_id DESC LIMIT ?
                )
                SELECT checkpoint.thread_id, checkpoint.type, checkpoint.checkpoint
                FROM recent
                JOIN checkpoints AS checkpoint ON checkpoint.thread_id = recent.thread_id
                    AND checkpoint.checkpoint_ns = '' AND checkpoint.checkpoint_id = recent.last_id
                ORDER BY recent.last_id DESC""",
                (f"{workflow_key}:%", history),
            )
        elif run_id is not None:
            # The saver runs schema setup before reading. Select its latest root checkpoint directly.
            rows = await conn.execute_fetchall(
                "SELECT thread_id, type, checkpoint FROM checkpoints "
                "WHERE thread_id = ? AND checkpoint_ns = '' ORDER BY checkpoint_id DESC LIMIT 1",
                (_run_thread_id(workflow_key, run_id),),
            )
        else:
            raise click.ClickException(missing)
    if not rows:
        raise click.ClickException(missing)

    serializer = JsonPlusSerializer(allowed_msgpack_modules=None)
    runs = [
        _checkpoint_status(thread_id.partition(":")[2], serializer.loads_typed((kind, data)),
                           details=history is None)
        for thread_id, kind, data in rows
    ]
    if history is None:
        return {"workflow": config.name, **runs[0]}
    return {"workflow": config.name, "latest_run_id": latest_run_id, "runs": runs}


@cli.command()
@click.argument("config_file", type=click.Path())
@click.option("--workdir", "-w", default=".", help="Working directory used by run.")
@click.option("--json", "json_output", is_flag=True, help="Print the saved status as JSON.")
@click.option("--run-id", "selected_run_id", type=click.UUID, help="Select an earlier saved run.")
@click.option("--history", type=click.IntRange(1, 100), metavar="LIMIT", help="List recent saved runs.")
def status(
    config_file: str, workdir: str, json_output: bool, selected_run_id: uuid.UUID | None, history: int | None,
) -> None:
    """Show saved checkpoints for a workflow."""
    if history is not None and selected_run_id is not None:
        raise click.UsageError("--history and --run-id cannot be combined.")
    config = load_config(config_file)
    try:
        run_id = selected_run_id.hex if selected_run_id is not None else None
        report = asyncio.run(_read_saved_status(config, workdir, run_id, history))
    except (sqlite3.DatabaseError, ValueError, TypeError, KeyError, NotImplementedError) as exc:
        raise click.ClickException(f"Cannot read checkpoint at {_db_path(workdir)}: {exc}") from exc

    if json_output:
        click.echo(json.dumps(report, sort_keys=True))
        return

    if history is not None:
        click.echo(f"Saved runs for {report['workflow']}")
        if report["latest_run_id"]:
            click.echo(f"Default resume target: {report['latest_run_id']}")
        for item in report["runs"]:
            complete = len(item["completed_nodes"])
            failed = len(item["failed_nodes"])
            click.echo(f"{item['run_id']}  {item['saved_at']}  {complete} completed, {failed} failed")
            if approval := item.get("pending_approval"):
                decision = approval.get("decision")
                label = f"Saved decision {decision}" if decision else "Pending approval"
                click.echo(f"  {label}: {approval.get('node')} ({approval.get('request_id')})")
        return

    click.echo(f"Saved checkpoint for {report['workflow']} (run {report['run_id']})")
    click.echo(f"Saved at {report['saved_at']}")
    click.echo(f"Completed: {', '.join(report['completed_nodes']) or '(none)'}")
    click.echo(f"Failed: {', '.join(report['failed_nodes']) or '(none)'}")
    if approval := report.get("pending_approval"):
        click.echo(f"Pending approval: {approval.get('node')} ({approval.get('request_id')})")
        click.echo(f"  {approval.get('message')}")
    for node, count in sorted(report["retries"].items()):
        click.echo(f"Retries for {node}: {count}")
    for node, records in sorted(report["evaluations"].items()):
        for record in records:
            result = json.dumps(record["result"], sort_keys=True)
            click.echo(f"Gate for {node}, attempt {record['attempt']}: {result}")
    for error in report["errors"]:
        click.echo(f"Error in {error['node']} ({error.get('kind', 'node_error')}): {error['error']}")


@cli.command()
@click.argument("config_file", type=click.Path())
def graph(config_file: str):
    """Render the workflow topology as a Mermaid diagram.

    Uses the same schema reconstruction as ``view`` — the AUTHORED
    graph (depends_on edges, route edges, fan-out hexagons, gated-node
    styling), not the compiled LangGraph. Compiled routing is runtime
    ``Command(goto=...)``, invisible to a static render, so drawing the
    compiled graph would show gated/fan-out topology edge-less.
    """
    from sqrlly.cli.view import render_mermaid

    config = load_config(config_file)
    click.echo(render_mermaid(config))


@cli.command()
@click.argument("config_file", type=click.Path(exists=True))
@click.option(
    "--log", "log_file", type=click.Path(exists=True),
    help="JSONL log from a prior run; adds status overlay + per-node "
         "event slices. Without it, the page renders authoring view "
         "(topology + per-node config only).",
)
@click.option(
    "--out", "out_path", type=click.Path(),
    help="Output HTML path. Defaults to "
         "<workdir>/sqrlly-view.html.",
)
@click.option(
    "--workdir", "-w", default=".",
    help="Working directory (used to resolve --out default).",
)
@click.option(
    "--direction", default="TB",
    type=click.Choice(["TB", "LR", "BT", "RL"], case_sensitive=False),
    help="Mermaid layout direction. TB (top-to-bottom, default), "
         "LR (left-to-right), BT, RL.",
)
def view(
    config_file: str,
    log_file: str | None,
    out_path: str | None,
    workdir: str,
    direction: str,
):
    """Render a workflow as a self-contained HTML viewer.

    Two modes:
      Authoring: `sqrlly view <yaml>` — topology + per-node
        config panel. No runtime overlay.
      Debug: `sqrlly view <yaml> --log <jsonl>` — same plus
        status overlay (passed/failed/retried/untouched) and
        per-node log slices on click.
    """
    from sqrlly.cli.view import read_jsonl_log, render_view

    config = load_config(config_file)

    events: list[dict] | None = None
    if log_file:
        try:
            events = read_jsonl_log(Path(log_file))
        except Exception as e:
            raise click.ClickException(f"Reading log: {e}")

    html = render_view(config, events, direction=direction.upper())

    if out_path is None:
        out_path = str(Path(workdir) / "sqrlly-view.html")
    Path(out_path).write_text(html)
    click.echo(f"Wrote {out_path}", err=True)


async def _run_async(
    config: Graph,
    workdir: str,
    dry_run: bool,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    log_file: str | None,
    quiet: bool = False,
    entry: str | None = None,
    safe_mode: bool | None = None,
    run_vars: dict[str, str] | None = None,
    refresh_items: bool = False,
    selected_run_id: str | None = None,
    approval: tuple[str, str, str, str] | None = None,
) -> dict:
    """Inner async runner — wires checkpointer, executor, and state.

    Logger lifecycle: constructed up front so the compile layer can hand
    it to subgraph wrappers (subgraph-internal events project into the
    same JSONL, prefixed with the parent node id). The runner itself
    only handles the outer state stream; CLI owns workflow_start /
    workflow_end / close().

    Two subscribers may attach to the event stream:
      - `JsonlLogger` when `--log <path>` is set
      - `TerminalRenderer` when stdout is a TTY and `--quiet` not set
    `TeeLogger` fans events to both when both are present.
    """
    import sys

    from sqrlly.runtime.logging import JsonlLogger
    from sqrlly.runtime.terminal import TeeLogger, TerminalRenderer

    thread_id = _thread_id_for(config, workdir)

    subs: list[Any] = []
    jsonl: JsonlLogger | None = None
    if log_file is not None:
        jsonl = JsonlLogger(log_file, invocation_id=uuid.uuid4().hex)
        subs.append(jsonl)
    if not quiet and not dry_run and sys.stdout.isatty():
        subs.append(TerminalRenderer(config))

    logger: Any | None
    if len(subs) == 0:
        logger = None
    elif len(subs) == 1:
        logger = subs[0]
    else:
        logger = TeeLogger(*subs)

    if logger is not None:
        logger.emit({
            "event": "workflow_start",
            "workflow": config.name,
            "dry_run": dry_run,
        })

    result: dict = {}
    status = "failed"
    try:
        result = await _execute_workflow(
            config, workdir, dry_run, resume, resume_from, rerun_all,
            thread_id=thread_id, logger=logger, entry=entry, safe_mode=safe_mode, log_file=log_file,
            run_vars=run_vars, refresh_items=refresh_items, selected_run_id=selected_run_id, approval=approval,
        )
        if result.get("pending_approval"):
            status = "pending_approval"
        else:
            status = "failed" if result.get("failed_nodes") else "succeeded"
        return result
    except (asyncio.CancelledError, KeyboardInterrupt):
        status = "interrupted"
        raise
    finally:
        if logger is not None:
            from sqrlly.runtime.logging import failed_kinds_map
            logger.emit({
                "event": "workflow_end",
                "status": status,
                "completed": len(result.get("completed_nodes", set())),
                "failed": len(result.get("failed_nodes", set())),
                "failed_kinds": failed_kinds_map(
                    result.get("failed_nodes", set()),
                    result.get("errors", []),
                ),
                **({"pending_approval": result["pending_approval"]} if status == "pending_approval" else {}),
            })
            logger.close()


async def _seed_state(
    cp: Any,
    config: Graph,
    workdir: str,
    *,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    entry: str | None,
    thread_id: str,
    run_vars: dict[str, str] | None = None,
    refresh_items: bool = False,
) -> dict:
    """Seed the initial WorkflowState for a run and wipe the checkpoint
    thread (so reducers don't merge with stale state). Three modes:
    resume from checkpoint, cold-start at ``entry``, or a fresh run.
    """
    if resume:
        prev = await cp.aget_tuple({"configurable": {"thread_id": thread_id}})
        if prev is None:
            raise click.ClickException(
                f"No saved state for this workflow at {_db_path(workdir)}"
            )
        old = dict(prev.checkpoint.get("channel_values", {}))
        if old.get("pending_approval"):
            raise click.ClickException("Pending approval must continue from its saved checkpoint.")
        from sqrlly.compile.resume import compute_skip_set
        prior_completed = set(old.get("completed_nodes", set()))
        prior_failed = set(old.get("failed_nodes", set()))
        from sqrlly.compile._manifest import _read_manifest, item_hash
        from sqrlly.runtime.result import ManifestError

        prior_manifests = old.get("fan_children", {})
        fan_prior = {}
        changed = set(resume_from)
        if not rerun_all:
            for node in config.nodes:
                if not node.for_each or node.id not in prior_manifests:
                    continue
                if old.get("fan_ready", {}).get(node.id) is False:
                    changed.add(node.id)
                prior_order = prior_manifests[node.id]
                fan_prior[node.id] = set(prior_order)
                try:
                    branches = _read_manifest(old, node, workflow_dir=config.workflow_dir)
                    prior_inputs = old.get("fan_inputs", {}).get(node.id, {})
                    values_changed = refresh_items and any(
                        item_hash(item) != prior_inputs.get(child, (None, 0))[0] for child, item in branches
                    )
                    if values_changed or [child for child, item in branches] != prior_order:
                        changed.add(node.id)
                except ManifestError:
                    # Dispatch records the error on the parent so a repaired manifest can resume.
                    changed.add(node.id)
            # A child can occur in several waves; only success in the checkpoint's active wave counts.
            attempts = old.get("attempts", {})
            child_attempts = old.get("fan_child_attempts", {})
            prior_completed = {
                key for key in prior_completed
                if "::" not in key or key.partition("::")[0] not in fan_prior
                or (key in fan_prior[key.partition("::")[0]]
                    and child_attempts.get(key) == attempts.get(key.partition("::")[0], 0))
            }
        skip = set() if rerun_all else compute_skip_set(config, prior_completed, prior_failed, changed)
        state = {
            **old,
            "completed_nodes": skip,
            "failed_nodes": set(), "retries": {}, "errors": [],
            "workdir": workdir, "dry_run": False, "_resume_skip": skip,
            "run_vars": old.get("run_vars", {}),
            "_fan_prior_children": fan_prior, "_refresh_items": refresh_items,
            "_fan_resume_attempts": {node.id: old.get("attempts", {}).get(node.id, 0)
                                     for node in config.nodes if node.for_each},
            "_snapshot_reexecute": prior_failed | (prior_completed - skip),
        }
        if rerun_all:
            state.pop("_cold_entry", None)
        if refresh_items:
            click.echo(f"Resuming: {len(skip)} completed nodes eligible for reuse before item comparison.")
        else:
            click.echo(f"Resuming: skipping {len(skip)} completed nodes from the checkpoint.")
        # Wipe the thread so reducers don't merge with stale state
        await cp.adelete_thread(thread_id)
        return state

    await cp.adelete_thread(thread_id)
    state = make_initial_state(
        workflow_name=config.name, workdir=workdir, dry_run=False, run_vars=run_vars or {},
    )
    if entry is not None:
        # Cold start at `entry`: NO checkpoint read. Seed FRESH state, then
        # compute the skip set as if every top-level node had already
        # completed and `entry` were the sole rerun target — the existing
        # dirty closure dirties `entry` + its downstream and freezes the
        # rest. Reseed completed_nodes + _resume_skip to that frozen set so
        # downstream join/barrier guards see upstream as done, exactly like
        # the resume reseed. The entry node trusts ON-DISK upstream
        # artifacts: upstream never ran this session, so node_outputs is
        # empty and `{{upstream}}` template vars resolve to nothing — an
        # --entry node must read files, not interpolate upstream output.
        from sqrlly.compile.resume import compute_skip_set
        all_ids = {n.id for n in config.nodes}
        skip = compute_skip_set(config, all_ids, set(), {entry})
        state["completed_nodes"] = set(skip)
        state["_resume_skip"] = set(skip)
        state["_cold_entry"] = entry
        click.echo(
            f"Cold start at {entry!r}: running "
            f"{len(all_ids - skip)} node(s) ({entry} + downstream); "
            f"freezing {len(skip)} upstream (trusting on-disk artifacts)."
        )
    return state


async def _execute_workflow(
    config: Graph,
    workdir: str,
    dry_run: bool,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    *,
    thread_id: str,
    logger: Any | None,
    entry: str | None = None,
    safe_mode: bool | None = None,
    log_file: str | None = None,
    run_vars: dict[str, str] | None = None,
    refresh_items: bool = False,
    selected_run_id: str | None = None,
    approval: tuple[str, str, str, str] | None = None,
) -> dict:
    if config.has_human_approval:
        workdir = str(Path(workdir).resolve())
    if dry_run:
        compiled = build_workflow_graph(config, None, logger=logger, entry=entry)
        state = make_initial_state(
            workflow_name=config.name, workdir=workdir, dry_run=True, run_vars=run_vars or {},
        )
        if entry is not None:
            from sqrlly.compile.resume import compute_skip_set
            state["_cold_entry"] = entry
            skip = compute_skip_set(config, {node.id for node in config.nodes}, set(), {entry})
            state.update(completed_nodes=skip, _resume_skip=skip)
        return await run_workflow(compiled, state, config, logger=logger)

    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.types import Command

    dispatch = DispatchExecutor(
        workdir=workdir, settings=config.settings,
        safe_mode=_effective_safe_mode(safe_mode, config.settings),
    )

    if selected_run_id is not None and not Path(_db_path(workdir)).exists():
        raise click.ClickException(f"No saved run {selected_run_id} at {_db_path(workdir)}")

    async with AsyncExitStack() as resources, AsyncSqliteSaver.from_conn_string(_db_path(workdir)) as cp:
        await cp.setup()
        # Fresh invocations have independent checkpoints even when their workflow and workdir match.
        workflow_key = thread_id
        await cp.conn.execute(
            "CREATE TABLE IF NOT EXISTS sqrlly_runs (workflow TEXT PRIMARY KEY, run TEXT NOT NULL)"
        )
        if selected_run_id is not None:
            run_id = selected_run_id
        elif resume:
            run_id = await _latest_run_id(cp.conn, workflow_key)
            if run_id is None:
                raise click.ClickException(f"No saved state for this workflow at {_db_path(workdir)}")
        else:
            run_id = uuid.uuid4().hex
        thread_id = _run_thread_id(workflow_key, run_id)
        lease_dir = Path(workdir) / ".sqrlly" / "run-locks"
        lease_dir.mkdir(parents=True, exist_ok=True)
        lease_key = hashlib.sha256(thread_id.encode()).hexdigest()
        # Claim the run before publishing it or touching its checkpoints.
        resources.enter_context(closing(acquire_sqlite_lease(lease_dir / lease_key, timeout=0)))
        run_config = {"configurable": {"thread_id": thread_id}}
        previous = await cp.aget_tuple(run_config) if resume else None
        if selected_run_id is not None and previous is None:
            raise click.ClickException(f"No saved run {selected_run_id} for this workflow and workdir")
        saved = previous.checkpoint["channel_values"] if previous else {}
        saved_approval_digest = saved.get("approval_workflow_digest")
        if previous and (config.has_human_approval or saved_approval_digest is not None):
            if saved_approval_digest != config.approval_digest:
                raise click.ClickException("Workflow definition changed for a run with approvals. Restore it or start a fresh run.")
        pending = saved.get("pending_approval")
        if pending:
            if resume_from or rerun_all or refresh_items:
                raise click.ClickException("Pending approval requires plain --resume or --resume with --approval.")
        if approval is not None and (
            not pending or approval[:3] != (pending["node"], run_id, pending["request_id"])
        ):
            raise click.ClickException("Approval does not match the pending node, run ID, and request ID.")
        recorded_decision = saved.get("approval_decision") if pending else None
        if approval is not None and recorded_decision is not None and approval[3] != recorded_decision:
            raise click.ClickException(f"The {recorded_decision} decision is already saved for this request.")
        if not resume:
            await cp.conn.execute(
                "INSERT OR REPLACE INTO sqrlly_runs VALUES (?, ?)", (workflow_key, run_id)
            )
            await cp.conn.commit()
        if _is_git_repo(workdir):
            executor_obj = ForemanExecutor(
                inner=dispatch,
                base_workdir=workdir,
                max_parallel_jobs=config.settings.max_parallel_jobs,
                workflow=f"{config.name}-{workflow_key}", run=run_id, recover=resume and not rerun_all,
                exclude_paths=(str(Path(log_file).resolve()),) if log_file else (),
                settings=config.settings,
            )
        else:
            if pending and saved.get("node_worktrees", {}).get(pending["node"]):
                raise click.ClickException("Approval requires its original Git working directory.")
            # Only surface in non-interactive contexts. When the live
            # renderer owns stdout, any mid-run write (even on stderr)
            # collides with the cursor-managed grid.
            import sys
            if not sys.stdout.isatty():
                click.echo(
                    "Note: workdir is not a git repo — running without "
                    "worktree isolation (foreman disabled).",
                    err=True,
                )
            executor_obj = dispatch

        clean = False
        try:
            state: Any = None
            if not pending:
                state = await _seed_state(
                    cp, config, workdir,
                    resume=resume, resume_from=resume_from, rerun_all=rerun_all,
                    entry=entry, thread_id=thread_id, run_vars=run_vars, refresh_items=refresh_items,
                )
                if config.has_human_approval:
                    state["approval_workflow_digest"] = config.approval_digest
                if isinstance(executor_obj, ForemanExecutor):
                    executor_obj.reexecute.update(state.pop("_snapshot_reexecute", set()))
            compiled = build_workflow_graph(
                config, executor_obj, checkpointer=cp, logger=logger, entry=entry or (state or {}).get("_cold_entry"),
            )
            result = None
            if pending and recorded_decision is None:
                snapshot = await compiled.aget_state(run_config)
                if approval is not None:
                    interrupts = snapshot.interrupts
                    if len(interrupts) != 1 or interrupts[0].value != pending:
                        raise click.ClickException("No matching approval interrupt. Run --resume to recover the checkpoint.")
                    state = Command(resume={interrupts[0].id: {
                        "request_id": pending["request_id"],
                        "decision": approval[3],
                    }})
                elif snapshot.interrupts:
                    result = snapshot.values
            if result is None:
                result = await run_workflow(compiled, state, config, thread_id=thread_id, logger=logger)
            if waiting := result.get("pending_approval"):
                result["pending_approval"] = {
                    "node": waiting["node"], "message": waiting["message"],
                    "request_id": waiting["request_id"], "run_id": run_id,
                }
            elif config.has_human_approval and isinstance(executor_obj, ForemanExecutor):
                executor_obj.set_approval_pending(False)
            for ref in sorted(result.get("retained_refs", set())):
                click.echo(f"Retained snapshot: {ref}")
            clean = not (result.get("failed_nodes") or result.get("pending_approval"))
        finally:
            if isinstance(executor_obj, ForemanExecutor):
                await executor_obj.close(clean=clean and selected_run_id is None)
            else:
                await executor_obj.close()

        return result


def _parse_run_vars(values: tuple[str, ...]) -> dict[str, str]:
    variables = {}
    for value in values:
        name, separator, text = value.partition("=")
        if not separator or not name.isidentifier():
            raise click.BadParameter("expected NAME=VALUE with an identifier as NAME", param_hint="--var")
        if name in variables:
            raise click.BadParameter(f"duplicate variable {name!r}", param_hint="--var")
        variables[name] = text
    return variables


@cli.command()
@click.argument("config_file", type=click.Path())
@click.option("--workdir", "-w", default=".", help="Working directory")
@click.option(
    "--dry-run", is_flag=True, help="Validate and trace without executing"
)
@click.option(
    "--resume", is_flag=True, help="Resume from the last checkpoint"
)
@click.option("--run-id", "selected_run_id", type=click.UUID,
              help="Select a saved run to resume by ID.")
@click.option(
    "--approval", type=(str, click.UUID, str, click.Choice(["approve", "reject"])),
    metavar="NODE RUN_ID REQUEST_ID DECISION",
    help="With --resume, decide the named human approval request.",
)
@click.option("--refresh-items", is_flag=True,
              help="Resume and rerun children whose manifest values changed.")
@click.option(
    "--resume-from", "resume_from", multiple=True,
    help="Re-run this node and everything downstream; freeze upstream. "
         "Implies --resume. Repeatable.",
)
@click.option(
    "--rerun-all", "rerun_all", is_flag=True,
    help="With --resume: re-execute every node (pre-0.6 full replay; "
         "disables skip-completed).",
)
@click.option(
    "--entry", "entry", default=None, metavar="NODE",
    help="Cold-start at NODE: run NODE and everything downstream, freezing "
         "everything upstream WITHOUT a checkpoint (the upstream artifacts "
         "must already be on disk). The entry node sees EMPTY {{upstream}} "
         "template vars — it must read files, not interpolate upstream "
         "output. Mutually exclusive with --resume / --resume-from / "
         "--rerun-all.",
)
@click.option("--log", "log_file", type=click.Path(), help="JSONL log output file")
@click.option(
    "--var", "variables", multiple=True, metavar="NAME=VALUE",
    help="Set a top-level template variable (repeatable). Resumes use saved values.",
)
@click.option(
    "--quiet", "-q", is_flag=True,
    help="Suppress the live terminal renderer (useful in CI/piped runs).",
)
@click.option(
    "--safe-mode/--no-safe-mode", "safe_mode", default=None,
    help="Run Anthropic's CLI provider with Claude operator customizations "
         "(output styles, CLAUDE.md, skills, MCP, hooks) disabled. Has no "
         "effect on Codex. Overrides settings.safe_mode; absent flag "
         "uses the setting.",
)
def run(
    config_file: str,
    workdir: str,
    dry_run: bool,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    entry: str | None,
    log_file: str | None,
    quiet: bool,
    safe_mode: bool | None,
    variables: tuple[str, ...],
    refresh_items: bool,
    selected_run_id: uuid.UUID | None,
    approval: tuple[str, uuid.UUID, str, str] | None,
):
    """Run a workflow from a configuration file."""
    config = load_config(config_file)

    decision = None
    if approval is not None:
        if not resume or dry_run or resume_from or rerun_all or refresh_items or entry is not None:
            raise click.UsageError("--approval requires --resume and cannot be combined with dry-run or replay options.")
        node_id, approval_run_id, request_id, choice = approval
        if selected_run_id is not None and selected_run_id != approval_run_id:
            raise click.UsageError("--run-id must match the approval run ID.")
        selected_run_id = approval_run_id
        decision = (node_id, approval_run_id.hex, request_id, choice)
    if entry is not None and config.has_human_approval:
        raise click.UsageError("--entry cannot be used in a workflow with human approvals.")
    if refresh_items and (dry_run or rerun_all or entry is not None or variables):
        raise click.UsageError("--refresh-items cannot be combined with --dry-run, --rerun-all, --entry, or --var.")
    resume = resume or bool(resume_from) or refresh_items
    if selected_run_id is not None and (not resume or dry_run):
        raise click.UsageError("--run-id requires --resume, --resume-from, or --refresh-items and cannot trace a dry run.")
    if resume and variables:
        raise click.UsageError("--var cannot be used with --resume or --resume-from. Start a fresh run to change inputs.")
    run_vars = _parse_run_vars(variables)
    if rerun_all and resume_from:
        raise click.ClickException(
            "--rerun-all and --resume-from are mutually exclusive"
        )
    valid_ids = {n.id for n in config.nodes}
    for rid in resume_from:
        if "::" in rid:
            raise click.ClickException(
                f"--resume-from {rid!r}: fan-out children are not addressable across runs"
            )
        if rid not in valid_ids:
            raise click.ClickException(
                f"--resume-from {rid!r}: unknown node id. "
                f"Valid: {', '.join(sorted(valid_ids))}"
            )

    if entry is not None:
        # --entry is a COLD start (no checkpoint); --resume family reads a
        # checkpoint. The two seed strategies are incompatible.
        if resume or rerun_all:
            raise click.ClickException(
                "--entry is mutually exclusive with --resume, "
                "--resume-from, and --rerun-all"
            )
        if "::" in entry:
            raise click.ClickException(
                f"--entry {entry!r}: fan-out children are not addressable "
                f"(name the top-level fan-out parent instead)"
            )
        if entry not in valid_ids:
            raise click.ClickException(
                f"--entry {entry!r}: unknown node id. "
                f"Valid: {', '.join(sorted(valid_ids))}"
            )

    _emit_warnings(config)

    from sqrlly.runtime.result import EvaluationError, ManifestError, RouteError
    try:
        result = asyncio.run(
            _run_async(config, workdir, dry_run, resume, resume_from, rerun_all, log_file,
                       quiet, entry, safe_mode, run_vars=run_vars, refresh_items=refresh_items,
                       selected_run_id=selected_run_id.hex if selected_run_id is not None else None,
                       approval=decision)
        )
    except KeyboardInterrupt:
        click.echo("Interrupted.", err=True)
        raise click.exceptions.Exit(130) from None
    except BlockingIOError as exc:
        raise click.ClickException("This workflow run is already active") from exc
    except (EvaluationError, ManifestError, RouteError) as e:
        # Infrastructure failures (broken validator / unreadable manifest
        # / unevaluable route predicate) halt the run loudly with a clean
        # message, not a traceback.
        raise click.ClickException(str(e))

    completed = result.get("completed_nodes", set())
    failed = result.get("failed_nodes", set())
    errors = result.get("errors", [])

    if waiting := result.get("pending_approval"):
        click.echo(f"waiting for approval: {waiting['node']}")
        click.echo(waiting["message"])
        click.echo(f"run id: {waiting['run_id']}")
        click.echo(f"request id: {waiting['request_id']}")
        if worktree := result.get("node_worktrees", {}).get(waiting["node"]):
            click.echo(f"worktree: {worktree}")
        raise click.exceptions.Exit(APPROVAL_PENDING)

    # When the live renderer was active, it already shows per-node
    # status + a "done in Xs" header — skip the redundant summary
    # block. Errors still surface so failures aren't hidden.
    import sys
    renderer_active = (
        not quiet
        and not dry_run
        and sys.stdout.isatty()
    )

    if not renderer_active:
        if dry_run:
            click.echo(f"Dry run completed: {len(completed)} nodes traced")
        else:
            click.echo(f"Completed: {len(completed)} nodes")
        if completed:
            click.echo(f"  Nodes: {', '.join(sorted(completed))}")
        if failed:
            click.echo(f"  Failed: {', '.join(sorted(failed))}")

    if errors:
        for err in errors:
            click.echo(f"  Error in {err['node']}: {err['error']}", err=True)

    # Announce where the run left things. The live renderer shows only
    # per-node status, so this is the one place a normal terminal run
    # surfaces output / log / artifact locations. Printed on success AND
    # failure (the log + checkpoint are exactly what you want on failure).
    if not quiet and not dry_run:
        for line in _run_artifact_summary(config, workdir, log_file):
            click.echo(line)

    if failed:
        raise click.exceptions.Exit(1)
