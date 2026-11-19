"""Issue 42 acceptance through sqrlly validate, view and run."""

from collections import Counter
import json
import sys

from click.testing import CliRunner
import pytest
import yaml

from sqrlly.cli.main import cli


@pytest.fixture
def scenario(tmp_path):
    config_dir = tmp_path / "flows"
    config_dir.mkdir()
    workdir = tmp_path / "work"
    workdir.mkdir()
    config = config_dir / "workflow.yaml"
    schema = config_dir / "value.json"
    schema.write_text(json.dumps({"type": "string"}))
    (config_dir / "worker.py").write_text(
        "import json, pathlib, sys, time\n"
        "name = sys.argv[1]\n"
        "if name == 'slow':\n"
        "    limit = time.monotonic() + 10\n"
        "    while not pathlib.Path('fast-finished').exists():\n"
        "        if time.monotonic() > limit: raise RuntimeError('fast child never finished')\n"
        "        time.sleep(0.02)\n"
        "with open('calls', 'a') as log: log.write(name + '\\n')\n"
        "if name == 'fast': pathlib.Path('fast-finished').touch()\n"
        "print(json.dumps(name))\n"
    )
    nodes = [
        {"id": "build", "for_each": {"file": "items.json"}, "outputs": {"schema": "value.json"},
         "run": [sys.executable, str(config_dir / "worker.py"), "{{item.value}}"]},
        {"id": "collect", "depends_on": ["build"], "run": [
            sys.executable, "-c", "import pathlib,sys; pathlib.Path('aggregate').write_text(sys.argv[1])",
            "{{build}}",
        ]},
    ]
    config.write_text(yaml.safe_dump({"nodes": nodes, "settings": {"max_parallel_jobs": 4}}))
    return config, workdir, nodes


def write_items(config, *items):
    (config.parent / "items.json").write_text(json.dumps(list(items)))


def run(config, workdir, *options, success=True):
    result = CliRunner().invoke(cli, ["run", str(config), "--workdir", str(workdir), *options])
    assert (result.exit_code == 0) == success, (result.output, result.exception)
    return result.output


def counts(workdir):
    return Counter((workdir / "calls").read_text().splitlines())


def test_clean_resume_inserts_one_child_and_rebuilds_ordered_aggregate(scenario):
    config, workdir, _ = scenario
    a = {"id": "a", "value": "slow"}
    b = {"id": "b", "value": "fast"}
    new = {"id": "new", "value": "middle"}
    write_items(config, a, b)
    run(config, workdir)
    assert (workdir / "calls").read_text().splitlines() == ["fast", "slow"]
    assert json.loads((workdir / "aggregate").read_text()) == ["slow", "fast"]
    write_items(config, a, new, b)
    run(config, workdir, "--resume")
    assert counts(workdir) == {"fast": 1, "slow": 1, "middle": 1}
    assert json.loads((workdir / "aggregate").read_text()) == ["slow", "middle", "fast"]


def test_identical_idless_items_remain_distinct_across_insertion(scenario):
    config, workdir, _ = scenario
    item = {"value": "same"}
    write_items(config, item, item)
    log = workdir / "initial.jsonl"
    run(config, workdir, "--log", str(log))
    completed = {event["node"] for line in log.read_text().splitlines()
                 if (event := json.loads(line)).get("event") == "node_completed"}
    assert len({node for node in completed if node.startswith("build::")}) == 2
    write_items(config, {"value": "before"}, item, item)
    run(config, workdir, "--resume")
    assert counts(workdir) == {"same": 2, "before": 1}
    assert json.loads((workdir / "aggregate").read_text()) == ["before", "same", "same"]


def test_edited_idless_item_fails_before_starting_any_new_child(scenario):
    config, workdir, _ = scenario
    write_items(config, {"value": "original"})
    run(config, workdir)
    write_items(config, {"value": "edited"})
    output = run(config, workdir, "--resume", success=False)
    assert "drift" in output.lower()
    assert counts(workdir) == {"original": 1}


@pytest.mark.parametrize("source,shape,schema", [
    ("plan.tasks", {"tasks": [{"value": "field"}]}, {"type": "object", "properties": {"tasks": {"type": "array"}}}),
    ("plan", [{"value": "array"}], {"type": "array"}),
    ("plan", {"items": [{"value": "wrapped"}]}, None),
    ("plan", [{"value": "text"}], None),
])
def test_upstream_manifest_sources(scenario, source, shape, schema):
    config, workdir, nodes = scenario
    plan = {"id": "plan", "run": ["echo", json.dumps(shape)]}
    if schema:
        (config.parent / "plan.json").write_text(json.dumps(schema))
        plan["outputs"] = {"schema": "plan.json"}
    nodes[0].update(depends_on=["plan"], for_each=source)
    config.write_text(yaml.safe_dump({"nodes": [plan, *nodes]}))
    run(config, workdir)
    items = shape if isinstance(shape, list) else next(iter(shape.values()))
    assert json.loads((workdir / "aggregate").read_text()) == [items[0]["value"]]


