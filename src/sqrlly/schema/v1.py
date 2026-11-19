"""Authoring models for the 1.0 grammar spike."""

import re
import shlex
from difflib import get_close_matches
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel, ConfigDict, Field, NonNegativeInt, PositiveFloat, PositiveInt,
    ValidationInfo, field_validator, model_validator,
)

KINDS = ("agent", "llm", "run", "workflow")
RESERVED = {"retry", "fail"}
Name = Annotated[str, Field(min_length=1)]
Provider = Literal["openai", "anthropic"]
Tools = Literal["read", "edit", "full"] | list[Name]
Worktree = Literal["isolated", "off"]
Destination = Name | Annotated[list[Name], Field(min_length=1)]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @model_validator(mode="before")
    @classmethod
    def _check_keys(cls, value: Any) -> Any:
        if isinstance(value, dict):
            keys = [f.alias or name for name, f in cls.model_fields.items()]
            for key in value:
                if key not in keys:
                    closest = get_close_matches(str(key), keys, n=1, cutoff=0)[0]
                    raise ValueError(f"unknown key {key!r}, closest legal key: {closest}")
                if value[key] is None:
                    raise ValueError(f"{key} cannot be null")
        return value


class Prompt(Record):
    prompt: Name | None = None
    text: Name | None = None
    model: Name | None = None
    provider: Provider | None = None

    @model_validator(mode="before")
    @classmethod
    def _scalar(cls, value: Any) -> Any:
        return {"prompt": value} if isinstance(value, str) else value

    @model_validator(mode="after")
    def _source(self) -> Self:
        if (self.prompt is None) == (self.text is None):
            raise ValueError("choose exactly one of prompt and text")
        return self


class Agent(Prompt):
    tools: Tools | None = None
    cli_args: list[str] = []


class Run(Record):
    cmd: Annotated[list[str], Field(min_length=1)]
    env: dict[str, str] = {}

    @model_validator(mode="before")
    @classmethod
    def _command(cls, value: Any) -> Any:
        if isinstance(value, (str, list)):
            value = {"cmd": value}
        if isinstance(value, dict) and isinstance(cmd := value.get("cmd"), str):
            if any(token in cmd for token in ("{{", "&&", "|", ";", ">", "$")):
                raise ValueError('use argv list form: run: [echo, "{{ plan.title }}"]')
            value = {**value, "cmd": shlex.split(cmd)}
        return value

    @field_validator("cmd")
    @classmethod
    def _executable(cls, value: list[str]) -> list[str]:
        if not value[0]:
            raise ValueError("cmd requires a nonempty executable")
        return value


class File(Record):
    file: Name


class Workflow(File):
    inputs: dict[str, Any] = Field(default_factory=dict, alias="with")

    @model_validator(mode="before")
    @classmethod
    def _scalar(cls, value: Any) -> Any:
        return {"file": value} if isinstance(value, str) else value


def repository_root(directory: Path) -> Path | None:
    # Linked worktrees have a .git file at their own root.
    return next((p for p in (directory, *directory.parents) if (p / ".git").exists()), None)


def require_reference(root: Path | None, path: Path, key: str, hint: str = "") -> None:
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as err:
        raise ValueError(f"{key}: cannot resolve {path}: {err}") from err
    if root is not None and not resolved.is_relative_to(root):
        raise ValueError(f"{key}: {path} resolves outside repository {root}")
    if not resolved.is_file():
        raise ValueError(f"{key}: file not found: {path}{hint}")


class SchemaOutput(Record):
    schema_file: Name | None = Field(None, alias="schema")


class Outputs(SchemaOutput):
    files: list[Name] = []


class Eval(Record):
    run: Run | None = None
    llm: Prompt | None = None
    agent: Agent | None = None
    model: Name | None = None
    timeout: PositiveFloat | None = None
    outputs: SchemaOutput | None = None

    @model_validator(mode="after")
    def _kind(self) -> Self:
        if len(self.model_fields_set.intersection(KINDS)) != 1:
            raise ValueError("eval requires exactly one of run, llm, agent")
        if self.run and self.model:
            raise ValueError("a run eval cannot set model")
        return self


class Case(Record):
    when: Name | None = None
    then: Destination | None = None
    otherwise: Destination | None = Field(None, alias="else")
    max_attempts: PositiveInt | None = None
    context: str = "last"

    @model_validator(mode="after")
    def _destination(self) -> Self:
        if (self.then is None) == (self.otherwise is None):
            raise ValueError("route case requires exactly one of then or else")
        if self.otherwise and self.when:
            raise ValueError("else cannot have a when predicate")
        targets = self.then or self.otherwise
        if isinstance(targets, list) and RESERVED.intersection(targets):
            raise ValueError("retry and fail must be single route destinations")
        return self


