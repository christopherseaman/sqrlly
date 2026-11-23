import subprocess

import pytest
from pydantic import ValidationError

from sqrlly.schema.models import Graph, Node, Settings, load_workflow


def test_kind_selection():
    assert Node(id="a").kind is None
    assert Node(id="a", agent="a.md").agent.prompt == "a.md"
    assert Node(id="a", llm={"text": "Hello"}).llm.text == "Hello"
    assert Node(id="a", run='echo "a b"').run.cmd == ["echo", "a b"]
    workflow = Node(id="a", workflow={"file": "b.yaml", "with": {"name": "Alex"}})
    assert workflow.workflow.inputs == {"name": "Alex"}
    with pytest.raises(ValidationError, match="agent, llm, run, workflow"):
        Node(id="a", run="pwd", workflow="a.yaml")
    with pytest.raises(ValidationError, match="execute"):
        Node(id="a", execute={"url": "a.md"})


@pytest.mark.parametrize("run", ["echo {{ a }}", "echo hi | cat", [], {"cmd": [""]}, {"cmd": "pwd", "env": {"X": 2}}])
def test_invalid_run(run):
    with pytest.raises(ValidationError):
        Node(id="a", run=run)


@pytest.mark.parametrize("key", ["presets", "base_url", "allow_remote_urls", "allowed_url_hosts", "url_headers", "max_remote_fetch_bytes"])
def test_removed_settings(key):
    with pytest.raises(ValidationError, match=key):
        Settings.model_validate({key: {}})


def test_repeating_files_resolve_at_load(tmp_path, monkeypatch):
    path = tmp_path / "workflow.yaml"
    path.write_text("settings: {prompts: text}\nnodes:\n"
                    "- {id: batch, agent: task.md, for_each: {file: items.json}}\n"
                    "- {id: end, depends_on: [batch], workflow: end.yaml}\n")
    monkeypatch.chdir(tmp_path.parent)
    with pytest.raises(ValidationError, match="items.json"):
        load_workflow(path)
    (tmp_path / "items.json").write_text("[]")
    with pytest.raises(ValidationError, match="task.md"):
        load_workflow(path)
    (tmp_path / "text").mkdir()
    (tmp_path / "text/task.md").write_text("{{item.title}}")
    with pytest.raises(ValidationError, match="end.yaml"):
        load_workflow(path)
    (tmp_path / "end.yaml").write_text("nodes: [{id: end}]")
    graph = load_workflow(path)
    assert graph.name == "workflow"
    assert graph.workflow_dir == tmp_path
    assert graph.settings.workflow_dir == tmp_path
    assert graph.nodes[0].for_each.file == "items.json"
    assert graph.nodes[1].workflow.file == "end.yaml"


def test_per_kind_defaults_and_node_overrides():
    settings = Settings(defaults={"run": {"worktree": "off", "timeout": 10, "promote": True}})
    node = Node(id="a", run="pwd")
    assert node.effective_worktree(settings) == "off"
    assert node.effective_timeout(settings) == 10
    assert node.effective_promote(settings) is True
    assert Node(id="a", run="pwd", promote=False).effective_promote(settings) is False


def test_named_tools_follow_effective_provider_on_repeating_agent():
    nodes = [{"id": "fan", "for_each": {"file": "items.json"}, "agent": {"text": "hi", "tools": ["Read"]}}]
    with pytest.raises(ValidationError, match="anthropic"):
        Graph(nodes=nodes)
    graph = Graph(nodes=nodes, settings={"defaults": {"agent": {"provider": "anthropic"}}})
    assert graph.nodes[0].agent.tools == ["Read"]


def test_child_uses_inherited_settings_during_validation(tmp_path):
    path = tmp_path / "child.yaml"
    path.write_text("nodes: [{id: a, agent: {prompt: review.md, tools: [Read]}}]")
    (tmp_path / "text").mkdir()
    (tmp_path / "text/review.md").write_text("review")
    settings = Settings(prompts="text", defaults={"agent": {"provider": "anthropic"}})
    graph = load_workflow(path, resolve_settings=lambda child: settings.model_copy())
    assert graph.name == "child"
    assert graph.settings.prompts == "text"
    assert graph.workflow_dir == tmp_path
    assert settings.workflow_dir is None


def test_authored_workflow_version_rejected(tmp_path):
    path = tmp_path / "flow.yaml"
    path.write_text('version: "1.0"\nnodes: [{id: one}]\n')
    with pytest.raises(ValidationError, match="version"):
        load_workflow(path)


@pytest.mark.parametrize("in_repository", [False, True])
def test_preamble_resolves_relative_to_declaring_workflow(tmp_path, in_repository):
    from sqrlly.runtime.executor.prompt import apply_preamble
    from sqrlly.runtime.settings_merge import merge_settings

    repo = tmp_path / "repo"
    child = repo / "child"
    child.mkdir(parents=True)
    if in_repository:
        subprocess.run(["git", "init", "-q", repo], check=True)
    (repo / "preamble.md").write_text("Parent")
    parent_path = repo / "workflow.yaml"
    parent_path.write_text("settings: {preamble_file: preamble.md}\nnodes: [{id: a}]")
    settings = load_workflow(parent_path).settings

    child_path = child / "workflow.yaml"
    child_path.write_text('nodes: [{id: a, llm: {text: Hello}}]')

    def load_child():
        return load_workflow(child_path, resolve_settings=lambda local: merge_settings(settings, local))

    graph = load_child()
    assert apply_preamble("Hello", settings=graph.settings, base_workdir=str(child)) == "Parent\n\nHello"
    child_path.write_text("settings: {preamble_file: preamble.md}\nnodes: [{id: a}]")
    with pytest.raises(ValidationError, match="settings.preamble_file: file not found"):
        load_child()

    preamble = child / "preamble.md"
    if in_repository:
        outside = tmp_path / "outside.md"
        outside.write_text("External")
        preamble.symlink_to(outside)
        with pytest.raises(ValidationError, match="settings.preamble_file: .*outside repository"):
            load_child()
        preamble.unlink()
    preamble.write_text("Child")
    graph = load_child()
    result = apply_preamble("Hello", settings=graph.settings, base_workdir=str(graph.workflow_dir))
    assert result == "Child\n\nHello"


@pytest.mark.parametrize("limit", [0, -1])
def test_parallel_jobs_requires_positive_limit(limit):
    with pytest.raises(ValidationError, match="max_parallel_jobs"):
        Settings(max_parallel_jobs=limit)
    assert Settings(max_parallel_jobs=1).max_parallel_jobs == 1
