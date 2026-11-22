"""Measure external effects using real CLI and SQLite adapter processes."""

import json

from langgraph.checkpoint.sqlite import SqliteSaver
import pytest
import yaml

from test_transport_recovery import experiment, rows, run, script, workflow


ADAPTER = """
from pathlib import Path
import sqlite3, sys

db_path, fault_file, key, value = sys.argv[1:]
fault = Path(fault_file)
# Prototype a run-scoped node token using the fixture's single workflow.
# This private lookup is instrumentation, not an adapter interface.
if key == '@run':
    with sqlite3.connect('.sqrlly-checkpoint.db') as checkpoint:
        (run_id,), = checkpoint.execute('select run from sqrlly_runs')
    key = run_id + '/write'
with sqlite3.connect(db_path, timeout=10) as db:
    db.execute('insert into calls values (?, ?)', (key, value))
    if fault.read_text() != 'reject':
        db.execute('insert into effects (key, value) values (?, ?) on conflict(key) do nothing',
                   (key if key != '-' else None, value))
        if key != '-':
            saved, = db.execute('select value from effects where key = ?', (key,)).fetchone()
            if saved != value:
                sys.exit('key reused with a different payload')
mode = fault.read_text()
if mode == 'lose-once':
    fault.write_text('ok')
if mode in ('lose', 'lose-once', 'reject'):
    sys.exit(mode)
print(value, end="")
"""


@pytest.fixture
def sink(experiment):
    database = experiment / "sink.db"
    rows(database, "create table calls (key text, value text)")
    rows(database, "create table effects (id integer primary key, key text unique, value text)")
    fault = experiment / "fault"
    fault.write_text("ok")
    return database, fault, [*script(experiment, "adapter", ADAPTER), str(database), str(fault)]


def checkpoint(worker):
    with SqliteSaver.from_conn_string(str(worker / ".sqrlly-checkpoint.db")) as saver:
        latest = next(saver.list(None, limit=1))
    return latest.config["configurable"]["thread_id"], latest.checkpoint["channel_values"]


def counts(database):
    return tuple(rows(database, f"select count(*) from {table}")[0][0] for table in ("calls", "effects"))


@pytest.mark.parametrize("key", ["-", "order-7/charge-1", "@run"])
def test_rejection_lost_ack_and_deliberate_replay(experiment, sink, key):
    database, fault, adapter = sink
    worker = experiment / "worker"
    worker.mkdir()
    config = workflow(experiment, "write", [
        {"id": "write", "run": [*adapter, "{{vars.key}}", "payload"]},
    ])
    keyed = key != "-"
    fault.write_text("reject")
    run(config, worker, 1, "--var", f"key={key}")
    thread, state = checkpoint(worker)
    assert state["run_vars"] == {"key": key}
    assert set(state["failed_nodes"]) == {"write"}
    assert "write" not in state.get("completed_nodes", ())
    assert counts(database) == (1, 0)

    fault.write_text("lose")
    run(config, worker, 1, "--resume")
    assert counts(database) == (2, 1)
    resumed_thread, state = checkpoint(worker)
    assert resumed_thread == thread
    assert set(state["failed_nodes"]) == {"write"}
    assert "write" not in state.get("completed_nodes", ())

    fault.write_text("ok")
    run(config, worker, 0, "--resume")
    assert counts(database) == (3, 1 if keyed else 2)
    assert "write" in checkpoint(worker)[1]["completed_nodes"]
    run(config, worker, 0, "--resume")
    assert counts(database) == (3, 1 if keyed else 2)
    run(config, worker, 0, "--resume-from", "write")
    assert checkpoint(worker)[0] == thread
    assert counts(database) == (4, 1 if keyed else 3)

    run(config, worker, 0, "--var", f"key={key}")
    assert checkpoint(worker)[0] != thread
    assert counts(database) == (5, {"-": 4, "order-7/charge-1": 1, "@run": 2}[key])
    next_key = "order-7/charge-2" if key == "order-7/charge-1" else key
    run(config, worker, 0, "--var", f"key={next_key}")
    assert counts(database) == (6, {"-": 5, "order-7/charge-1": 2, "@run": 3}[key])
    if key == "@run":
        original_token = thread.rsplit(":", 1)[-1] + "/write"
        assert rows(database, "select key from calls order by rowid limit 4") == [(original_token,)] * 4


@pytest.mark.parametrize("keyed", [False, True])
def test_command_retry_after_commit(experiment, sink, keyed):
    database, fault, adapter = sink
    worker = experiment / "worker"
    worker.mkdir()
    fault.write_text("lose-once")
    config = workflow(experiment, "retry", [
        {"id": "write", "run": [*adapter, "order-7/charge-1" if keyed else "-", "payload"]},
    ])
    document = yaml.safe_load(config.read_text())
    document["settings"] = {"defaults": {"run": {"backend_max_retries": 1}}}
    config.write_text(yaml.safe_dump(document))
    run(config, worker, 0)
    assert counts(database) == (2, 1 if keyed else 2)
    state = checkpoint(worker)[1]
    assert state["attempts"]["write"] == 1
    assert "write" in state["completed_nodes"]


def test_refresh_wave_and_duplicate_subgraph_names(experiment, sink):
    database, _, adapter = sink
    worker = experiment / "worker"
    worker.mkdir()
    child = workflow(experiment, "same_name", [
        {"id": "write", "run": [*adapter, "{{key}}", "{{value}}"]},
    ])
    manifest = experiment / "items.json"

    def items(value, edition):
        manifest.write_text(json.dumps([
            {"id": "a", "value": value, "edition": edition},
            {"id": "b", "value": "two", "edition": 1},
        ]))

    items("one", 1)
    config = workflow(experiment, "scopes", [
        {"id": "left", "workflow": {"file": child.name, "with": {"key": "left/a/1", "value": "one"}}},
        {"id": "right", "workflow": {"file": child.name, "with": {"key": "right/a/1", "value": "one"}}},
        {"id": "batch", "for_each": {"file": manifest.name}, "run": [
            *adapter, "batch/{{item.id}}/{{item.edition}}", "{{item.value}}",
        ]},
        {"id": "gate", "depends_on": ["batch"], "run": [
            "sh", "-ec", "if test -e repeat; then mv repeat repeated; echo again; else echo done; fi",
        ], "route": [{"when": "'again' in gate", "then": "batch", "max_attempts": 4}]},
    ])
    log = experiment / "events.jsonl"
    run(config, worker, 0, "--log", str(log))
    assert counts(database) == (4, 4)
    completed = {event["node"] for line in log.read_text().splitlines()
                 if (event := json.loads(line)).get("event") == "node_completed"}
    assert {"left::write", "right::write", "batch::a", "batch::b"} <= completed

    items("revised", 2)
    run(config, worker, 0, "--resume")
    assert counts(database) == (4, 4)
    (worker / "repeat").touch()
    run(config, worker, 0, "--refresh-items")
    assert counts(database) == (7, 5)
    assert rows(database, "select key, value from effects order by key") == [
        ("batch/a/1", "one"), ("batch/a/2", "revised"), ("batch/b/1", "two"),
        ("left/a/1", "one"), ("right/a/1", "one"),
    ]
    assert rows(database, "select key, count(*) from calls group by key order by key") == [
        ("batch/a/1", 1), ("batch/a/2", 2), ("batch/b/1", 2), ("left/a/1", 1), ("right/a/1", 1),
    ]
    state = checkpoint(worker)[1]
    assert state["attempts"]["batch"] == 3
    assert json.loads(state["node_outputs"]["batch"]) == ["revised", "two"]