class Node(Record):
    id: Name
    name: Name | None = None
    agent: Agent | None = None
    llm: Prompt | None = None
    run: Run | None = None
    workflow: Workflow | None = None
    depends_on: list[Name] = []
    timeout: PositiveFloat | None = None
    eval: Eval | None = None
    for_each: Name | File | None = None
    route: Annotated[list[Case], Field(min_length=1)] | None = None
    outputs: Outputs | None = None
    worktree: Worktree | None = None
    promote: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _count_kinds(cls, value: Any) -> Any:
        if isinstance(value, dict) and len(value.keys() & set(KINDS)) > 1:
            raise ValueError(f"choose at most one kind: {', '.join(KINDS)}")
        return value

    @model_validator(mode="after")
    def _envelope(self) -> Self:
        if self.name is None:
            self.name = self.id
        if self.id in RESERVED:
            raise ValueError(f"reserved node id: {self.id}")
        if self.for_each and self.route:
            raise ValueError("for_each and route cannot appear together")
        if self.llm or self.workflow:
            for key in ("worktree", "promote"):
                if key in self.model_fields_set:
                    raise ValueError(f"{key} only applies to agent and run")
        if self.outputs:
            if self.llm and "files" in self.outputs.model_fields_set:
                raise ValueError("llm cannot declare outputs.files")
            if self.workflow and self.outputs.schema_file:
                raise ValueError("workflow cannot declare outputs.schema")
        if self.eval and (self.eval.llm or self.eval.agent) and not self.route:
            kind = "llm" if self.eval.llm else "agent"
            raise ValueError(f"an {kind} eval requires a route")
        if any(case.otherwise for case in (self.route or [])[:-1]):
            raise ValueError("else must be the last route case")
        return self


class CommonDefaults(Record):
    timeout: PositiveFloat | None = None


class RunDefaults(CommonDefaults):
    backend_max_retries: NonNegativeInt = 0
    worktree: Worktree | None = None
    promote: bool | None = None


class LlmDefaults(CommonDefaults):
    model: Name | None = None
    provider: Provider = "openai"
    backend_max_retries: NonNegativeInt = 2


class AgentDefaults(LlmDefaults):
    tools: Tools = "edit"
    cli_args: list[str] = []
    worktree: Worktree | None = None
    promote: bool | None = None


class Defaults(Record):
    agent: AgentDefaults = AgentDefaults()
    llm: LlmDefaults = LlmDefaults()
    run: RunDefaults = RunDefaults()
    workflow: CommonDefaults = CommonDefaults()


class Settings(Record):
    prompts: Name = "prompts/"
    model: Name | None = None
    defaults: Defaults = Defaults()
    max_parallel_jobs: PositiveInt = 4
    on_promote_conflict: Literal["fail", "warn", "overwrite", "skip"] = "fail"
    on_manifest_drift: Literal["fail", "warn"] = "fail"
    promote_exclude: list[Name] = []
    worktree_setup: list[Name] = []
    worktree_share: list[Name] = []


class Graph(Record):
    name: Name | None = None
    nodes: Annotated[list[Node], Field(min_length=1)]
    settings: Settings = Settings()

    @model_validator(mode="after")
    def _validate_graph(self, info: ValidationInfo) -> Self:
        ids = {node.id for node in self.nodes}
        if len(ids) != len(self.nodes):
            raise ValueError("duplicate node id")
        defaults = self.settings.defaults.agent
        if isinstance(defaults.tools, list) and defaults.provider != "anthropic":
            raise ValueError("agent tool-name lists require provider: anthropic")
        for node in self.nodes:
            for target in node.depends_on:
                if target not in ids:
                    raise ValueError(f"{node.id}.depends_on references unknown node {target}")
            for case in node.route or []:
                targets = case.then or case.otherwise
                for target in targets if isinstance(targets, list) else [targets]:
                    if target not in ids and target not in RESERVED:
                        raise ValueError(f"{node.id}.route references unknown node {target}")
            for step in (node, node.eval):
                if step and step.agent:
                    agent = step.agent
                    tools = defaults.tools if agent.tools is None else agent.tools
                    if isinstance(tools, list) and (agent.provider or defaults.provider) != "anthropic":
                        raise ValueError(f"{node.id}: agent tool-name lists require provider: anthropic")
        try:
            TopologicalSorter({node.id: node.depends_on for node in self.nodes}).prepare()
        except CycleError as err:
            raise ValueError(f"depends_on cycle: {err.args[1]}") from err
        if info.context and "workflow_dir" in info.context:
            self._check_files(Path(info.context["workflow_dir"]))
        return self

    def _check_files(self, directory: Path) -> None:
        directory = directory.resolve()
        root = repository_root(directory)

        for node in self.nodes:
            for step in (node, node.eval):
                if step is None:
                    continue
                prompt = step.agent or step.llm
                if prompt and prompt.prompt:
                    require_reference(root, directory / self.settings.prompts / prompt.prompt,
                            f"{node.id}.prompt", ' (for inline prompts use {text: "..."})')
                if step.outputs and step.outputs.schema_file:
                    require_reference(root, directory / step.outputs.schema_file, f"{node.id}.outputs.schema")
            if node.workflow:
                require_reference(root, directory / node.workflow.file, f"{node.id}.workflow",
                        ' ({text: "..."} is for inline agent or llm prompts)')
            if isinstance(node.for_each, File):
                require_reference(root, directory / node.for_each.file, f"{node.id}.for_each.file")


class WorkflowLoader(yaml.SafeLoader):
    """Keep YAML words such as off and yes available as argv tokens and names."""

    yaml_implicit_resolvers = {
        key: [rule for rule in rules if rule[0] != "tag:yaml.org,2002:bool"]
        for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.IGNORECASE), list("tTfF"),
)


def load_workflow(path: str | Path) -> Graph:
    path = Path(path)
    with path.open(encoding="utf-8") as stream:
        raw = yaml.load(stream, Loader=WorkflowLoader)
    return Graph.model_validate(raw, context={"workflow_dir": path.parent})
