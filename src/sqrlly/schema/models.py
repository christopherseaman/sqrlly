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
    field_validator,
    model_validator,
)

from sqrlly.schema.v1 import (
    KINDS, Agent, Defaults, Name, Prompt, Run, Workflow, WorkflowLoader,
    repository_root, require_reference,
)


# Worktree isolation modes. `none` is an accepted alias for `off`.
# This field is a MODE selector — a group name belongs in `worktree_group`,
# not here; non-mode tokens are rejected to catch misplaced group names.
_WORKTREE_MODES = ("auto", "isolated", "off")


def _normalize_worktree(value: Any) -> Any:
    """Normalize a worktree-mode value: ``None`` passes through (inherit);
    ``"none"`` aliases to ``"off"``; one of ``auto``/``isolated``/``off``
    is accepted; anything else raises."""
    if value is None:
        return None
    # Bare `worktree: off`/`on` parse as YAML booleans (off/no/false →
    # False, on/yes/true → True). Map the toggle to the isolation modes
    # so the ergonomic `worktree: off` opt-out works unquoted.
    if isinstance(value, bool):
        return "isolated" if value else "off"
    if not isinstance(value, str):
        raise ValueError(
            f"worktree must be a string, got {type(value).__name__}"
        )
    mode = value.strip().lower()
    if mode == "none":
        return "off"
    if mode not in _WORKTREE_MODES:
        raise ValueError(
            f"worktree must be one of auto/isolated/off "
            f"(named shared-worktree groups are not yet supported), "
            f"got {value!r}"
        )
    return mode


def _check_worktree_group_exclusive(
    worktree: str | None, worktree_group: str | None
) -> None:
    """A ``worktree_group`` already implies a shared tree, so pairing it with
    an explicit ``isolated``/``off`` mode is contradictory. Shared by the
    Settings and Node after-validators."""
    if worktree_group is not None and worktree in ("isolated", "off"):
        raise ValueError(
            "worktree_group cannot be combined with worktree="
            f"{worktree!r}: a group already implies a shared tree "
            "(omit worktree, or set it to auto)"
        )


class RouteCase(BaseModel):
    """One case in a route ladder.

    `goto` accepts a single target id or a list (static fan-out via
    `Command(goto=[...])`). `include_eval` opts the goto target into
    receiving the same neutral eval-result preamble that retry
    attempts get auto-prepended to their prompt. Default off — success
    paths typically perform a different task than the previous node,
    so the previous eval's feedback is noise unless explicitly wanted.
    """
    model_config = ConfigDict(extra="forbid")
    when: str
    goto: str | list[str]
    include_eval: bool = False


class RouteElse(BaseModel):
    """Structured else: target — same fields as RouteCase minus when:."""
    model_config = ConfigDict(extra="forbid")
    goto: str | list[str]
    include_eval: bool = False


