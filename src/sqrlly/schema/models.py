from __future__ import annotations

from collections.abc import Callable
from difflib import get_close_matches
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    PrivateAttr,
    ValidationInfo,
    model_validator,
)

from sqrlly.schema.references import assign_ancestors, validate_references
from sqrlly.schema.v1 import (
    KINDS, RESERVED, Agent, Case, Defaults, Eval, Name, Outputs, Prompt, Run, Workflow, WorkflowLoader, Worktree,
    repository_root, require_reference,
)




class KindStep(BaseModel):
    outputs: Outputs | None = None
    model_config = ConfigDict(extra="forbid")
    agent: Agent | None = None
    llm: Prompt | None = None
    run: Run | None = None
    workflow: Workflow | None = None

    eval: Eval | None = None
    route: list[Case] | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _validate_route(self) -> Self:
        if self.outputs:
            if self.llm and "files" in self.outputs.model_fields_set:
                raise ValueError("llm cannot declare outputs.files")
            if self.workflow and self.outputs.schema_file:
                raise ValueError("workflow cannot declare outputs.schema")
            if not self.kind:
                raise ValueError("outputs requires an executable kind")
        if self.eval and (self.eval.agent or self.eval.llm) and not self.route:
            kind = "agent" if self.eval.agent else "llm"
            raise ValueError(f"{kind} eval requires a route")
        if any(case.otherwise for case in (self.route or [])[:-1]):
            raise ValueError("else must be the last route case")
        return self

    @model_validator(mode="before")
    @classmethod
    def _count_kinds(cls, value: Any) -> Any:
        if isinstance(value, dict) and len(value.keys() & set(KINDS)) > 1:
            raise ValueError(f"choose at most one kind: {', '.join(KINDS)}")
        if isinstance(value, dict):
            for key in value:
                if key not in cls.model_fields:
                    closest = get_close_matches(str(key), cls.model_fields, n=1, cutoff=0)[0]
                    raise ValueError(f"unknown key {key!r}, closest legal key: {closest}")
                if key in KINDS and value[key] is None:
                    raise ValueError(f"{key} requires a value")
        return value

    @property
    def kind(self) -> str | None:
        return next((kind for kind in KINDS if getattr(self, kind) is not None), None)


class FanOutTemplate(KindStep):
    """Template for nodes spawned during fan-out over a manifest."""
    model_config = ConfigDict(extra="forbid")


class FanOutFinalNode(KindStep):
    """A node that runs after fan-out completes, consuming aggregate output."""
    model_config = ConfigDict(extra="forbid")
    id: Name
    name: Name = Field(default_factory=lambda data: data["id"])
    description: str | None = None


class FanOut(BaseModel):
    """Fan-out configuration: spawn N parallel instances over a manifest.

    The presence of the ``fan_out:`` block on a node IS the activation —
    there is no separate enable flag. To disable fan-out for a node,
    remove the block entirely. (The legacy ``enabled: bool`` field was
    removed in the post-Stage-5c audit because ``fan_out: { template:
    ... }`` with ``enabled: false`` was a silent author footgun.)
    """
    model_config = ConfigDict(extra="forbid")
    manifest_path: str | None = None
    template: FanOutTemplate | None = None
    final_nodes: list[FanOutFinalNode] = []


