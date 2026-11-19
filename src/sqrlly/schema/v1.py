"""Payloads shared by workflow nodes, evals and settings."""

import re
import shlex
from difflib import get_close_matches
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from sqrlly.schema.json_schema import load_validator
import yaml
from pydantic import (
    BaseModel, ConfigDict, Field, NonNegativeInt, PositiveFloat, PositiveInt, PrivateAttr,
    field_validator, model_validator,
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
    _schema_document: dict | bool | None = PrivateAttr(default=None)

    @property
    def schema_document(self) -> dict | bool | None:
        return self._schema_document

    _validator: Any = PrivateAttr(default=None)

    def load_schema(self, path: Path, root: Path | None = None) -> None:
        self._schema_document, self._validator = load_validator(
            path, root or repository_root(path.parent.resolve()) or path.parent,
        )

    def validate_value(self, value: Any) -> None:
        self._validator.validate(value)


class Outputs(SchemaOutput):
    files: list[Name] = []

    @field_validator("files")
    @classmethod
    def _relative_files(cls, patterns: list[str]) -> list[str]:
        for pattern in patterns:
            path = Path(pattern)
            if path.is_absolute() or ".." in path.parts or {".git", ".sqrlly"}.intersection(path.parts):
                raise ValueError(f"outputs.files requires a path inside the node tree: {pattern}")
        return patterns


class Eval(Record):
    run: Run | None = None
    llm: Prompt | None = None
    agent: Agent | None = None
    model: Name | None = None
    timeout: PositiveFloat | None = None
    outputs: Outputs | None = None

    @field_validator("outputs")
    @classmethod
    def _schema_only(cls, value):
        if value and "files" in value.model_fields_set:
            raise ValueError("eval cannot declare outputs.files")
        return value

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

    @property
    def targets(self) -> list[str]:
        target = self.then if self.then is not None else self.otherwise
        assert target is not None
        return [target] if isinstance(target, str) else target

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


class WorkflowLoader(yaml.SafeLoader):
    """Keep YAML words such as off and yes available as argv tokens and names."""

    yaml_implicit_resolvers = {
        key: [rule for rule in rules if rule[0] != "tag:yaml.org,2002:bool"]
        for key, rules in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }


WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.IGNORECASE), list("tTfF"),
)