class Route(BaseModel):
    """Forward-edge dispatch on a node — Stage 5c inline routing.

    Exactly one shape is set:

    - **Unconditional shorthand**: `goto:` (str | list[str]) plus
      optional `include_eval:`. Compiles to `Command(goto=...)` from
      the source node, no predicate evaluation.
    - **Conditional ladder**: `cases:` (first-match-wins) plus
      `else:`. `else:` may be a bare string/list (auto-normalized to
      `RouteElse(goto=..., include_eval=False)`) or a structured
      RouteElse for `include_eval` control.

    Lives on `Node.route`. When the source node also has `execute:`,
    the route fires after execute (and after eval, if present)
    settles. When `execute:` is omitted, the node is a standalone
    router.
    """
    model_config = ConfigDict(populate_by_name=True)
    # Unconditional shorthand:
    goto: str | list[str] | None = None
    include_eval: bool = False
    # OR conditional ladder:
    cases: list[RouteCase] = []
    else_: RouteElse | None = Field(default=None, alias="else")

    @model_validator(mode="before")
    @classmethod
    def _normalize_else(cls, data: Any) -> Any:
        # Allow `else: <string>` and `else: [a, b]` shorthand by
        # promoting to RouteElse(goto=..., include_eval=False).
        if not isinstance(data, dict):
            return data
        else_val = data.get("else") if "else" in data else data.get("else_")
        if isinstance(else_val, (str, list)):
            promoted = {"goto": else_val, "include_eval": False}
            if "else" in data:
                data = {**data, "else": promoted}
            else:
                data = {**data, "else_": promoted}
        return data

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        has_goto = self.goto is not None
        has_cases = bool(self.cases)
        has_else = self.else_ is not None
        if has_goto:
            if has_cases or has_else:
                raise ValueError(
                    "Route shorthand `goto:` is mutually exclusive with "
                    "`cases:`/`else:` (got both)"
                )
        else:
            if not has_cases and not has_else:
                raise ValueError(
                    "Route requires either `goto:` (unconditional) "
                    "or `cases:` + `else:` (conditional ladder)"
                )
            if has_cases and not has_else:
                raise ValueError(
                    "Route with `cases:` requires `else:` target"
                )
            if has_else and not has_cases:
                # `else:` without `cases:` would be a silent unconditional
                # redirect — confusing structure, identical to `goto:`
                # shorthand. Reject so authors pick the unambiguous form.
                raise ValueError(
                    "Route with `else:` requires `cases:` — use `goto:` "
                    "shorthand for unconditional dispatch"
                )
            if self.include_eval:
                # include_eval at the route level only meaningful for
                # the goto-shorthand form; cases/else carry their own.
                raise ValueError(
                    "`include_eval` at the route level is only valid "
                    "with `goto:` shorthand; for `cases:` ladders, set "
                    "include_eval per-case (and on the else target)"
                )
        return self


class DimensionCheck(BaseModel):
    # `threshold` matches `Evaluation.threshold`; `min` stays accepted
    # as a YAML alias for back-compat with pre-rename workflows.
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    field: str
    threshold: float = Field(ge=0.0, le=1.0, alias="min")


class Evaluation(BaseModel):
    """Evaluation configuration for a node."""
    model_config = ConfigDict(extra="forbid")
    validator: str
    threshold: float = Field(ge=0.0, le=1.0, default=0.0)
    blocking: bool = False
    max_retries: int | None = None
    model: str | None = None
    dimensions: list[DimensionCheck] | None = None


class OutputContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_directory: str
    required_files: list[str] = []

    def required_paths(self) -> list[str]:
        """Required files as workdir-relative POSIX paths, with
        ``base_directory`` prepended. A single derivation so every consumer
        constructs these paths identically (used by the existence check in
        ``gates.validate_output_contract``). A ``base_directory`` of ``"."``
        collapses to the bare filename."""
        return [(Path(self.base_directory) / f).as_posix()
                for f in self.required_files]


class KindStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent: Agent | None = None
    llm: Prompt | None = None
    run: Run | None = None
    workflow: Workflow | None = None

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
    evaluation: Evaluation | None = None
    # Per-fan-out worktree-isolation override. None (default) inherits
    # settings.worktree — same optional-override semantics as Node.worktree.
    # Lets one workflow run an isolated build fan-out AND a shared-base
    # planner fan-out under a single top-level settings.worktree: the value
    # here overrides settings.worktree at the per-branch isolation gate
    # (subgraph templates: make_fan_out_subgraph_invoker; non-subgraph
    # templates: the synthetic child node's effective_worktree).
    worktree: Literal["auto", "isolated", "off"] | None = None

    @field_validator("worktree", mode="before")
    @classmethod
    def _normalize_worktree_override(cls, v: Any) -> Any:
        return _normalize_worktree(v)