class Settings(BaseModel):
    _workflow_dir: Path | None = PrivateAttr(default=None)
    _preamble_path: Path | None = PrivateAttr(default=None)

    def preamble_path(self, base_dir: Path) -> Path | None:
        if self.preamble_file is None:
            return None
        return self._preamble_path or base_dir / self.preamble_file

    @property
    def workflow_dir(self) -> Path | None:
        return self._workflow_dir

    model: Name | None = None
    prompts: Name = "prompts/"
    defaults: Defaults = Defaults()
    model_config = ConfigDict(extra="forbid")

    # Run Claude with `--safe-mode` (cli transport only): operator
    # customizations — output styles, CLAUDE.md, skills, MCP, hooks — are
    # disabled, so workflow generation stays reproducible and free of e.g.
    # an "explanatory" output style prepending commentary into node output.
    # Default off: sqrlly never overrides operator settings unless asked.
    # The `--safe-mode` / `--no-safe-mode` CLI flag overrides this per run.
    safe_mode: bool = False
    default_timeout: float | None = None
    preamble_file: str | None = None
    retry_backoff: list[float] = []
    max_parallel_jobs: PositiveInt = 4
    # Memory back-pressure: block new dispatches while host memory
    # percent is ABOVE this value (`psutil.virtual_memory().percent`).
    # Default ``None`` = disabled. Composes (AND) with
    # ``max_parallel_jobs`` — every gate must allow dispatch. In-flight
    # jobs are never aborted; only new acquisitions wait.
    memory_threshold_pct: float | None = Field(default=None, ge=0.0, le=100.0)

    # Cross-node promote reconciliation when two same-wave promoting nodes
    # touch the same path. `warn`: log the overlap, last-write-wins
    # (run stays green). `fail`: abort before any write. `overwrite`: silent
    # last-write-wins. `skip`: the first promoting node (in `nodes` order)
    # keeps the path; later nodes drop it (their other paths still promote).
    on_promote_conflict: Literal["fail", "warn", "overwrite", "skip"] = "fail"
    # Fan-out `--resume` manifest-drift policy. On resume, a fan-out parent
    # re-reads its manifest and re-fans; if that manifest DROPS a prior branch
    # id (the dispatcher minted non-deterministic ids, e.g. uuid/counter), the
    # completed siblings silently vanish and the failed child is orphaned while
    # all N re-run. `fail` (default): halt the parent before any Send, naming
    # the vanished/orphaned ids — matches `on_promote_conflict: fail`. `warn`:
    # log and proceed with the new manifest (opt-in for an author who INTENDS a
    # changed manifest on resume). A stable-id (or purely additive) re-fan never
    # trips it. Only fires on `--resume` / `--resume-from` (not fresh / --entry /
    # --rerun-all).
    on_manifest_drift: Literal["fail", "warn"] = "fail"
    promote_exclude: list[str] = []
    worktree_share: list[str] = []
    # Rehydrate path: ordered shell commands run in each fresh worktree so the
    # package manager builds its own real node_modules there (e.g.
    # ["pnpm install --prefer-offline", "pnpm exec prisma generate"]). PM-agnostic.
    worktree_setup: list[str] = []


class Node(KindStep):
    _ancestors: list[str] = PrivateAttr(default_factory=list)
    id: Name
    name: Name = Field(default_factory=lambda data: data["id"])
    description: str | None = None
    depends_on: list[str] = []
    fan_out: FanOut | None = None
    timeout: float | None = None
    worktree: Worktree | None = None
    promote: bool | None = None

    @model_validator(mode="after")
    def _worktree_kind(self) -> Self:
        for field in ("worktree", "promote"):
            if field in self.model_fields_set:
                if self.kind not in ("agent", "run"):
                    raise ValueError(f"{field} only applies to agent and run")
                if getattr(self, field) is None:
                    raise ValueError(f"{field} requires a value")
        return self

    @model_validator(mode="after")
    def _fan_out_route_exclusive(self) -> "Node":
        # fan_out and route pick incompatible dispatchers: a gated fan-out
        # parent goes to _fan_<id> while route goes to _route_<id>, and an
        # ungated node fires BOTH static out-edges (double dispatch). To
        # branch after a fan-out, route from a downstream node instead.
        if self.fan_out is not None and self.route is not None:
            raise ValueError(
                f"Node {self.id!r} sets both fan_out and route — mutually "
                f"exclusive; route from a node downstream of the fan-out."
            )
        return self

    def effective_timeout(self, settings: Settings) -> float | None:
        if self.timeout is not None:
            return self.timeout
        if self.kind:
            timeout = getattr(settings.defaults, self.kind).timeout
            if timeout is not None:
                return timeout
        # The legacy dispatch deadline applies to each leaf inside a workflow.
        return None if self.workflow else settings.default_timeout

    def effective_worktree(self, settings: Settings, *, fan_child: bool = False) -> Worktree:
        if self.kind not in ("agent", "run"):
            return "off"
        default = getattr(settings.defaults, self.kind).worktree
        return self.worktree or default or ("isolated" if self.agent or fan_child else "off")

    def effective_promote(self, settings: Settings) -> bool:
        if self.kind not in ("agent", "run"):
            return False
        if self.promote is not None:
            return self.promote
        return getattr(settings.defaults, self.kind).promote is not False


