"""Exercise the public CLI with durable state outside replaceable workers."""

import json
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.request import ProxyHandler, build_opener

import pytest
import yaml


# A loopback fault injector. Every POST commits a new row, then closes the
# connection before sending any response. GET uses an adapter-owned match key.
HTTP_SERVICE = """
from http.server import BaseHTTPRequestHandler, HTTPServer
import json, sqlite3, sys

class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        key = self.path.rsplit('/', 1)[-1]
        with sqlite3.connect(sys.argv[1]) as db:
            db.execute('insert into resources (key) values (?)', (key,))
        self.close_connection = True

    def do_GET(self):
        key = self.path.rsplit('/', 1)[-1]
        if key == 'ready':
            self.send_response(204)
            self.end_headers()
            return
        with sqlite3.connect(sys.argv[1]) as db:
            rows = db.execute('select id from resources where key = ?', (key,)).fetchall()
        if len(rows) != 1:
            self.send_error(404 if not rows else 409)
            return
        body = json.dumps({'id': rows[0][0]}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

with HTTPServer(('127.0.0.1', 0), Handler) as server:
    print(server.server_port, flush=True)
    server.serve_forever()
"""

HTTP_ADAPTER = """
from http.client import RemoteDisconnected
import sys
from urllib.request import ProxyHandler, Request, build_opener

method, url = sys.argv[1:]
try:
    with build_opener(ProxyHandler({})).open(Request(url, method=method), timeout=5) as response:
        print(response.read().decode())
except RemoteDisconnected:
    sys.exit('create response lost')
"""

STORE = """
import sqlite3, sys

db_path, resource_id = sys.argv[1:]
with sqlite3.connect(db_path) as db:
    db.execute('insert into imported values (?) on conflict do nothing', (int(resource_id),))
"""

# The registry stands in for an independent job owner. Its submission and
# completion are separate from the launcher and its checkpoint.
JOB_OWNER = """
import json, sqlite3, sys, uuid

db_path, action, *args = sys.argv[1:]
with sqlite3.connect(db_path) as db:
    if action == 'submit':
        handle = uuid.uuid4().hex
        db.execute('insert into jobs values (?, ?)', (handle, 'accepted'))
    elif action == 'finish':
        db.execute("update jobs set status = 'complete' where handle = ?", args)
    else:
        row = db.execute('select status from jobs where handle = ?', args).fetchone()
        if row is None:
            sys.exit('unknown handle')
        print(json.dumps({'handle': args[0], 'status': row[0]}))
if action == 'submit':
    print(handle)
"""

LAUNCHER = """
from pathlib import Path
import os, subprocess, sys

owner, registry, receipt = sys.argv[1:]
handle = subprocess.check_output([sys.executable, owner, registry, 'submit'], text=True).strip()
if receipt != '-':
    with Path(receipt).open('x') as saved:
        saved.write(handle)
        saved.flush()
        os.fsync(saved.fileno())
sys.exit('launcher stopped before emitting structured output')
"""

COLLECTOR = """
from pathlib import Path
import os, sys

owner, registry, receipt = sys.argv[1:]
if not Path(receipt).is_file():
    sys.exit('missing handle')
os.execv(sys.executable, [sys.executable, owner, registry, 'query', Path(receipt).read_text()])
"""


@pytest.fixture
def experiment():
    # Keep CLI workdirs out of the checkout even when pytest --basetemp is in Git.
    with TemporaryDirectory(prefix="sqrlly-transport-") as directory:
        yield Path(directory)


def script(directory, name, source):
    path = directory / f"{name}.py"
    path.write_text(source)
    return [sys.executable, str(path)]


def workflow(directory, name, nodes):
    path = directory / f"{name}.yaml"
    path.write_text(yaml.safe_dump({"name": name, "nodes": nodes}))
    return path


def run(config, worker, expected, *flags):
    result = subprocess.run(
        [str(Path(sys.executable).with_name("sqrlly")), "run", str(config), "--workdir", str(worker), *flags],
        text=True, capture_output=True, timeout=20,
    )
    output = result.stdout + result.stderr
    assert result.returncode == expected, output
    print(f"{config.stem} [{worker.name}] {' '.join(flags)} -> {result.returncode}\n{output}")
    return output


def rows(database, query):
    with sqlite3.connect(database) as db:
        return db.execute(query).fetchall()


def schema(directory, properties):
    path = directory / "receipt.json"
    path.write_text(json.dumps({
        "type": "object", "required": list(properties), "properties": properties,
    }))
    return {"schema": str(path)}