class FanOutFinalNode(KindStep):
    """A node that runs after fan-out completes, consuming aggregate output."""
    model_config = ConfigDict(extra="forbid")
    id: Name
    name: Name = Field(default_factory=lambda data: data["id"])
    description: str | None = None
    evaluation: Evaluation | None = None
    # Enforced via the standard execution-node path (the final node runs
    # through `_make_execution_node`), so it scaffolds + validates just
    # like a top-level node's contract.
    output_contract: OutputContract | None = None


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
    # When true, each Send branch's worktree delta is promoted back to base
    # at the end of a clean run (top-level nodes only, before GC), routed
    # through reconcile_promotions + on_promote_conflict + promote_exclude.
    # Distinct from Node.promote (which promotes a node's OWN worktree —
    # for a fan-out parent that is the manifest-only tree; see the lint).
    promote: bool = False


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

    max_retries: int = 3
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

    # Worktree isolation default for the graph; inherits graph→subgraph via
    # `merge_settings`. `auto` = isolate per-node iff in a git repo (today's
    # implicit behavior, made explicit). Per-node override: `Node.worktree`.
    worktree: Literal["auto", "isolated", "off"] = "auto"
    worktree_group: str | None = None
    worktree_gc: Literal["never", "on_success"] = "never"
    # Cross-node promote reconciliation when two same-wave promoting nodes
    # touch the same path. `warn` (default): log the overlap, last-write-wins
    # (run stays green). `fail`: abort before any write. `overwrite`: silent
    # last-write-wins. `skip`: the first promoting node (in `nodes` order)
    # keeps the path; later nodes drop it (their other paths still promote).
    on_promote_conflict: Literal["fail", "warn", "overwrite", "skip"] = "warn"
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
    # Git pathspecs filtered out of EVERY promoting node's footprint (promote
    # layer). Defense-in-depth beneath worktree-level excludes: keeps generated
    # artifacts (node_modules, build caches) from being promoted into base even
    # if they slip past a worktree's .git/info/exclude. Prefix-match, e.g.
    # ["node_modules", ".next/cache"].
    promote_exclude: list[str] = []
    # Git pathspecs RE-INCLUDED into the promote footprint after
    # `promote_exclude` removes them — the allow-list half of exclude. Lets
    # you drop a directory but keep a subpath (e.g. promote_exclude=["log/"]
    # + promote_include=["log/phases/**"] keeps log/phases/** and drops the
    # rest of log/). Git `:(exclude)` pathspecs have no in-list negation, so
    # this is a second pass unioned back in. Applies to every promoting node.
    promote_include: list[str] = []
    # Read-only sharing: whole-dir symlinks of these base paths into each
    # worktree (no in-worktree install). For read-only in-branch gates
    # (tsc --noEmit, scoped tests). Each shared path also gets the
    # info/exclude write so it stays out of the promote footprint.
    worktree_share: list[str] = []
    # Rehydrate path: ordered shell commands run in each fresh worktree so the
    # package manager builds its own real node_modules there (e.g.
    # ["pnpm install --prefer-offline", "pnpm exec prisma generate"]). PM-agnostic.
    worktree_setup: list[str] = []
    # Paths written to each worktree's info/exclude before promote can run, so
    # generated artifacts (node_modules, in-tree prisma output=) stay out of the
    # footprint. Explicit — sqrlly does not infer generator output paths.
    worktree_setup_exclude: list[str] = []

    @field_validator("worktree", mode="before")
    @classmethod
    def _normalize_worktree_setting(cls, v: Any) -> Any:
        return _normalize_worktree(v)

    @model_validator(mode="after")
    def _worktree_group_exclusive(self) -> "Settings":
        _check_worktree_group_exclusive(self.worktree, self.worktree_group)
        return self



