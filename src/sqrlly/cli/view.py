"""Render a workflow as a single self-contained HTML viewer.

Two modes:
  - Authoring: ``sqrlly view <yaml>`` — topology + per-node
    config panel. No runtime overlay.
  - Debug: ``sqrlly view <yaml> --log <jsonl>`` — same as above,
    plus per-node status overlay (passed/failed/untouched, with a
    retry-count badge) and per-node log slices on click.

The Mermaid output is generated directly from the ``Graph`` model
rather than from ``compiled.get_graph().draw_mermaid()``. This gives
the viewer two properties LangGraph's emission does not:

  1. **Author's perspective.** Synthetic ``_eval_<id>`` and
     ``_route_<id>`` nodes that the compile layer adds are not
     surfaced — authors see the workflow as written, not as compiled.
  2. **Layout control.** All workflow nodes are wrapped in a Mermaid
     ``subgraph`` block. Invisible spine edges ``START ~~~ workflow
     ~~~ END`` are emitted first to anchor layout direction; real
     entry/terminal edges follow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqrlly.compile.lint import iter_route_edges
from sqrlly.schema.models import Graph, Node


# ----- Mermaid emission -----------------------------------------------------


_END_TARGET = "__end__"


def _node_shape(node: Node) -> tuple[str, str]:
    """Return Mermaid delimiters for the authored node."""
    if node.fan_out is not None:
        return "{{", "}}"
    if node.route and node.kind is None:
        return "{", "}"
    if node.workflow:
        return "[[", "]]"
    return "[", "]"


def _node_label(node: Node) -> str:
    """Human-readable label used inside the shape brackets.

    Escapes double quotes and trims the rendered name. The full
    config (timeouts, model, eval, etc.) is rendered in
    the side panel, not on the diagram.
    """
    label = node.name or node.id
    return label.replace('"', '&quot;')


def _route_targets(graph: Graph) -> dict[str, list[tuple[str, str | None]]]:
    """For each node, list its outgoing route targets and their labels.

    Returns ``{node_id: [(target_id, label_or_None), ...]}``. Label is
    ``None`` for unconditional gotos, ``"else"`` for else branches,
    and the ``when`` predicate string for case branches.
    """
    out: dict[str, list[tuple[str, str | None]]] = {}
    for node in graph.nodes:
        if not node.route:
            continue
        edges: list[tuple[str, str | None]] = []
        for case in node.route:
            for target in case.targets:
                label = "else" if case.otherwise else case.when
                if target in ("retry", "fail"):
                    label = f"{label}: {target}" if label else target
                    target = node.id if target == "retry" else _END_TARGET
                edges.append((target, label))
        if edges:
            out[node.id] = edges
    return out


def _classify_endpoints(graph: Graph) -> tuple[set[str], set[str]]:
    """Return nodes with an entry edge and nodes that fall through to END."""
    all_ids = {n.id for n in graph.nodes}
    has_incoming: set[str] = set()
    has_outgoing: set[str] = set()

    # depends_on contributes incoming for the node itself, outgoing
    # for the dep.
    for node in graph.nodes:
        if node.depends_on:
            has_incoming.add(node.id)
            for dep in node.depends_on:
                has_outgoing.add(dep)

    # A conditional route can also fall through to its static dependents or END.
    for node in graph.nodes:
        if any(case.when is None and case.max_attempts is None for case in node.route or []):
            has_outgoing.add(node.id)
    for _, _, target, is_upstream in iter_route_edges(graph):
        if not is_upstream and target in all_ids:
            has_incoming.add(target)

    entry_ids = all_ids - has_incoming
    terminal_ids = all_ids - has_outgoing
    return entry_ids, terminal_ids


def render_mermaid(graph: Graph, direction: str = "TB") -> str:
    """Emit a Mermaid flowchart for the workflow.

    Layout strategy:
      - ``START`` and ``END`` declared at top.
      - Invisible spine ``START ~~~ workflow ~~~ END`` defined FIRST
        (anchors dagre's layout direction).
      - All workflow nodes wrapped in a single ``subgraph workflow``
        block.
      - Real entry edges (``START --> entry_node``) and terminal
        edges (``terminal --> END``) emitted last.
    """
    if direction not in ("TB", "LR", "BT", "RL"):
        raise ValueError(
            f"direction must be one of TB/LR/BT/RL, got {direction!r}"
        )

    routes = _route_targets(graph)
    entry_ids, terminal_ids = _classify_endpoints(graph)

    # Fan-out multiplicity: children only exist at run time, so each
    # fan-out parent gets a dashed STAND-IN node for its N children
    # (entered 1→N) plus its declared final_nodes as real chained steps.
    # Dependents of the parent consume the branch aggregate, so their
    # edges re-source from the chain exit: the stand-in (N→1) when there
    # are no finals, else the LAST final — matching the compiled graph,
    # where the last final's pass targets are the parent's dependents.
    all_ids = {n.id for n in graph.nodes}

    def _fresh(render_id: str) -> str:
        while render_id in all_ids:
            render_id += "_"
        all_ids.add(render_id)
        return render_id

    fan_standin: dict[str, str] = {}
    fan_exit: dict[str, str] = {}
    fan_decls: list[str] = []
    fan_edges: list[str] = []
    for node in sorted(graph.nodes, key=lambda n: n.id):
        if node.fan_out is None:
            continue
        standin = _fresh(f"{node.id}__items")
        fan_standin[node.id] = standin
        template = node.fan_out.template
        tname = template.kind if template and template.kind else "template"
        if template and template.kind in ("llm", "agent"):
            prompt = getattr(template, template.kind).prompt
            if prompt:
                tname = Path(prompt).name
        elif template and template.workflow:
            tname = Path(template.workflow.file).name
        safe_t = tname.replace('"', "'")
        fan_decls.append(
            f'        {standin}[/"×N per item: {safe_t}"/]:::fanout'
        )
        fan_edges.append(f'        {node.id} -->|"1→N"| {standin}')
        prev, prev_label = standin, '|"N→1"|'
        for final in node.fan_out.final_nodes:
            rid = _fresh(f"{node.id}__{final.id}")
            marker = ":::gated" if final.eval else ""
            safe_f = (final.name or final.id).replace('"', "&quot;")
            fan_decls.append(f'        {rid}["{safe_f}"]{marker}')
            fan_edges.append(f"        {prev} -->{prev_label} {rid}")
            prev, prev_label = rid, ""
        fan_exit[node.id] = prev

    lines: list[str] = []
    lines.append(f"flowchart {direction}")
    lines.append("    START([START])")
    lines.append("    END([END])")
    lines.append("")
    # Invisible spine — first edges in the file so dagre uses them
    # to anchor the workflow box between START and END.
    lines.append("    START ~~~ workflow")
    lines.append("    workflow ~~~ END")
    lines.append("")
    lines.append('    subgraph workflow [" "]')

    # Node declarations, sorted by id for stable output.
    nodes_sorted = sorted(graph.nodes, key=lambda n: n.id)
    for node in nodes_sorted:
        open_b, close_b = _node_shape(node)
        label = _node_label(node)
        suffix = ":::gated" if node.eval else ""
        lines.append(f'        {node.id}{open_b}"{label}"{close_b}{suffix}')
    lines.extend(fan_decls)

    # Internal edges: depends_on + routes between in-graph nodes.
    # A dep on a fan-out parent re-sources from the parent's chain exit
    # (N→1 labeled when the exit is the children stand-in itself).
    for node in nodes_sorted:
        for dep in node.depends_on:
            src = fan_exit.get(dep, dep)
            if src == fan_standin.get(dep):
                lines.append(f'        {src} -->|"N→1"| {node.id}')
            else:
                lines.append(f"        {src} --> {node.id}")
    lines.extend(fan_edges)
    for src in sorted(routes.keys()):
        for tgt, label in routes[src]:
            if tgt == _END_TARGET or tgt not in all_ids:
                continue
            if label is None:
                lines.append(f"        {src} -.-> {tgt}")
            else:
                safe = label.replace("|", "/").replace('"', "'")
                lines.append(f'        {src} -.->|"{safe}"| {tgt}')

    lines.append("    end")
    lines.append("")

    # Spine endpoints: START → entries, terminals → END.
    for entry in sorted(entry_ids):
        lines.append(f"    START --> {entry}")
    for term in sorted(terminal_ids):
        lines.append(f"    {fan_exit.get(term, term)} --> END")

    # A fail destination ends this path.
    for src in sorted(routes.keys()):
        for tgt, label in routes[src]:
            if tgt != _END_TARGET:
                continue
            if label is None:
                lines.append(f"    {src} -.-> END")
            else:
                safe = label.replace("|", "/").replace('"', "'")
                lines.append(f'    {src} -.->|"{safe}"| END')

    # Style for gated nodes — colored stroke marks "this node has an
    # evaluation gate." Subtle so it doesn't clash with status overlay.
    lines.append("")
    lines.append("    classDef gated stroke:#9333ea,stroke-width:2px")
    lines.append(
        "    classDef fanout stroke-dasharray: 5 4,stroke:#0891b2"
    )

    return "\n".join(lines)


# ----- Per-node config extraction -------------------------------------------


def extract_node_config(node: Node) -> dict[str, Any]:
    """Distill the YAML-level fields a viewer should show in the
    side panel.

    Keeps only fields with non-default values so the panel doesn't
    drown in defaults. Returned dict is JSON-serializable.
    """
    cfg: dict[str, Any] = {"id": node.id, "name": node.name}
    if node.description:
        cfg["description"] = node.description
    if node.depends_on:
        cfg["depends_on"] = list(node.depends_on)
    if node.timeout is not None:
        cfg["timeout"] = node.timeout
    if node.kind:
        cfg[node.kind] = getattr(node, node.kind).model_dump(by_alias=True, exclude_unset=True)
    if node.eval:
        cfg["eval"] = node.eval.model_dump(by_alias=True, exclude_unset=True)
    if node.fan_out:
        fo: dict[str, Any] = {}
        if node.fan_out.manifest_path:
            fo["manifest_path"] = node.fan_out.manifest_path
        if node.fan_out.template:
            fo["template"] = node.fan_out.template.model_dump(by_alias=True, exclude_unset=True)
        if node.fan_out.final_nodes:
            fo["final_nodes"] = [n.id for n in node.fan_out.final_nodes]
        cfg["fan_out"] = fo
    if node.route:
        cfg["route"] = [case.model_dump(by_alias=True, exclude_unset=True) for case in node.route]
    if node.output_contract:
        oc: dict[str, Any] = {}
        if node.output_contract.base_directory:
            oc["base_directory"] = node.output_contract.base_directory
        if node.output_contract.required_files:
            oc["required_files"] = list(node.output_contract.required_files)
        cfg["output_contract"] = oc
    return cfg


# ----- Status overlay from JSONL events -------------------------------------


@dataclass
class NodeStatus:
    status: str  # "passed" | "failed" | "untouched"
    # NB: retry is surfaced via ``retry_count`` (and the "retried" CSS
    # class keys off retry_count > 0), not via this status field — a
    # retried-then-passed node is "passed".
    fired_count: int
    retry_count: int
    last_error: str | None
    events: list[dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "fired_count": self.fired_count,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "events": self.events,
        }


def compute_node_status(
    graph: Graph,
    events: list[dict[str, Any]] | None,
) -> dict[str, NodeStatus]:
    """Reduce a JSONL event list to a per-node status snapshot.

    Rules:
      - ``status="untouched"`` if no terminal event for the node.
      - ``status="passed"`` if the LAST terminal event is
        ``node_completed``.
      - ``status="failed"`` if the LAST terminal event is
        ``node_failed``.
      - ``retry_count`` = number of ``node_retried`` events.
      - ``fired_count`` = number of ``node_completed`` events
        (a goto-driven re-fire that completes successfully bumps
        this; useful for spotting wave-pattern dispatcher loops).
      - ``last_error`` is the most recent ``error`` field on a
        ``node_failed`` event, else ``None``.
      - All events with matching ``node`` field are kept under
        ``events`` for the side-panel drill-down.

    Untouched nodes (defined in topology, no events) get a
    ``NodeStatus`` row with empty events. Nodes that appear in events
    but are NOT in the topology (e.g., fan-out children
    ``parent::child``) are also returned so the viewer can render
    chip-level stats for them; the topology renderer simply ignores
    those keys.
    """
    statuses: dict[str, NodeStatus] = {}

    # Seed from topology so untouched nodes still appear.
    for node in graph.nodes:
        statuses[node.id] = NodeStatus(
            status="untouched",
            fired_count=0,
            retry_count=0,
            last_error=None,
            events=[],
        )

    if not events:
        return statuses

    for ev in events:
        node_id = ev.get("node")
        if not node_id:
            continue
        if node_id not in statuses:
            statuses[node_id] = NodeStatus(
                status="untouched",
                fired_count=0,
                retry_count=0,
                last_error=None,
                events=[],
            )
        s = statuses[node_id]
        s.events.append(ev)
        et = ev.get("event")
        if et == "node_completed":
            s.status = "passed"
            s.fired_count += 1
        elif et == "node_failed":
            s.status = "failed"
            s.last_error = ev.get("error")
        elif et == "node_retried":
            s.retry_count += 1

    return statuses


# ----- HTML rendering -------------------------------------------------------


_TEMPLATE_PATH = Path(__file__).parent / "templates" / "view.html.j2"


def render_view(
    graph: Graph,
    events: list[dict[str, Any]] | None,
    direction: str = "TB",
) -> str:
    """Render the full HTML page as a string.

    Imports Jinja2 lazily so unrelated CLI commands don't pay the
    import cost.
    """
    from jinja2 import Template

    mermaid = render_mermaid(graph, direction=direction)
    statuses = compute_node_status(graph, events)
    node_configs = {n.id: extract_node_config(n) for n in graph.nodes}

    payload = {
        "workflow": {
            "name": graph.name,
        },
        "node_configs": node_configs,
        "statuses": {nid: s.to_json() for nid, s in statuses.items()},
        "has_log": events is not None,
    }

    template = Template(
        _TEMPLATE_PATH.read_text(),
        keep_trailing_newline=True,
    )
    return template.render(
        mermaid=mermaid,
        payload=payload,
        workflow_name=graph.name or "Workflow",
        has_log=events is not None,
    )


def read_jsonl_log(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL log file into a list of event dicts.

    Skips blank lines and lines that don't parse as JSON (resilient
    to trailing whitespace, partial writes).
    """
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
