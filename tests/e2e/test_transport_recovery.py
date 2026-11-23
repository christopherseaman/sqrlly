"""Exercise the public CLI with durable state outside replaceable workers."""

import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
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

owner, registry, receipt, artifacts = sys.argv[1:]
if not Path(receipt).is_file():
    sys.exit('missing handle')
handle = Path(receipt).read_text()
if not (Path(artifacts) / handle).is_file():
    sys.exit('artifact store unavailable')
os.execv(sys.executable, [sys.executable, owner, registry, 'query', handle])
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


def run(config, worker, expected, *flags, cwd=None):
    result = subprocess.run(
        [str(Path(sys.executable).with_name("sqrlly")), "run", str(config), "--workdir", str(worker), *flags],
        cwd=cwd, text=True, capture_output=True, timeout=20,
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
    artifacts = experiment / "artifacts"
    artifacts.mkdir()
    (artifacts / handle).write_text("job output")

    worker.rename(experiment / "retired")
    worker.mkdir()
    assert "No saved state" in run(launch, worker, 1, "--resume")
    collect = workflow(experiment, "collect", [{
        "id": "collect", "run": [*collector, owner[1], str(jobs), str(receipt), str(artifacts)],
        "outputs": schema(experiment, {"handle": {"type": "string"}, "status": {"const": "complete"}}),
    }, {
        "id": "record", "depends_on": ["collect"],
        "run": [sys.executable, "-c", "from pathlib import Path; import sys; "
                "Path('collected').write_text(sys.argv[1])", "{{collect.handle}}"],
        "outputs": {"files": ["collected"]},
    }])
    if save_handle:
        unavailable = artifacts.rename(experiment / "offline-artifacts")
        assert "artifact store unavailable" in run(collect, worker, 1)
        assert receipt.read_text() == handle
        assert rows(jobs, "select * from jobs") == [(handle, "complete")]
        assert not (worker / "collected").exists()
        unavailable.rename(artifacts)
        run(collect, worker, 0, "--resume")
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


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX SIGTERM")
@pytest.mark.parametrize("key", ["-", "charge-1"])
@pytest.mark.parametrize("git", [False, True], ids=["plain", "git-isolated"])
def test_sigterm_after_external_commit(experiment, request, key, git):
    database = experiment / "effects.db"
    rows(database, "create table effects (key text unique, owner integer)")
    rows(database, "create table calls (owner integer)")
    owner = script(experiment, "owner", """
from pathlib import Path
import os, sqlite3, sys, time

database, mailbox = sys.argv[1:]
while True:
    for message in Path(mailbox).glob('*.request'):
        acknowledgment = message.with_suffix('.ack')
        if acknowledgment.exists():
            continue
        key = message.read_text()
        with sqlite3.connect(database) as db:
            db.execute('insert into calls values (?)', (os.getpid(),))
            db.execute('insert into effects values (?, ?) on conflict(key) do nothing',
                       (None if key == '-' else key, os.getpid()))
        acknowledgment.touch()
    time.sleep(0.025)
""")
    mailbox = experiment / "mailbox"
    mailbox.mkdir()
    sink = subprocess.Popen([*owner, str(database), str(mailbox)], cwd=experiment)

    def stop_sink():
        sink.terminate()
        sink.wait(timeout=5)

    request.addfinalizer(stop_sink)
    adapter = script(experiment, "adapter", """
from pathlib import Path
import os, sys, time, uuid

mailbox, key, ready, release, done = sys.argv[1:]
message = Path(mailbox) / uuid.uuid4().hex
message.write_text(key)
message.rename(message.with_suffix('.request'))
deadline = time.monotonic() + 10
while not message.with_suffix('.ack').exists():
    assert time.monotonic() < deadline, 'owner did not acknowledge'
    time.sleep(0.025)
if not Path(release).exists():
    Path(ready).write_text(str(os.getpid()))
    deadline = time.monotonic() + 30
    while not Path(release).exists() and time.monotonic() < deadline:
        time.sleep(0.025)
    Path(done).touch()
print('acknowledged')
""")
    worker = experiment / "worker"
    worker.mkdir()
    restored = experiment / "restored-launcher"
    restored.mkdir()
    if git:
        subprocess.run(["git", "init", "-q", str(worker)], check=True)
        subprocess.run(["git", "-C", str(worker), "-c", "user.name=Test", "-c",
                        "user.email=test@example.org", "commit", "--allow-empty", "-qm", "initial"],
                       check=True)
    ready, release, done = (experiment / name for name in ("ready", "release", "done"))
    config = workflow(experiment, "terminate", [
        {"id": "seed", "run": ["echo", "seed"]},
        {"id": "write", "depends_on": ["seed"], "worktree": "isolated",
         "run": [*adapter, str(mailbox), key, str(ready), str(release), str(done)]},
    ])
    with (experiment / "initial.log").open("w") as log:
        launcher = subprocess.Popen(
            [str(Path(sys.executable).with_name("sqrlly")), "run", str(config), "--workdir", str(worker)],
            cwd=experiment, stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 15
            while (not ready.exists() or not ready.read_text()) and launcher.poll() is None:
                assert time.monotonic() < deadline
                time.sleep(0.025)
            [(saved_key, owner_pid)] = rows(database, "select * from effects")
            assert saved_key == (None if key == "-" else key)
            assert owner_pid == sink.pid
            assert owner_pid != int(ready.read_text())
            launcher.send_signal(signal.SIGTERM)
            assert launcher.wait(timeout=5) == -signal.SIGTERM
            assert not done.exists()
            release.touch()
            deadline = time.monotonic() + 5
            while not done.exists():
                assert time.monotonic() < deadline
                time.sleep(0.025)
        finally:
            release.touch()
            if launcher.poll() is None:
                launcher.kill()
                launcher.wait(timeout=5)

    output = run(config, worker, 1 if git else 0, "--resume", cwd=restored)
    assert "skipping 1 completed nodes" in output
    if git:
        assert "Interrupted dispatch has no recorded result" in output
        assert list((worker / ".sqrlly").glob("wt-*"))
    effects = rows(database, "select key, owner from effects order by rowid")
    assert effects[0] == (saved_key, owner_pid)
    assert len(effects) == (1 if git or key != "-" else 2)
    attempts = rows(database, "select owner from calls order by rowid")
    assert sink.poll() is None
    assert attempts == [(owner_pid,)] * (1 if git else 2)