def test_view_draws_multiplicity_and_validate_rejects_route(scenario):
    config, workdir, nodes = scenario
    write_items(config, {"value": "test"})
    out = workdir / "view.html"
    result = CliRunner().invoke(cli, ["view", str(config), "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "×N" in out.read_text()
    nodes[0]["route"] = [{"then": "fail"}]
    config.write_text(yaml.safe_dump({"nodes": nodes}))
    result = CliRunner().invoke(cli, ["validate", str(config)])
    assert result.exit_code != 0
    assert "route" in result.output and "for_each" in result.output


def test_downstream_cannot_read_fields_directly_from_repeating_node(scenario):
    config, _, nodes = scenario
    write_items(config, {"value": "one"})
    nodes[1]["run"] = ["echo", "{{build.value}}"]
    config.write_text(yaml.safe_dump({"nodes": nodes}))
    result = CliRunner().invoke(cli, ["validate", str(config)])
    assert result.exit_code != 0
    assert "build" in result.output and "field" in result.output


def test_empty_manifest_runs_the_dependent_with_an_empty_array(scenario):
    config, workdir, _ = scenario
    write_items(config)
    run(config, workdir)
    assert json.loads((workdir / "aggregate").read_text()) == []
    assert not (workdir / "calls").exists()


def test_for_each_serializes_item_with_json_method_names(tmp_path):
    item = {"id": "first", "items": [{"keys": {"values": [1]}}]}
    # Whitespace distinguishes the producer's original text from reserialized JSON.
    raw = json.dumps(item, indent=2)
    (tmp_path / "items.json").write_text(json.dumps([item]))
    (tmp_path / "object.json").write_text('{"type":"object"}')
    workflow = tmp_path / "workflow.yaml"
    workflow.write_text(yaml.safe_dump({"nodes": [
        {"id": "plan", "run": ["printf", "%s", raw], "outputs": {"schema": "object.json"}},
        {"id": "check", "for_each": {"file": "items.json"}, "depends_on": ["plan"],
         "run": [sys.executable, "-c",
                 "import json,pathlib,sys; pathlib.Path('observed').write_text(json.dumps(sys.argv[1:]))",
                 "{{plan}}", "{{plan.items[0].keys.values[0]}}", "{{item|tojson}}",
                 "{{item.items[0].keys.values[0]}}"]},
    ]}))
    run(workflow, tmp_path)
    text, plan_field, encoded, item_field = json.loads((tmp_path / "observed").read_text())
    assert text == raw
    assert plan_field == item_field == "1"
    assert json.loads(encoded) == item


@pytest.mark.parametrize("suffix", ["branches", "branch_worktrees", "branch_map"])
def test_validate_rejects_removed_branch_bindings(scenario, suffix):
    config, _, nodes = scenario
    write_items(config, {"value": "one"})
    name = f"build_{suffix}"
    nodes[1]["run"] = ["echo", "{{" + name + "}}"]
    config.write_text(yaml.safe_dump({"nodes": nodes}))
    result = CliRunner().invoke(cli, ["validate", str(config)])
    assert result.exit_code != 0 and name in result.output


def test_repeated_eval_reads_current_child_by_authored_id(tmp_path):
    (tmp_path / "schema.json").write_text('{"type":"object","required":["label"]}')
    (tmp_path / "items.json").write_text('[{"id":"first","value":"alpha"},{"id":"second","value":"beta"}]')
    config = tmp_path / "workflow.yaml"
    config.write_text(yaml.safe_dump({"nodes": [{
        "id": "build", "for_each": {"file": "items.json"},
        "run": [sys.executable, "-c", "import json,sys; print(json.dumps({'label':sys.argv[1]}))", "{{item.value}}"],
        "outputs": {"schema": "schema.json"},
        "eval": {"run": [
            sys.executable, "-c", "import json,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))",
            "{{item.id}}", "{{build.label}}", "{{output}}", "{{item.value}}",
        ]},
    }]}))
    run(config, tmp_path)
    for name, value in (("first", "alpha"), ("second", "beta")):
        label, output, item = json.loads((tmp_path / name).read_text())
        assert label == item == value
        assert json.loads(output) == {"label": value}