class Graph(BaseModel):
    _source_path: Path | None = PrivateAttr(default=None)

    @property
    def source_path(self) -> Path | None:
        return self._source_path

    model_config = ConfigDict(extra="forbid")

    name: Name = "workflow"
    nodes: list[Node]
    settings: Settings = Settings()

    @property
    def workflow_dir(self) -> Path | None:
        return self.settings.workflow_dir

    @model_validator(mode="after")
    def _validate_steps(self, info: ValidationInfo) -> Self:
        context = info.context or {}
        path = context.get("workflow_path")
        if path and self.settings.preamble_file is not None:
            self.settings._preamble_path = path.parent.resolve() / self.settings.preamble_file
        if resolve_settings := context.get("resolve_settings"):
            self.settings = resolve_settings(self.settings)
        if path:
            self._source_path = path.resolve()
            self.settings._workflow_dir = path.parent.resolve()
            if "name" not in self.model_fields_set:
                self.name = path.stem
        directory = self.workflow_dir
        root = repository_root(directory) if directory else None
        if directory and (preamble := self.settings.preamble_path(directory)) is not None:
            require_reference(root, preamble, "settings.preamble_file")
        defaults = self.settings.defaults.agent
        if isinstance(defaults.tools, list) and defaults.provider != "anthropic":
            raise ValueError("agent tool-name lists require provider: anthropic")
        node_ids = {node.id for node in self.nodes}
        for node in self.nodes:
            steps: list[KindStep] = [node]
            if node.fan_out:
                if node.fan_out.template:
                    steps.append(node.fan_out.template)
                steps.extend(node.fan_out.final_nodes)
            for step in steps:
                if isinstance(step, (Node, FanOutFinalNode)) and step.id in RESERVED:
                    raise ValueError(f"reserved node id: {step.id}")
                for payload in (step, step.eval):
                    if payload is None:
                        continue
                    if payload.agent:
                        agent = payload.agent
                        tools = defaults.tools if agent.tools is None else agent.tools
                        if isinstance(tools, list) and (agent.provider or defaults.provider) != "anthropic":
                            raise ValueError(f"{node.id}: agent tool-name lists require provider: anthropic")
                    prompt = payload.agent or payload.llm
                    if directory and prompt and prompt.prompt:
                        require_reference(root, directory / self.settings.prompts / prompt.prompt,
                                          f"{node.id}.prompt", ' (for inline prompts use {text: "..."})')
                    if directory and payload.outputs and payload.outputs.schema_file:
                        schema_path = directory / payload.outputs.schema_file
                        label = f"{node.id}.eval" if payload is step.eval else node.id
                        require_reference(root, schema_path, f"{label}.outputs.schema")
                        payload.outputs.load_schema(schema_path, root or directory)
                if directory and step.workflow:
                    require_reference(root, directory / step.workflow.file, f"{node.id}.workflow",
                                      ' ({text: "..."} is for inline agent or llm prompts)')
                for case in step.route or []:
                    for target in case.targets:
                        if target not in node_ids and target not in RESERVED:
                            raise ValueError(f"{node.id}.route references unknown node {target}")
        return self

    @model_validator(mode="after")
    def _validate_unique_ids(self) -> Self:
        seen: set[str] = set()
        for n in self.nodes:
            if n.id in seen:
                raise ValueError(f"Duplicate node id: {n.id}")
            seen.add(n.id)
        return self

    @model_validator(mode="after")
    def _validate_depends_on(self) -> Self:
        node_ids = {n.id for n in self.nodes}
        for node in self.nodes:
            for dep in node.depends_on:
                if dep == node.id:
                    raise ValueError(f"Node '{node.id}' has a self-dependency")
                if dep not in node_ids:
                    raise ValueError(
                        f"Node '{node.id}' depends on '{dep}' "
                        f"which references nonexistent node"
                    )
        assign_ancestors(self)
        return self



def load_workflow(
    path: str | Path, *, resolve_settings: Callable[[Settings], Settings] | None = None,
    inputs: frozenset[str] | None = None,
) -> Graph:
    path = Path(path)
    with path.open(encoding="utf-8") as stream:
        raw = yaml.load(stream, Loader=WorkflowLoader)
    graph = Graph.model_validate(raw, context={"workflow_path": path, "resolve_settings": resolve_settings})
    validate_references(graph, inputs)
    return graph
