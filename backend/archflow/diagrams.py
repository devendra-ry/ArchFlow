"""More diagrams from the same model, as text: Mermaid and PlantUML sources with a plain-language alt text.

Every diagram is derived from the project state (nothing is stored), so it always matches the canvas:

* ``hld`` — the high-level architecture itself (every top-level component and connection);
* ``c4-context`` — C4 level 1: the system as one box, its users (clients) and the external systems it uses;
* ``c4-container`` — C4 level 2: the containers (gateways, services, agents, stores, queues…) inside the system
  boundary, with users and external systems outside it;
* ``sequence-<n>`` — one sequence diagram per critical flow. Flows are traced deterministically: the shortest path
  from a client (or another entry point) through gateways and services to each data store, with side calls to
  security, caches and LLMs on the way. With an LLM configured, the ``diagrams.sequence`` agent may name the flows
  after the requirements they serve and pick among the traced paths (it can only choose paths that exist);
  offline, or when it fails, flows are named after the requirement most of their components trace to;
* ``er`` — every ``table`` of every low-level design (props.columns), grouped by database, with relations from the
  LLD connections and from foreign-key columns (``customer_id`` → ``customers``);
* ``deployment`` — components grouped by platform (vendor: AWS, Azure, Google Cloud, SaaS, self-hosted) and
  runtime (Kubernetes, serverless, containers, managed data services…) from their vendor, catalog entry,
  technology and props;
* ``dataflow`` — data flows across trust boundaries: public clients, edge (gateways and security controls),
  private services, data stores and third parties. Flows that cross a boundary are drawn thick, and a flow from
  the public zone straight into private services or data is flagged.

Mermaid output escapes labels with numeric entity codes and gives every element a prefixed id, so names with
quotes, ``#``, ``;``, brackets or reserved words (``end``) cannot break the syntax; ``accTitle`` / ``accDescr``
carry the alt text into the rendered SVG. PlantUML output uses the C4-PlantUML standard library for the C4 views.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from . import agents, catalog, llm
from .lld import _plural, _singular, parse_columns
from .models import Node, ProjectState

log = logging.getLogger("archflow.diagrams")

PROMPT = "diagrams.sequence"  # prompts/diagrams.sequence.yaml
MAX_FLOWS = 6
MAX_CANDIDATES = 12
CACHE_SIZE = 128

STORE_TYPES = ("database", "storage")
DATA_TYPES = ("database", "storage", "cache", "queue")
EXTERNAL_TYPES = ("external", "llm")
CONTAINER_TYPES = ("gateway", "service", "agent", "mcp", "security", "database", "cache", "queue", "storage")
# Dependencies called on the way through a flow (not part of the main path), with the verb of the call.
SIDE_CALLS = {"security": "verify token", "cache": "get / set", "llm": "prompt"}
VERBS = {"database": "query / write", "storage": "read / write", "cache": "get / set", "llm": "prompt",
         "mcp": "tool call", "security": "verify token"}
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
TYPE_WORDS = {
    "client": ("client", "clients"), "gateway": ("gateway", "gateways"), "service": ("service", "services"),
    "agent": ("agent", "agents"), "mcp": ("MCP server", "MCP servers"), "security": ("security component",
    "security components"), "database": ("database", "databases"), "cache": ("cache", "caches"),
    "queue": ("queue", "queues"), "storage": ("storage", "storage components"), "llm": ("LLM", "LLMs"),
    "external": ("external system", "external systems"),
}


# --------------------------------------------------------------------------- text escaping
_MERMAID_ESCAPES = {'"': "#34;", "#": "#35;", "%": "#37;", ";": "#59;", "<": "#60;", ">": "#62;", "`": "#96;",
                    "{": "#123;", "|": "#124;", "}": "#125;"}


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def mermaid_text(text: str) -> str:
    """A label safe inside Mermaid quotes and messages: special characters become numeric entity codes."""
    return "".join(_MERMAID_ESCAPES.get(c, c) for c in _one_line(text))


def mermaid_acc(text: str) -> str:
    """Text for ``accTitle`` / ``accDescr`` (one line, no comment markers)."""
    return _one_line(text).replace("%%", "%")


def plantuml_text(text: str) -> str:
    """A label safe inside PlantUML double quotes and after ``:`` (no quotes, backslashes or line breaks)."""
    return _one_line(text).replace('"', "'").replace("\\", "/")


def _er_word(text: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", text or "").strip("_") or fallback


class _Ids:
    """Diagram ids: ``<prefix>_<slug>``, unique per diagram; the prefix keeps reserved words (``end``) out."""

    def __init__(self) -> None:
        self.map: dict[str, str] = {}
        self.used: set[str] = set()

    def __call__(self, key: str, hint: str = "", prefix: str = "n") -> str:
        if key not in self.map:
            base = f"{prefix}_" + (re.sub(r"[^A-Za-z0-9_]", "_", hint or key).strip("_")[:40] or "x")
            out, i = base, 2
            while out in self.used:
                out, i = f"{base}_{i}", i + 1
            self.used.add(out)
            self.map[key] = out
        return self.map[key]


def _names(items: list[str], limit: int = 8) -> str:
    """'A', 'A and B', 'A, B and C', '…, and 3 more'."""
    if len(items) > limit:
        items = items[:limit] + [f"{len(items) - limit} more"]
    if len(items) < 2:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _count(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular if n == 1 else plural or singular + 's'}"


# --------------------------------------------------------------------------- model helpers
def top_nodes(state: ProjectState) -> list[Node]:
    return [n for n in state.nodes.values() if not n.parent_id and n.type != "note"]


def _top_edges(state: ProjectState, ids: set[str]) -> list[tuple[str, str, str]]:
    return [(e.source, e.target, _edge_label(e.protocol, e.label)) for e in state.edges.values()
            if e.source in ids and e.target in ids and e.source != e.target]


def _edge_label(protocol: str, label: str) -> str:
    protocol, label = _one_line(protocol), _one_line(label)
    return f"{protocol} ({label})" if protocol and label and label != protocol else protocol or label


def _detail(node: Node) -> str:
    return _one_line(node.technology)


# --------------------------------------------------------------------------- graph diagrams (IR + renderers)
@dataclass
class Box:
    id: str
    label: str
    shape: str = "container"  # person | system | container | gateway | security | db | queue | external
    detail: str = ""
    group: str = ""
    description: str = ""


@dataclass
class Group:
    id: str
    label: str
    parent: str = ""
    kind: str = "rectangle"  # PlantUML keyword: rectangle | node | cloud | package


@dataclass
class Link:
    source: str
    target: str
    label: str = ""
    style: str = "solid"  # solid | dashed | thick


@dataclass
class Graph:
    direction: str
    boxes: list[Box] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    c4: str = ""  # "" | context | container: PlantUML uses the C4 library


SHAPE_OF = {"client": "person", "gateway": "gateway", "security": "security", "database": "db", "storage": "db",
            "cache": "db", "queue": "queue", "external": "external", "llm": "external"}
_MERMAID_SHAPES = {"person": '(["{}"])', "system": '["{}"]', "container": '["{}"]', "gateway": '{{{{"{}"}}}}',
                   "security": '[/"{}"\\]', "db": '[("{}")]', "queue": '>"{}"]', "external": '["{}"]'}
# Only an outline colour per kind: fill and text come from whatever theme renders the diagram (Mermaid's neutral or dark
# theme in the app, GitHub's or a wiki's elsewhere), so the same source reads well in light and dark. The shapes above
# already tell the kinds apart; the outline is a second, quieter cue. The hues match the canvas's component colours.
_MERMAID_CLASSES = {
    "person": "stroke:#0090ff,stroke-width:1.5px", "system": "stroke:#3e63dd,stroke-width:1.5px",
    "container": "stroke:#30a46c,stroke-width:1.5px", "gateway": "stroke:#6e56cf,stroke-width:1.5px",
    "security": "stroke:#dc3e42,stroke-width:1.5px", "db": "stroke:#f76b15,stroke-width:1.5px",
    "queue": "stroke:#ffb224,stroke-width:1.5px", "external": "stroke:#8b8d98,stroke-width:1.5px,stroke-dasharray:4 3",
}
_ARROWS = {"solid": "-->", "dashed": "-.->", "thick": "==>"}


def graph_mermaid(g: Graph, title: str, alt: str) -> str:
    lines = [f"flowchart {g.direction}", f"    accTitle: {mermaid_acc(title)}", f"    accDescr: {mermaid_acc(alt)}"]

    def box_line(b: Box, indent: str) -> str:
        text = mermaid_text(b.label) + (f"<br/><small>[{mermaid_text(b.detail)}]</small>" if b.detail else "")
        return indent + b.id + _MERMAID_SHAPES[b.shape].format(text)

    def emit(parent: str, depth: int) -> None:
        indent = "    " * depth
        for b in g.boxes:
            if b.group == parent:
                lines.append(box_line(b, indent))
        for grp in g.groups:
            if grp.parent == parent:
                lines.append(f'{indent}subgraph {grp.id}["{mermaid_text(grp.label)}"]')
                emit(grp.id, depth + 1)
                lines.append(f"{indent}end")

    emit("", 1)
    for link in g.links:
        arrow = _ARROWS[link.style]
        label = f'|"{mermaid_text(link.label)}"|' if link.label else ""
        lines.append(f"    {link.source} {arrow}{label} {link.target}")
    shapes: dict[str, list[str]] = {}
    for b in g.boxes:
        shapes.setdefault(b.shape, []).append(b.id)
    for shape, ids in shapes.items():
        lines.append(f"    classDef {shape} {_MERMAID_CLASSES[shape]}")
        lines.append(f"    class {','.join(ids)} {shape}")
    return "\n".join(lines)


_PLANTUML_KEYWORDS = {"person": "actor", "system": "rectangle", "container": "component", "gateway": "component",
                      "security": "component", "db": "database", "queue": "queue", "external": "rectangle"}
_C4_MACROS = {"person": "Person", "system": "System", "external": "System_Ext", "db": "ContainerDb",
              "queue": "ContainerQueue"}
_PLANTUML_ARROWS = {"solid": "-->", "dashed": "..>", "thick": "-[bold]->"}


def graph_plantuml(g: Graph, title: str) -> str:
    lines = ["@startuml"]
    if g.c4:
        lines.append(f"!include <C4/C4_{'Context' if g.c4 == 'context' else 'Container'}>")
    lines.append(f"title {plantuml_text(title)}")
    if g.direction == "LR":
        lines.append("left to right direction" if not g.c4 else "LAYOUT_LEFT_RIGHT()")

    def box_line(b: Box, indent: str) -> str:
        label, detail, desc = plantuml_text(b.label), plantuml_text(b.detail), plantuml_text(b.description)
        if g.c4:
            macro = _C4_MACROS.get(b.shape, "Container")
            if macro.startswith("Container"):
                return f'{indent}{macro}({b.id}, "{label}", "{detail}", "{desc}")'
            return f'{indent}{macro}({b.id}, "{label}", "{desc or detail}")'
        text = label + (f"\\n[{detail}]" if detail else "")
        stereo = " <<external>>" if b.shape == "external" else ""
        return f'{indent}{_PLANTUML_KEYWORDS[b.shape]} "{text}" as {b.id}{stereo}'

    def emit(parent: str, depth: int) -> None:
        indent = "  " * depth
        for b in g.boxes:
            if b.group == parent:
                lines.append(box_line(b, indent))
        for grp in g.groups:
            if grp.parent == parent:
                if g.c4:
                    lines.append(f'{indent}System_Boundary({grp.id}, "{plantuml_text(grp.label)}") {{')
                else:
                    lines.append(f'{indent}{grp.kind} "{plantuml_text(grp.label)}" as {grp.id} {{')
                emit(grp.id, depth + 1)
                lines.append(f"{indent}}}")

    emit("", 0)
    for link in g.links:
        if g.c4:
            lines.append(f'Rel({link.source}, {link.target}, "{plantuml_text(link.label) or "Uses"}")')
        else:
            label = f" : {plantuml_text(link.label)}" if link.label else ""
            lines.append(f"{link.source} {_PLANTUML_ARROWS[link.style]} {link.target}{label}")
    lines.append("@enduml")
    return "\n".join(lines)


def _diagram(id_: str, kind: str, title: str, alt: str, mermaid: str = "", plantuml: str = "",
             **extra: Any) -> dict[str, Any]:
    return {"id": id_, "kind": kind, "title": title, "alt": alt, "empty": not mermaid, "mermaid": mermaid,
            "plantuml": plantuml, **extra}


def _graph_diagram(id_: str, kind: str, title: str, alt: str, g: Graph, **extra: Any) -> dict[str, Any]:
    return _diagram(id_, kind, title, alt, graph_mermaid(g, title, alt), graph_plantuml(g, title), **extra)


def _by_type(nodes: list[Node]) -> str:
    """'2 clients (WhatsApp and Web App), 1 gateway (API Gateway), …' in the order types first appear."""
    groups: dict[str, list[str]] = {}
    for n in nodes:
        groups.setdefault(n.type, []).append(n.name)
    parts = []
    for t, names in groups.items():
        one, many = TYPE_WORDS.get(t, (t, t + "s"))
        parts.append(f"{len(names)} {one if len(names) == 1 else many} ({_names(names, 5)})")
    return "; ".join(parts)


# --------------------------------------------------------------------------- HLD, C4 context and container
def hld(state: ProjectState, name: str) -> dict[str, Any]:
    title = f"{name}: high-level architecture"
    nodes = top_nodes(state)
    if not nodes:
        return _diagram("hld", "hld", title, "The architecture has no components yet.")
    ids = _Ids()
    g = Graph("TB", boxes=[Box(ids(n.id, n.name), n.name, SHAPE_OF.get(n.type, "container"), _detail(n))
                           for n in nodes])
    edges = _top_edges(state, {n.id for n in nodes})
    g.links = [Link(ids.map[s], ids.map[t], label) for s, t, label in edges]
    alt = (f"High-level architecture of {name} with {_count(len(nodes), 'component')} and "
           f"{_count(len(edges), 'connection')}: {_by_type(nodes)}.")
    return _graph_diagram("hld", "hld", title, alt, g)


def c4_context(state: ProjectState, name: str) -> dict[str, Any]:
    title = f"{name}: system context (C4 level 1)"
    nodes = top_nodes(state)
    if not nodes:
        return _diagram("c4-context", "c4-context", title, "The architecture has no components yet.")
    ids = _Ids()
    system = ids("__system__", name, prefix="s")
    people = [n for n in nodes if n.type == "client"]
    externals = [n for n in nodes if n.type in EXTERNAL_TYPES]
    inside = [n for n in nodes if n.type not in ("client", *EXTERNAL_TYPES)]
    outer = {n.id: ids(n.id, n.name) for n in people + externals}
    g = Graph("LR", c4="context")
    g.boxes += [Box(outer[n.id], n.name, "person", _detail(n), description=n.description) for n in people]
    g.boxes.append(Box(system, name, "system", "Software system",
                       description=f"{_count(len(inside), 'container')}: " + _names([n.name for n in inside], 6)
                       if inside else "Software system"))
    g.boxes += [Box(outer[n.id], n.name, "external", _detail(n), description=n.description) for n in externals]
    rels: dict[tuple[str, str], list[str]] = {}
    for s, t, label in _top_edges(state, {n.id for n in nodes}):
        a, b = outer.get(s, system), outer.get(t, system)
        if a != b:
            labels = rels.setdefault((a, b), [])
            if label and label not in labels:
                labels.append(label)
    g.links = [Link(a, b, "Uses" + (f": {', '.join(labels[:3])}" if labels else "")) for (a, b), labels in rels.items()]
    alt = (f"System context of {name}. "
           + (f"Used by {_names([n.name for n in people])}. " if people else "No clients are modelled. ")
           + (f"Depends on the external systems {_names([n.name for n in externals])}. " if externals
              else "It uses no external systems. ")
           + f"Inside the system: {_count(len(inside), 'container')}.")
    return _graph_diagram("c4-context", "c4-context", title, alt, g)


def c4_container(state: ProjectState, name: str) -> dict[str, Any]:
    title = f"{name}: containers (C4 level 2)"
    nodes = top_nodes(state)
    if not nodes:
        return _diagram("c4-container", "c4-container", title, "The architecture has no components yet.")
    ids = _Ids()
    boundary = ids("__system__", name, prefix="g")
    g = Graph("TB", c4="container", groups=[Group(boundary, name)])
    containers = [n for n in nodes if n.type in CONTAINER_TYPES]
    people = [n for n in nodes if n.type == "client"]
    externals = [n for n in nodes if n.type in EXTERNAL_TYPES]
    for n in people:
        g.boxes.append(Box(ids(n.id, n.name), n.name, "person", _detail(n), description=n.description))
    for n in containers:
        g.boxes.append(Box(ids(n.id, n.name), n.name, SHAPE_OF.get(n.type, "container"), _detail(n), boundary,
                           n.description))
    for n in externals:
        g.boxes.append(Box(ids(n.id, n.name), n.name, "external", _detail(n), description=n.description))
    edges = _top_edges(state, {n.id for n in people + containers + externals})
    g.links = [Link(ids.map[s], ids.map[t], label) for s, t, label in edges]
    alt = (f"Containers of {name}. Inside the system boundary: {_by_type(containers) or 'nothing yet'}. "
           + (f"Used by {_names([n.name for n in people])}. " if people else "")
           + (f"External systems: {_names([n.name for n in externals])}. " if externals else "")
           + f"{_count(len(edges), 'connection')}.")
    return _graph_diagram("c4-container", "c4-container", title, alt, g)


# --------------------------------------------------------------------------- deployment
PLATFORMS = {"devices": "User devices", "aws": "AWS", "azure": "Microsoft Azure", "gcp": "Google Cloud",
             "saas": "Third-party SaaS", "self": "Self-hosted / unspecified"}
_PLATFORM_HINTS = [("aws", r"\b(aws|amazon|lambda|dynamodb|aurora|sqs|sns|eks|ecs|fargate|s3|cloudfront)\b"),
                   ("azure", r"\b(azure|cosmos ?db|aks|entra)\b"),
                   ("gcp", r"\b(gcp|google|bigquery|firestore|gke|cloud run|pub/?sub|spanner)\b")]
_RUNTIME_HINTS = [("Kubernetes", r"\b(kubernetes|k8s|eks|aks|gke|helm|openshift)\b"),
                  ("Serverless", r"\b(serverless|lambda|functions?|cloud run|fargate|app runner|vercel|netlify|"
                                 r"workers)\b"),
                  ("Containers", r"\b(docker|containers?|ecs|container apps)\b"),
                  ("Virtual machines", r"\b(vm|vms|ec2|virtual machines?|compute engine)\b")]
_RUNTIME_OF_KIND = {"compute.kubernetes": "Kubernetes", "compute.function": "Serverless",
                    "compute.container": "Containers", "compute.vm": "Virtual machines"}


def placement(node: Node) -> tuple[str, str]:
    """(platform key, runtime label) of a top-level component, from its vendor, catalog entry, technology and
    props (``runtime`` / ``deployment`` / ``hosting`` / ``platform``)."""
    if node.type == "client":
        return "devices", "Browsers and apps"
    entry = catalog.match(node.model_dump(include={"catalog_id", "technology", "name"}))
    hints = " ".join([node.technology, node.name, *(node.props.get(k, "") for k in
                                                    ("runtime", "deployment", "hosting", "platform"))]).lower()
    vendor = node.vendor if node.vendor not in ("", "generic") else (entry or {}).get("vendor", "")
    if vendor in ("", "generic"):
        vendor = next((v for v, rx in _PLATFORM_HINTS if re.search(rx, hints)), "")
    if not vendor and node.type in EXTERNAL_TYPES:
        vendor = "saas"
    platform = vendor if vendor in PLATFORMS else "self"
    runtime = next((label for label, rx in _RUNTIME_HINTS if re.search(rx, hints)), "")
    runtime = runtime or _RUNTIME_OF_KIND.get((entry or {}).get("kind", ""), "")
    if not runtime:
        managed = platform in ("aws", "azure", "gcp")
        runtime = {"database": "Data services", "storage": "Data services", "cache": "Data services",
                   "queue": "Messaging", "gateway": "Edge and networking", "security": "Identity and security",
                   "llm": "AI model APIs", "external": "External APIs"}.get(node.type, "Application runtime")
        if managed and runtime in ("Data services", "Messaging"):
            runtime = f"Managed {runtime[0].lower()}{runtime[1:]}"
    return platform, runtime


def deployment(state: ProjectState, name: str) -> dict[str, Any]:
    title = f"{name}: deployment"
    nodes = top_nodes(state)
    if not nodes:
        return _diagram("deployment", "deployment", title, "The architecture has no components yet.")
    ids = _Ids()
    g = Graph("TB")
    layout: dict[str, dict[str, list[Node]]] = {}
    for n in nodes:
        platform, runtime = placement(n)
        layout.setdefault(platform, {}).setdefault(runtime, []).append(n)
    order = list(PLATFORMS)
    summary = []
    for platform in sorted(layout, key=order.index):
        pid = ids(f"__p_{platform}", platform, prefix="g")
        kind = "cloud" if platform in ("aws", "azure", "gcp", "saas") else "node"
        g.groups.append(Group(pid, PLATFORMS[platform], kind=kind))
        parts = []
        for runtime, members in layout[platform].items():
            rid = ids(f"__r_{platform}_{runtime}", f"{platform}_{runtime}", prefix="g")
            g.groups.append(Group(rid, runtime, pid, kind="node"))
            for n in members:
                g.boxes.append(Box(ids(n.id, n.name), n.name, SHAPE_OF.get(n.type, "container"), _detail(n), rid))
            parts.append(f"{runtime}: {_names([n.name for n in members], 6)}")
        summary.append(f"{PLATFORMS[platform]} ({'; '.join(parts)})")
    edges = _top_edges(state, {n.id for n in nodes})
    g.links = [Link(ids.map[s], ids.map[t], label, "dashed") for s, t, label in edges]
    alt = (f"Deployment of {name} across {_count(len(layout), 'platform')}: " + ". ".join(summary)
           + f". {_count(len(edges), 'connection')} between them.")
    return _graph_diagram("deployment", "deployment", title, alt, g)


# --------------------------------------------------------------------------- data flow with trust boundaries
ZONES = {"public": "Public zone (untrusted clients)", "edge": "Edge / DMZ (gateways and security controls)",
         "private": "Private services", "data": "Data stores", "third": "Third-party services"}
ZONE_NAMES = {"public": "the public zone", "edge": "the edge (DMZ)", "private": "private services",
              "data": "data stores", "third": "third parties"}


def zone_of(node: Node) -> str:
    if node.type == "client":
        return "public"
    if node.type in ("gateway", "security"):
        return "edge"
    if node.type in DATA_TYPES:
        return "data"
    if node.type in EXTERNAL_TYPES:
        return "third"
    return "private"


def dataflow(state: ProjectState, name: str) -> dict[str, Any]:
    title = f"{name}: data flow and trust boundaries"
    nodes = top_nodes(state)
    if not nodes:
        return _diagram("dataflow", "dataflow", title, "The architecture has no components yet.")
    ids = _Ids()
    g = Graph("LR")
    zone = {n.id: zone_of(n) for n in nodes}
    for z, label in ZONES.items():
        members = [n for n in nodes if zone[n.id] == z]
        if not members:
            continue
        gid = ids(f"__z_{z}", z, prefix="z")
        g.groups.append(Group(gid, label, kind="rectangle"))
        g.boxes += [Box(ids(n.id, n.name), n.name, SHAPE_OF.get(n.type, "container"), _detail(n), gid)
                    for n in members]
    crossings, unguarded, outbound = 0, [], []
    for s, t, label in _top_edges(state, set(zone)):
        a, b = zone[s], zone[t]
        style = "solid"
        if a != b:
            crossings += 1
            style = "thick"
            if a == "public" and b in ("private", "data"):
                unguarded.append(f"{state.nodes[s].name} to {state.nodes[t].name}")
                label = (label + " " if label else "") + "(no gateway)"
            if b == "third":
                outbound.append(f"{state.nodes[s].name} to {state.nodes[t].name}")
        g.links.append(Link(ids.map[s], ids.map[t], label, style))
    controls = [n for n in nodes if n.type == "security"]
    zones_used = [ZONE_NAMES[z] for z in ZONES if z in zone.values()]
    alt = (f"Data flow of {name} across {_count(len(zones_used), 'trust zone')} ({_names(zones_used)}). "
           f"{_count(crossings, 'flow crosses', 'flows cross')} a trust boundary"
           + (f"; data leaves to third parties from {_names(outbound, 4)}" if outbound else "") + ". "
           + (f"Security controls: {_names([n.name for n in controls])}. " if controls
              else "No security components are modelled. ")
           + (f"Warning: {_names(unguarded, 4)} reach{'es' if len(unguarded) == 1 else ''} private services or "
              "data without passing a gateway." if unguarded else "Every public flow enters through the edge."))
    return _graph_diagram("dataflow", "dataflow", title, alt, g, crossings=crossings, unguarded=unguarded)


# --------------------------------------------------------------------------- ER across all components
def _database_of(state: ProjectState, table: Node) -> str:
    if table.props.get("database"):
        return _one_line(table.props["database"])
    parent = state.nodes.get(table.parent_id)
    if not parent:
        return "Unassigned"
    return parent.name if parent.type in STORE_TYPES else f"{parent.name} (owned tables)"


def er_model(state: ProjectState) -> dict[str, Any]:
    """Every table of every low-level design: ``{tables: [{id, name, database, columns}], relations: [...]}``.
    Relations come from LLD connections between tables and from foreign-key columns named ``<thing>_id``."""
    tables = [n for n in state.nodes.values() if n.type == "table"]
    rows = [{"id": t.id, "name": t.name, "database": _database_of(state, t),
             "columns": parse_columns(t.props.get("columns", ""))} for t in tables]
    ids = {t.id for t in tables}
    rels: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for e in state.edges.values():
        if e.source in ids and e.target in ids and e.source != e.target and (e.source, e.target) not in seen:
            seen.add((e.source, e.target))
            rels.append({"from": e.source, "to": e.target, "label": _edge_label(e.protocol, e.label) or "has"})
    by_word: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        word = _er_word(r["name"], "t").lower()
        for key in {word, _singular(word), _plural(word)}:
            by_word.setdefault(key, []).append(r)
    for r in rows:
        for c in r["columns"]:
            col = c["name"].lower()
            if not col.endswith("_id") or c["key"] == "PK":
                continue
            base = col[:-3]
            targets = [t for t in by_word.get(base, []) + by_word.get(_plural(base), []) if t["id"] != r["id"]]
            if not targets:
                continue
            target = next((t for t in targets if t["database"] == r["database"]), targets[0])
            if (target["id"], r["id"]) not in seen:
                seen.add((target["id"], r["id"]))
                rels.append({"from": target["id"], "to": r["id"], "label": c["name"]})
    return {"tables": rows, "relations": rels}


def er(state: ProjectState, name: str) -> dict[str, Any]:
    title = f"{name}: data model (ER)"
    model = er_model(state)
    if not model["tables"]:
        return _diagram("er", "er", title, "No tables yet. Generate the low-level design of a database or a "
                        "service that uses one to define its tables.", tables=0)
    ids = _Ids()
    for t in model["tables"]:
        ids(t["id"], t["name"], prefix="t")
    dbs: dict[str, list[dict[str, Any]]] = {}
    for t in model["tables"]:
        dbs.setdefault(t["database"], []).append(t)
    names = {t["id"]: t["name"] for t in model["tables"]}
    alt = (f"Data model of {name}: {_count(len(model['tables']), 'table')} in {_count(len(dbs), 'database')} ("
           + "; ".join(f"{db}: {_names([t['name'] for t in ts], 6)}" for db, ts in dbs.items()) + "). "
           + (f"{_count(len(model['relations']), 'relationship')}: "
              + _names([f"{names[r['from']]} has many {names[r['to']]}" for r in model["relations"]], 6) + "."
              if model["relations"] else "No relationships between tables."))

    mm = ["erDiagram", f"    accTitle: {mermaid_acc(title)}", f"    accDescr: {mermaid_acc(alt)}"]
    pu = ["@startuml", f"title {plantuml_text(title)}", "hide circle", "skinparam linetype ortho"]
    for db, ts in dbs.items():
        pu.append(f'package "{plantuml_text(db)}" {{')
        for t in ts:
            tid = ids.map[t["id"]]
            mm.append(f'    {tid}["{mermaid_text(t["name"])}"] {{')
            pu.append(f'  entity "{plantuml_text(t["name"])}" as {tid} {{')
            keys = [c for c in t["columns"] if c["key"] == "PK"]
            for c in t["columns"]:
                typ, col = _er_word(c["type"], "text"), _er_word(c["name"], "col")
                mm.append(f"        {typ} {col}" + (f" {c['key']}" if c["key"] in ("PK", "FK", "UK") else ""))
            for c in keys:
                pu.append(f"    * {plantuml_text(c['name'])} : {plantuml_text(c['type'])} <<PK>>")
            if keys:
                pu.append("    --")
            for c in t["columns"]:
                if c["key"] != "PK":
                    mark = f" <<{c['key']}>>" if c["key"] else ""
                    pu.append(f"    {plantuml_text(c['name'])} : {plantuml_text(c['type'])}{mark}")
            mm.append("    }")
            pu.append("  }")
        pu.append("}")
    for r in model["relations"]:
        label = " ".join(re.sub(r"[^A-Za-z0-9 _./:-]", " ", r["label"]).split()) or "has"
        mm.append(f'    {ids.map[r["from"]]} ||--o{{ {ids.map[r["to"]]} : "{label}"')
        pu.append(f"{ids.map[r['from']]} ||--o{{ {ids.map[r['to']]} : {plantuml_text(label)}")
    pu.append("@enduml")
    return _diagram("er", "er", title, alt, "\n".join(mm), "\n".join(pu), tables=len(model["tables"]))


# --------------------------------------------------------------------------- critical flows (sequence)
@dataclass
class Flow:
    name: str
    path: list[str]  # node ids, entry point first
    requirement_ids: list[str] = field(default_factory=list)


def _adjacency(state: ProjectState, nodes: list[Node]) -> tuple[dict[str, list[str]], dict[tuple[str, str], str]]:
    out: dict[str, list[str]] = {n.id: [] for n in nodes}
    labels: dict[tuple[str, str], str] = {}
    for s, t, label in _top_edges(state, set(out)):
        if t not in out[s]:
            out[s].append(t)
            labels[(s, t)] = label
    return out, labels


def _entries(state: ProjectState, nodes: list[Node], out: dict[str, list[str]]) -> list[Node]:
    clients = [n for n in nodes if n.type == "client"]
    if clients:
        return clients
    called = {t for targets in out.values() for t in targets}
    return [n for n in nodes if n.id not in called and n.type not in (*DATA_TYPES, *EXTERNAL_TYPES) and out[n.id]]


def _shortest_paths(start: str, out: dict[str, list[str]], state: ProjectState) -> dict[str, list[str]]:
    """BFS from ``start`` along connections; data stores and external systems end a path."""
    paths = {start: [start]}
    frontier = [start]
    while frontier:
        nxt = []
        for nid in frontier:
            if nid != start and state.nodes[nid].type in (*STORE_TYPES, "cache", *EXTERNAL_TYPES):
                continue
            for t in out[nid]:
                if t not in paths:
                    paths[t] = paths[nid] + [t]
                    nxt.append(t)
        frontier = nxt
    return paths


def candidate_paths(state: ProjectState) -> list[list[str]]:
    """Deterministic critical paths: for each data store, the shortest path to it from an entry point (clients
    first, in canvas order); without reachable stores, the longest shortest path from each entry point."""
    nodes = top_nodes(state)
    out, _ = _adjacency(state, nodes)
    entries = _entries(state, nodes, out)
    best: dict[str, tuple[int, int, list[str]]] = {}
    reach = []
    for i, entry in enumerate(entries):
        paths = _shortest_paths(entry.id, out, state)
        reach.append(paths)
        for target, path in paths.items():
            if state.nodes[target].type in STORE_TYPES and len(path) > 1:
                key = (len(path), i)
                if target not in best or key < best[target][:2]:
                    best[target] = (len(path), i, path)
    if best:
        found = sorted(best.values(), key=lambda b: (b[1], b[0]))
        return [p for _, _, p in found][:MAX_CANDIDATES]
    result: list[list[str]] = []
    for paths in reach:
        longest = max(paths.values(), key=len)
        if len(longest) > 1 and longest not in result:
            result.append(longest)
    return result[:MAX_CANDIDATES]


def _requirement_for(state: ProjectState, path: list[str]) -> str | None:
    counts: dict[str, int] = {}
    for nid in path:
        for rid in state.nodes[nid].requirement_ids:
            if rid in state.requirements and state.requirements[rid].kind == "functional":
                counts[rid] = counts.get(rid, 0) + 1
    if not counts:
        return None
    return min(counts, key=lambda r: (-counts[r], PRIORITY_RANK.get(state.requirements[r].priority, 9), r))


def offline_flows(state: ProjectState, paths: list[list[str]] | None = None) -> list[Flow]:
    """Name each traced path after the functional requirement most of its components implement."""
    flows: list[Flow] = []
    for path in (paths if paths is not None else candidate_paths(state))[:MAX_FLOWS]:
        rid = _requirement_for(state, path)
        start, end = state.nodes[path[0]].name, state.nodes[path[-1]].name
        name = state.requirements[rid].title if rid else f"{start} to {end}"
        flows.append(Flow(name, path, [rid] if rid else []))
    names = [f.name for f in flows]
    for f in flows:
        if names.count(f.name) > 1:
            f.name = f"{f.name} ({state.nodes[f.path[-1]].name})"
    return flows


FLOWS_SCHEMA: dict[str, Any] = {
    "type": "object", "required": ["flows"],
    "properties": {"flows": {"type": "array", "maxItems": MAX_FLOWS, "items": {
        "type": "object", "required": ["name"],
        "properties": {
            "name": {"type": "string", "description": "The user goal this flow serves, e.g. 'Customer looks up an order'"},
            "candidate": {"type": "integer", "description": "Index of the traced path to use"},
            "path": {"type": "array", "items": {"type": "string"},
                     "description": "Optional: component ids from entry point to the end, each connected to the next"},
            "requirement_ids": {"type": "array", "items": {"type": "string"}},
        }}}},
}


def _flows_prompt(state: ProjectState, candidates: list[list[str]]) -> str:
    nodes = top_nodes(state)
    names = {n.id: n.name for n in nodes}
    payload = {
        "components": [{"id": n.id, "name": n.name, "type": n.type, **({"technology": n.technology} if n.technology
                                                                          else {})} for n in nodes][:80],
        "connections": [f"{s} -> {t}" + (f" ({label})" if label else "")
                        for s, t, label in _top_edges(state, set(names))][:160],
        "requirements": [{"id": r.id, "title": r.title, "description": r.description, "priority": r.priority,
                          "components": [n.id for n in nodes if r.id in n.requirement_ids]}
                         for r in state.requirements.values() if r.kind == "functional"][:40],
        "candidates": [{"index": i, "path": [names[x] for x in p], "ids": p} for i, p in enumerate(candidates)],
    }
    return json.dumps(payload, indent=1)


def normalize_flows(raw: dict[str, Any], state: ProjectState, candidates: list[list[str]]) -> list[Flow]:
    """Keep only flows whose path exists on the canvas; raises ValueError when none are usable."""
    nodes = top_nodes(state)
    out, _ = _adjacency(state, nodes)
    by_name = {n.name.lower(): n.id for n in nodes}
    flows: list[Flow] = []
    for f in (raw.get("flows") or [])[:MAX_FLOWS]:
        if not isinstance(f, dict) or not _one_line(f.get("name") or ""):
            continue
        path = [x if x in out else by_name.get(str(x).lower(), "") for x in f.get("path") or []]
        valid = len(path) > 1 and len(set(path)) == len(path) and all(
            b in out.get(a, []) for a, b in zip(path, path[1:]))
        if not valid:
            idx = f.get("candidate")
            if not isinstance(idx, int) or not 0 <= idx < len(candidates):
                continue
            path = candidates[idx]
        if any(fl.path == path for fl in flows):
            continue
        rids = [r for r in f.get("requirement_ids") or [] if r in state.requirements]
        flows.append(Flow(_one_line(f["name"])[:80], path, rids))
    if not flows:
        raise ValueError("no usable flows")
    return flows


_cache: OrderedDict[str, tuple[list[Flow], str]] = OrderedDict()
_cache_lock = threading.Lock()


def _state_key(state: ProjectState, org_id: str | None) -> str:
    data = {"n": [[n.id, n.name, n.type, n.parent_id, n.requirement_ids] for n in top_nodes(state)],
            "e": sorted([s, t, label] for s, t, label in _top_edges(state, set(state.nodes))),
            "r": [[r.id, r.title, r.description, r.kind, r.priority] for r in state.requirements.values()],
            "o": org_id}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def flows(state: ProjectState, org_id: str | None = None, ai: bool = False) -> tuple[list[Flow], str]:
    """(critical flows, engine). Offline by default; with ``ai`` and a configured model the ``diagrams.sequence``
    agent names and picks them (cached per model state, so listing and opening a diagram agree)."""
    candidates = candidate_paths(state)
    if not candidates or not ai or not llm.available(org_id):
        return offline_flows(state, candidates), "offline"
    key = _state_key(state, org_id)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    try:
        data = agents.run_json(PROMPT, FLOWS_SCHEMA, org_id=org_id,
                               variables={"project": _flows_prompt(state, candidates)},
                               question="Name the critical flows")
        result = (normalize_flows(data, state, candidates), "llm")
    except (llm.LLMUnavailable, llm.LLMRefused, ValueError, KeyError, TypeError) as exc:
        log.info("LLM flow naming failed (%s); using the traced flows", type(exc).__name__)
        return offline_flows(state, candidates), "offline"
    with _cache_lock:
        _cache[key] = result
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return result


def flow_steps(state: ProjectState, path: list[str]) -> list[dict[str, str]]:
    """Messages along a path: each hop is a call (async to or from a queue) answered on the way back; side calls
    to security, caches and LLMs connected to a hop are made when the request reaches it."""
    nodes = top_nodes(state)
    out, labels = _adjacency(state, nodes)
    on_path = set(path)
    steps: list[dict[str, str]] = []

    def visit(i: int) -> None:
        nid = path[i]
        for side in out[nid]:
            st = state.nodes[side].type
            if side not in on_path and st in SIDE_CALLS:
                label = f"{labels.get((nid, side), '')} {SIDE_CALLS[st]}".strip()
                steps.append({"from": nid, "to": side, "label": label, "kind": "call"})
                steps.append({"from": side, "to": nid, "label": "result", "kind": "return"})
        if i + 1 >= len(path):
            return
        nxt = path[i + 1]
        async_hop = "queue" in (state.nodes[nid].type, state.nodes[nxt].type)
        proto = labels.get((nid, nxt), "")
        if async_hop:
            verb = "publish" if state.nodes[nxt].type == "queue" else "deliver"
            steps.append({"from": nid, "to": nxt, "label": f"{proto} {verb}".strip(), "kind": "async"})
            visit(i + 1)
            return
        verb = VERBS.get(state.nodes[nxt].type, "")
        steps.append({"from": nid, "to": nxt, "label": f"{proto} {verb}".strip() or "request", "kind": "call"})
        terminal = i + 2 >= len(path) and not any(
            s not in on_path and state.nodes[s].type in SIDE_CALLS for s in out[nxt])
        if not terminal:
            steps.append({"from": nxt, "to": nxt, "label": "", "kind": "activate"})
        visit(i + 1)
        if not terminal:
            steps.append({"from": nxt, "to": nxt, "label": "", "kind": "deactivate"})
        done = "result" if state.nodes[nxt].type in (*DATA_TYPES, *EXTERNAL_TYPES) else "response"
        steps.append({"from": nxt, "to": nid, "label": done, "kind": "return"})

    visit(0)
    return steps


_SEQ_MERMAID = {"call": "->>", "async": "-)", "return": "-->>"}
_SEQ_PLANTUML = {"call": "->", "async": "->>", "return": "-->"}
_SEQ_PARTICIPANT = {"client": "actor", "gateway": "boundary", "database": "database", "storage": "database",
                    "cache": "database", "queue": "queue"}


def sequence(state: ProjectState, name: str, index: int, flow: Flow, engine: str) -> dict[str, Any]:
    id_ = f"sequence-{index + 1}"
    title = f"{name}: {flow.name}"
    steps = flow_steps(state, flow.path)
    order: list[str] = [flow.path[0]]  # participants in order of first appearance
    for s in steps:
        for nid in (s["from"], s["to"]):
            if nid not in order:
                order.append(nid)
    ids = _Ids()
    for nid in order:
        ids(nid, state.nodes[nid].name)
    nm = {nid: state.nodes[nid].name for nid in order}
    msgs = [s for s in steps if s["kind"] in ("call", "async")]
    reqs = [f"{r} {state.requirements[r].title}" for r in flow.requirement_ids if r in state.requirements]
    alt = (f"Sequence diagram of the flow '{flow.name}'" + (f", implementing {_names(reqs, 3)}" if reqs else "")
           + f": {_count(len(msgs), 'request')} between {_count(len(order), 'participant')}. "
           + "; ".join(f"{nm[s['from']]} {'publishes to' if s['kind'] == 'async' and state.nodes[s['to']].type == 'queue' else 'sends to' if s['kind'] == 'async' else 'calls'} {nm[s['to']]}"
                       + (f" ({s['label']})" if s["label"] else "") for s in msgs)
           + (f"; the responses return to {nm[flow.path[0]]}." if any(s["kind"] == "return" for s in steps) else "."))

    mm = ["sequenceDiagram", f"    accTitle: {mermaid_acc(title)}", f"    accDescr: {mermaid_acc(alt)}",
          "    autonumber"]
    pu = ["@startuml", f"title {plantuml_text(title)}", "autonumber"]
    for nid in order:
        node = state.nodes[nid]
        kind = "actor" if node.type == "client" else "participant"
        mm.append(f"    {kind} {ids.map[nid]} as {mermaid_text(node.name)}")
        pu.append(f'{_SEQ_PARTICIPANT.get(node.type, "participant")} "{plantuml_text(node.name)}" as {ids.map[nid]}')
    for s in steps:
        a, b = ids.map[s["from"]], ids.map[s["to"]]
        if s["kind"] in ("activate", "deactivate"):
            mm.append(f"    {s['kind']} {a}")
            pu.append(f"{s['kind']} {a}")
            continue
        mm.append(f"    {a}{_SEQ_MERMAID[s['kind']]}{b}: {mermaid_text(s['label'])}")
        pu.append(f"{a} {_SEQ_PLANTUML[s['kind']]} {b}" + (f" : {plantuml_text(s['label'])}" if s["label"] else ""))
    pu.append("@enduml")
    return _diagram(id_, "sequence", title, alt, "\n".join(mm), "\n".join(pu), flow=flow.name,
                    requirement_ids=flow.requirement_ids, path=[nm[n] for n in flow.path], engine=engine)


# --------------------------------------------------------------------------- catalog
KINDS = {"hld": "High-level architecture", "c4-context": "C4 system context", "c4-container": "C4 containers",
         "sequence": "Sequence", "er": "Data model (ER)", "deployment": "Deployment",
         "dataflow": "Data flow and trust boundaries"}


def all_diagrams(state: ProjectState, name: str, org_id: str | None = None, ai: bool = False) -> list[dict[str, Any]]:
    """Every diagram of the branch, in reading order (sequence diagrams after the containers)."""
    name = _one_line(name) or "Project"
    found, engine = flows(state, org_id, ai)
    seqs = [sequence(state, name, i, f, engine) for i, f in enumerate(found)]
    if not seqs:
        seqs = [_diagram("sequence-1", "sequence", f"{name}: request flows",
                         "No request flows yet: connect a client (or another entry point) through services to a "
                         "data store.", flow="")]
    return [hld(state, name), c4_context(state, name), c4_container(state, name), *seqs, er(state, name),
            deployment(state, name), dataflow(state, name)]


def summary(d: dict[str, Any]) -> dict[str, Any]:
    """A diagram without its sources (for listings)."""
    return {k: v for k, v in d.items() if k not in ("mermaid", "plantuml")}


def get(state: ProjectState, name: str, diagram_id: str, org_id: str | None = None,
        ai: bool = False) -> dict[str, Any] | None:
    return next((d for d in all_diagrams(state, name, org_id, ai) if d["id"] == diagram_id), None)