def test_lost_create_reply_and_lookup_after_worker_replacement(experiment):
    resources, imports = experiment / "resources.db", experiment / "imports.db"
    rows(resources, "create table resources (id integer primary key, key text)")
    rows(imports, "create table imported (id integer primary key)")
    adapter = script(experiment, "http_adapter", HTTP_ADAPTER)
    store = script(experiment, "store", STORE)
    output = schema(experiment, {"id": {"type": "integer"}})
    service = subprocess.Popen(
        [*script(experiment, "http_service", HTTP_SERVICE), str(resources)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert select.select([service.stdout], [], [], 5)[0], "loopback service did not start"
        port = service.stdout.readline().strip()
        if not port:
            pytest.fail(service.communicate(timeout=5)[1])
        assert port.isdecimal()
        base = f"http://127.0.0.1:{port}"
        with build_opener(ProxyHandler({})).open(f"{base}/ready", timeout=5) as response:
            assert response.status == 204
        create = workflow(experiment, "create", [
            {"id": "create", "run": [*adapter, "POST", "{{vars.url}}"]},
        ])
        lookup = workflow(experiment, "lookup", [
            {"id": "lookup", "run": [*adapter, "GET", "{{vars.url}}"], "outputs": output},
            {"id": "store", "depends_on": ["lookup"], "run": [*store, str(imports), "{{lookup.id}}"]},
        ])
        worker = experiment / "worker"
        worker.mkdir()
        assert "create response lost" in run(create, worker, 1, "--var", f"url={base}/unique")
        assert rows(resources, "select id, key from resources") == [(1, "unique")]
        assert rows(imports, "select id from imported") == []

        worker.rename(experiment / "retired")
        worker.mkdir()
        assert "No saved state" in run(create, worker, 1, "--resume")
        run(lookup, worker, 0, "--var", f"url={base}/unique")
        assert rows(imports, "select id from imported") == [(1,)]

        # Blind replay of a different create duplicates it in the owner's store.
        run(create, worker, 1, "--var", f"url={base}/ambiguous")
        assert "create response lost" in run(create, worker, 1, "--resume")
        assert rows(resources, "select id, key from resources order by id") == [
            (1, "unique"), (2, "ambiguous"), (3, "ambiguous"),
        ]
        assert "HTTP Error 409: Conflict" in run(lookup, worker, 1, "--var", f"url={base}/ambiguous")
        assert rows(imports, "select id from imported") == [(1,)]
    finally:
        service.terminate()
        service.communicate(timeout=5)


@pytest.mark.parametrize("save_handle", [False, True])
def test_job_handle_after_launcher_restart(experiment, save_handle):
    jobs = experiment / "jobs.db"
    rows(jobs, "create table jobs (handle text primary key, status text)")
    owner = script(experiment, "owner", JOB_OWNER)
    launcher = script(experiment, "launcher", LAUNCHER)
    collector = script(experiment, "collector", COLLECTOR)
    receipt = experiment / "handle"
    worker = experiment / "worker"
    worker.mkdir()
    launch = workflow(experiment, "launch", [{
        "id": "launch",
        "run": [*launcher, owner[1], str(jobs), str(receipt) if save_handle else "-"],
    }])
    assert "launcher stopped" in run(launch, worker, 1)
    [(handle, status)] = rows(jobs, "select * from jobs")
    assert status == "accepted"
    subprocess.run([*owner, str(jobs), "finish", handle], check=True, timeout=5)

    worker.rename(experiment / "retired")
    worker.mkdir()
    assert "No saved state" in run(launch, worker, 1, "--resume")
    collect = workflow(experiment, "collect", [{
        "id": "collect", "run": [*collector, owner[1], str(jobs), str(receipt)],
        "outputs": schema(experiment, {"handle": {"type": "string"}, "status": {"const": "complete"}}),
    }, {
        "id": "record", "depends_on": ["collect"],
        "run": [sys.executable, "-c", "from pathlib import Path; import sys; "
                "Path('collected').write_text(sys.argv[1])", "{{collect.handle}}"],
        "outputs": {"files": ["collected"]},
    }])
    if save_handle:
        run(collect, worker, 0)
        assert (worker / "collected").read_text() == handle

        worker.rename(experiment / "finished")
        worker.mkdir()
        checkpoint = experiment / "finished" / ".sqrlly-checkpoint.db"
        shutil.copy2(checkpoint, worker)
        run(collect, worker, 0, "--resume")
        assert not (worker / "collected").exists()
        run(collect, worker, 0, "--resume-from", "collect")
        assert (worker / "collected").read_text() == handle

        relocated = experiment / "relocated"
        relocated.mkdir()
        shutil.copy2(checkpoint, relocated)
        assert "No saved state" in run(collect, relocated, 1, "--resume")
    else:
        assert "missing handle" in run(collect, worker, 1)
        assert not (worker / "collected").exists()
    assert rows(jobs, "select * from jobs") == [(handle, "complete")]


def test_external_transaction_survives_output_contract_replay(experiment):
    database = experiment / "effects.db"
    rows(database, "create table effects (id integer primary key)")
    write = script(experiment, "write", """
import sqlite3, sys

with sqlite3.connect(sys.argv[1]) as db:
    db.execute('insert into effects default values')
print('{"id": "wrong type"}')
""")
    worker = experiment / "worker"
    worker.mkdir()
    config = workflow(experiment, "effects", [{
        "id": "write",
        "run": [*write, str(database)],
        "outputs": schema(experiment, {"id": {"type": "integer"}}),
    }])
    assert "Output contract violated" in run(config, worker, 1)
    assert rows(database, "select id from effects order by id") == [(1,), (2,), (3,)]

    worker.rename(experiment / "retired")
    worker.mkdir()
    assert "No saved state" in run(config, worker, 1, "--resume")
    assert rows(database, "select id from effects order by id") == [(1,), (2,), (3,)]
