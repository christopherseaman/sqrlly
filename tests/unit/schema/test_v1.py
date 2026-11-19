import subprocess

import pytest
import yaml
from pydantic import ValidationError

from sqrlly.schema.models import load_workflow


def write_workflow(directory, nodes, filename="flow.yaml", **fields):
    path = directory / filename
    path.write_text(yaml.safe_dump({**fields, "nodes": nodes}))
    return path


def diagnostics(error):
    return "\n".join(
        f"{'.'.join(map(str, detail['loc']))} {detail['msg']}"
        for detail in error.errors()
    )


@pytest.mark.parametrize(("kind", "field", "value"), [
    ("llm", "outputs.files", ["report.txt"]),
    ("llm", "worktree", "isolated"),
    ("llm", "promote", False),
    ("workflow", "outputs.schema", "result.json"),
    ("workflow", "worktree", "off"),
    ("workflow", "promote", True),
])
def test_matrix_rejects_field_on_kind(tmp_path, kind, field, value):
    write_workflow(tmp_path, [{"id": "child", "run": "pwd"}], "child.yaml")
    (tmp_path / "result.json").write_text('{"type":"object"}')
    node = {"id": "measure", kind: {"text": "Count."} if kind == "llm" else "child.yaml"}
    if field.startswith("outputs."):
        node["outputs"] = {field.split(".")[1]: value}
    else:
        node[field] = value

    with pytest.raises(ValidationError) as exc:
        load_workflow(write_workflow(tmp_path, [node]))
    assert field in diagnostics(exc.value)


def test_conflicting_kinds_name_all_choices_once(tmp_path):
    node = {"id": "a", "run": "echo hello", "agent": {"text": "Hello"}}
    with pytest.raises(ValidationError) as exc:
        load_workflow(write_workflow(tmp_path, [node]))
    errors = exc.value.errors()
    assert len(errors) == 1
    assert all(kind in errors[0]["msg"] for kind in ("agent", "llm", "run", "workflow"))


@pytest.mark.parametrize(("node", "key"), [
    ({"id": "retry"}, "retry"),
    ({"id": "fail"}, "fail"),
    ({"llm": {"text": "Count."}, "eval": {"llm": {"text": "Score."}}}, "route"),
    ({"run": "date", "eval": {"agent": {"text": "Inspect."}}}, "route"),
    ({"route": [{"else": "b"}, {"then": "b"}]}, "else"),
    ({"run": "date", "for_each": {"file": "items.json"}, "route": [{"then": "b"}]}, "route"),
    ({"run": "echo {{b}}"}, "run"),
    ({"run": "echo hello | wc"}, "run"),
    ({"agnet": {"text": "Count."}}, "agent"),
    ({"depends_on": ["missing"]}, "missing"),
    ({"route": [{"then": "missing"}]}, "missing"),
])
def test_invalid_node_reports_the_problem(tmp_path, node, key):
    path = write_workflow(tmp_path, [{"id": "a", **node}, {"id": "b"}])
    with pytest.raises(ValidationError) as exc:
        load_workflow(path)
    assert key in diagnostics(exc.value)


def test_loads_relative_files_and_inline_forms_from_another_cwd(tmp_path, monkeypatch):
    folder = tmp_path / "workflow"
    prompts = folder / "templates"
    prompts.mkdir(parents=True)
    (prompts / "task.md").write_text("Suggest a title.")
    (folder / "score.json").write_text('{"type":"object"}')
    write_workflow(folder, [{"id": "version", "run": "git --version"}], "child.yaml")
    nodes = [
        {"id": "draft", "agent": "task.md", "outputs": {"files": ["draft.txt"]}},
        {"id": "score", "llm": {"text": "Score {{draft}}."}, "depends_on": ["draft"],
         "outputs": {"schema": "score.json"}},
        {"id": "decide", "depends_on": ["score"], "route": [
            {"when": "score.value > 2", "then": "publish"}, {"else": "fail"}]},
        {"id": "publish", "workflow": {"file": "child.yaml"}},
    ]
    path = write_workflow(folder, nodes, settings={"prompts": "templates"})
    monkeypatch.chdir(tmp_path)
    graph = load_workflow(path)
    assert [node.id for node in graph.nodes] == ["draft", "score", "decide", "publish"]
    assert graph.nodes[1].depends_on == ["draft"]


def test_missing_prompt_identifies_inline_alternative(tmp_path):
    path = write_workflow(tmp_path, [{"id": "draft", "agent": "missing.md"}])
    with pytest.raises(ValidationError) as exc:
        load_workflow(path)
    message = diagnostics(exc.value)
    assert "missing.md" in message and "text" in message


def test_run_scalar_preserves_quoted_argument(tmp_path):
    path = write_workflow(tmp_path, [{"id": "show", "run": "echo 'two words'"}])
    assert load_workflow(path).nodes[0].run.cmd == ["echo", "two words"]


def test_file_reference_resolves_within_its_worktree(tmp_path):
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", repo], check=True)
    folder = repo / "flows"
    folder.mkdir()
    shared = repo / "schema.json"
    shared.write_text('{"type":"number"}')
    node = {"id": "count", "run": "echo 2", "outputs": {"schema": "../schema.json"}}
    path = write_workflow(folder, [node])
    assert load_workflow(path).nodes[0].outputs.schema_file == "../schema.json"

    external = tmp_path / "external.json"
    shared.rename(external)
    shared.symlink_to(external)
    with pytest.raises(ValidationError) as exc:
        load_workflow(path)
    assert "outside repository" in diagnostics(exc.value)


def test_yaml_off_is_a_worktree_mode_and_false_is_a_boolean(tmp_path):
    path = tmp_path / "flow.yaml"
    path.write_text("nodes: [{id: edit, run: pwd, worktree: off, promote: false}]")
    node = load_workflow(path).nodes[0]
    assert (node.name, node.worktree, node.promote) == ("edit", "off", False)


def test_named_tools_follow_the_effective_provider(tmp_path):
    settings = {"defaults": {"agent": {"provider": "anthropic", "tools": ["Read"]}}}
    agent = {"text": "Read the notes."}
    nodes = [{"id": "review", "agent": agent}]
    path = write_workflow(tmp_path, nodes, settings=settings)
    assert load_workflow(path).settings.defaults.agent.tools == ["Read"]

    agent["provider"] = "openai"
    with pytest.raises(ValidationError) as exc:
        load_workflow(write_workflow(tmp_path, nodes, settings=settings))
    assert "anthropic" in diagnostics(exc.value)