class Node(KindStep):
    id: Name
    name: Name = Field(default_factory=lambda data: data["id"])
    description: str | None = None
    depends_on: list[str] = []
    evaluation: Evaluation | None = None
    output_contract: OutputContract | None = None
    fan_out: FanOut | None = None
    route: Route | None = None
    timeout: float | None = None
    worktree: Literal["auto", "isolated", "off"] | None = None
    worktree_group: str | None = None
    # Apply this node's worktree git delta to the base workdir after a clean
    # run. Wired for top-level nodes only (not fan-out children / subgraph
    # inner nodes). See cli/main.py run flow.
    promote: bool = False

    @field_validator("worktree", mode="before")
    @classmethod
    def _normalize_worktree_override(cls, v: Any) -> Any:
        return _normalize_worktree(v)

    @model_validator(mode="after")
    def _worktree_group_exclusive(self) -> "Node":
        _check_worktree_group_exclusive(self.worktree, self.worktree_group)
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

    def effective_worktree(self, settings: Settings) -> tuple[str, str | None]:
        """Resolve isolation by scope specificity. Returns (kind, group):
        kind in {auto, isolated, off, group}; group is the shared-tree name
        when kind == "group", else None."""
        if self.worktree_group is not None:
            return ("group", self.worktree_group)
        if self.worktree is not None:
            return (self.worktree, None)
        if self.kind in ("agent", "run"):
            mode = getattr(settings.defaults, self.kind).worktree
            if mode is not None:
                return (mode, None)
        if settings.worktree_group is not None:
            return ("group", settings.worktree_group)
        return (settings.worktree, None)

    def effective_promote(self, settings: Settings) -> bool:
        if "promote" not in self.model_fields_set and self.kind in ("run", "agent"):
            return bool(getattr(settings.defaults, self.kind).promote)
        return self.promote

    def effective_max_retries(self, settings: Settings) -> int:
        if self.evaluation and self.evaluation.max_retries is not None:
            return self.evaluation.max_retries
        return settings.max_retries


class Graph(BaseModel):
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
        for node in self.nodes:
            steps: list[KindStep] = [node]
            if node.fan_out:
                if node.fan_out.template:
                    steps.append(node.fan_out.template)
                steps.extend(node.fan_out.final_nodes)
            for step in steps:
                if step.agent:
                    tools = defaults.tools if step.agent.tools is None else step.agent.tools
                    if isinstance(tools, list) and (step.agent.provider or defaults.provider) != "anthropic":
                        raise ValueError(f"{node.id}: agent tool-name lists require provider: anthropic")
                if directory:
                    prompt = step.agent or step.llm
                    if prompt and prompt.prompt:
                        require_reference(root, directory / self.settings.prompts / prompt.prompt,
                                          f"{node.id}.prompt", ' (for inline prompts use {text: "..."})')
                    elif step.workflow:
                        require_reference(root, directory / step.workflow.file, f"{node.id}.workflow",
                                          ' ({text: "..."} is for inline agent or llm prompts)')
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
        return self

    @model_validator(mode="after")
    def _validate_route_targets(self) -> Self:
        node_ids = {n.id for n in self.nodes}

        def _check_targets(source_id: str, where: str, targets: Any) -> None:
            ids = targets if isinstance(targets, list) else [targets]
            for tgt in ids:
                if tgt == "__end__":
                    continue
                if tgt not in node_ids:
                    raise ValueError(
                        f"Route '{source_id}' {where} '{tgt}' "
                        f"references nonexistent node"
                    )

        for node in self.nodes:
            if node.route is None:
                continue
            r = node.route
            if r.goto is not None:
                _check_targets(node.id, "goto", r.goto)
            for case in r.cases:
                _check_targets(
                    node.id, f"case[when={case.when!r}] goto", case.goto,
                )
            if r.else_ is not None:
                _check_targets(node.id, "else goto", r.else_.goto)
        return self

    @model_validator(mode="after")
    def _validate_routes_not_depended_on(self) -> Self:
        # Inline-route nodes dispatch via Command(goto=); they cannot
        # appear in another node's `depends_on:` (would double-trigger
        # the goto target).
        inline_route_ids: set[str] = {
            n.id for n in self.nodes if n.route is not None
        }
        for node in self.nodes:
            for dep in node.depends_on:
                if dep in inline_route_ids:
                    raise ValueError(
                        f"Node '{node.id}' depends on route '{dep}'; "
                        f"routes dispatch via Command(goto=), so they "
                        f"cannot appear in another node's depends_on"
                    )
        return self



def load_workflow(
    path: str | Path, *, resolve_settings: Callable[[Settings], Settings] | None = None,
) -> Graph:
    path = Path(path)
    with path.open(encoding="utf-8") as stream:
        raw = yaml.load(stream, Loader=WorkflowLoader)
    return Graph.model_validate(raw, context={"workflow_path": path, "resolve_settings": resolve_settings})
