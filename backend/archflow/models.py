"""Core project model.

Everything on the canvas and in the requirements list is a structured object with a
revision counter. Every mutation — from a human dragging a node, the AI, or an external
MCP agent — goes through the same operation pipeline in ``ops.py``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

NodeType = Literal[
    "client",
    "gateway",
    "service",
    "database",
    "cache",
    "queue",
    "storage",
    "agent",
    "llm",
    "mcp",
    "external",
    "security",
    "note",
    # Low-level design (C4 levels 3-4): elements inside an HLD component's own diagram.
    "component",  # internal module / class group
    "endpoint",   # API operation; props: method, path
    "table",      # database table; props: columns ("name type[ pk|fk]" comma-separated)
    "event",      # domain event; props: channel
]
NODE_TYPES: tuple[str, ...] = NodeType.__args__  # type: ignore[attr-defined]
LLD_TYPES: tuple[str, ...] = ("component", "endpoint", "table", "event")

Priority = Literal["low", "medium", "high", "critical"]
ReqKind = Literal["functional", "non_functional"]
ReqStatus = Literal["draft", "review", "approved", "implemented"]


class Position(BaseModel):
    x: float = 0
    y: float = 0


class Versioned(BaseModel):
    """Revision bookkeeping used for field-level conflict detection."""

    rev: int = 0
    # field name -> project revision at which that field last changed
    field_revs: dict[str, int] = Field(default_factory=dict)


class Node(Versioned):
    id: str
    type: NodeType = "service"
    name: str
    technology: str = ""
    vendor: str = ""  # catalog vendor: aws, azure, gcp, oss, saas, generic (empty = unspecified)
    catalog_id: str = ""  # catalog entry this component was created from, e.g. "aws-lambda"
    description: str = ""
    position: Position = Field(default_factory=Position)
    requirement_ids: list[str] = Field(default_factory=list)
    props: dict[str, str] = Field(default_factory=dict)
    # Diagram level: "" = the top-level (HLD) diagram; otherwise the id of the component whose internal
    # (LLD) diagram this element belongs to. Connections only join elements of the same diagram.
    parent_id: str = ""


class Edge(Versioned):
    id: str
    source: str
    target: str
    protocol: str = ""
    label: str = ""


class Requirement(Versioned):
    id: str
    title: str
    description: str = ""
    kind: ReqKind = "functional"
    category: str = ""  # NFR category: performance, security, availability, ...
    priority: Priority = "medium"
    actors: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    status: ReqStatus = "draft"
    source: dict[str, str] = Field(default_factory=dict)  # provenance: type, ref, url, at
    deleted: bool = False


class ProjectState(BaseModel):
    """The whole editable model of one branch."""

    rev: int = 0
    nodes: dict[str, Node] = Field(default_factory=dict)
    edges: dict[str, Edge] = Field(default_factory=dict)
    requirements: dict[str, Requirement] = Field(default_factory=dict)
    # Highest REQ-n ever issued on this branch. It never goes down, so a permanently removed requirement's id is not
    # handed out again: its history rows and Jira/GitHub links are keyed by that id and would attach to the new one.
    requirement_seq: int = 0


def requirement_number(rid: str) -> int:
    """n for "REQ-n", else 0."""
    head, _, num = rid.partition("-")
    return int(num) if head == "REQ" and num.isdigit() else 0


def keep_requirement_seq(new: ProjectState, *previous: ProjectState) -> ProjectState:
    """Set ``new.requirement_seq`` to the highest of its own, ``previous`` states' and every id present (in place).
    For code that replaces a branch's state wholesale (restore, adopt, merge, the collab projection)."""
    new.requirement_seq = max([new.requirement_seq, *(p.requirement_seq for p in previous),
                               *(requirement_number(r) for s in (new, *previous) for r in s.requirements)], default=0)
    return new


# Fields that clients may not set directly through an update op.
PROTECTED_FIELDS = {"id", "rev", "field_revs"}


# Soft delete: a removed requirement stays in the state with ``deleted=True`` (its links kept) so it can be restored.
# Everything that reads requirements — analysis, AI context, exports, MCP — sees only the active ones.
def active_requirements(state: ProjectState) -> dict[str, Requirement]:
    return {rid: r for rid, r in state.requirements.items() if not r.deleted}


def without_deleted(state: ProjectState) -> ProjectState:
    """``state`` without soft-deleted requirements or the components' links to them (``state`` itself if none)."""
    gone = {rid for rid, r in state.requirements.items() if r.deleted}
    if not gone:
        return state
    out = state.model_copy(deep=True)
    for rid in gone:
        del out.requirements[rid]
    for node in out.nodes.values():
        node.requirement_ids = [r for r in node.requirement_ids if r not in gone]
    return out
