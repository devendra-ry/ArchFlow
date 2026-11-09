"""The single mutation pipeline for a project branch.

Humans (canvas UI), the AI command endpoint and MCP agents all submit lists of
operations. A batch is applied atomically: either every op succeeds or nothing changes.

Operation shapes::

    {"op": "add_node", "node": {...Node fields...}}
    {"op": "update_node", "id": "<id or name>", "changes": {...}, "base_rev": 12}
    {"op": "remove_node", "id": "<id or name>"}   # also removes its LLD children, recursively
    {"op": "add_edge", "edge": {"source": ..., "target": ..., "protocol": ...}}
    {"op": "update_edge", "id": ..., "changes": {...}, "base_rev": 12}
    {"op": "remove_edge", "id": ...}   # or {"op": "remove_edge", "source": ..., "target": ...}
    {"op": "add_requirement", "requirement": {...}}
    {"op": "update_requirement", "id": "REQ-001", "changes": {...}, "base_rev": 12}
    {"op": "remove_requirement", "id": "REQ-001"}    # soft delete: marks it deleted, keeps its links
    {"op": "restore_requirement", "id": "REQ-001"}   # undoes a soft delete
    {"op": "purge_requirement", "id": "REQ-001"}     # hard delete of a deleted requirement (admins; see main.py)
    {"op": "mark_reviewed", "id": "<node id or name>", "requirement_id": "REQ-001"}  # omit requirement_id: all

``base_rev`` is the project revision the client last saw. If a field in ``changes`` was
modified by someone else after that revision (and to a different value), the batch is
rejected with a :class:`ConflictError` instead of silently overwriting.

Stale links: a component's link to a requirement is stale when the requirement changed after the component did
(see ``analysis.stale_links``). ``mark_reviewed`` records that someone checked the component still fits, as the
bookkeeping entry ``field_revs["reviewed:<requirement id>"]`` on the node.

Diagram levels (C4 drill-down): a node's ``parent_id`` names the component whose internal diagram it belongs
to ("" = top level). Connections must join two nodes of the same diagram.
"""

from __future__ import annotations

import re
import secrets
from typing import Any

from pydantic import ValidationError

from .models import PROTECTED_FIELDS, Edge, Node, ProjectState, Requirement, requirement_number

REVIEWED = "reviewed:"  # node.field_revs key prefix: revision at which a linked requirement was last reviewed

# Moving things around is last-writer-wins; everything else is conflict-checked.
NO_CONFLICT_FIELDS = {"position"}


class OpError(ValueError):
    pass


class ConflictError(Exception):
    def __init__(self, conflicts: list[dict[str, Any]]):
        super().__init__(f"{len(conflicts)} conflicting change(s)")
        self.conflicts = conflicts


def slugify(text: str, max_len: int = 40) -> str:
    """Lower-case, hyphen-separated slug of at most ``max_len`` characters, cut at a word boundary."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > max_len:
        cut = slug[:max_len + 1]
        # Prefer the last hyphen (a word boundary); a single over-long word is cut hard.
        slug = cut[:cut.rfind("-")] if cut.rfind("-") > 0 else slug[:max_len]
        slug = slug.strip("-")
    return slug or "item"


def unique_slug(text: str, taken, max_len: int = 40, prefix: str = "") -> str:
    """``prefix + slugify(text)``, made unique against ``taken`` (a container or a predicate) by appending -2, -3…

    The suffix never pushes the slug past ``max_len``: the base is re-cut at a word boundary to make room."""
    exists = taken if callable(taken) else (lambda name: name in taken)
    base = slugify(text, max_len)
    candidate, i = f"{prefix}{base}", 2
    while exists(candidate):
        suffix = f"-{i}"
        candidate, i = f"{prefix}{slugify(base, max_len - len(suffix))}{suffix}", i + 1
    return candidate


def new_node_id(state: ProjectState, name: str) -> str:
    base = slugify(name)
    candidate = base
    while candidate in state.nodes:
        candidate = f"{base}-{secrets.token_hex(2)}"
    return candidate


def new_edge_id(state: ProjectState) -> str:
    while True:
        candidate = f"e-{secrets.token_hex(4)}"
        if candidate not in state.edges:
            return candidate


def next_requirement_id(state: ProjectState) -> str:
    """The next REQ-n, past every id present and every id ever issued (``requirement_seq``), so a purged id is never
    reused."""
    highest = max([state.requirement_seq, *(requirement_number(rid) for rid in state.requirements)])
    return f"REQ-{highest + 1:03d}"


def resolve_node(state: ProjectState, ref: str | None) -> Node | None:
    """Find a node by id, or by case-insensitive name (convenient for AI and MCP callers)."""
    if not ref:
        return None
    if ref in state.nodes:
        return state.nodes[ref]
    wanted = ref.strip().lower()
    for node in state.nodes.values():
        if node.name.lower() == wanted:
            return node
    slug = slugify(ref)
    for node in state.nodes.values():
        if slugify(node.name) == slug:
            return node
    return None


def resolve_requirement(state: ProjectState, ref: str | None) -> Requirement | None:
    if not ref:
        return None
    if ref in state.requirements:
        return state.requirements[ref]
    wanted = ref.strip().lower()
    # active requirements first, so a title shared with a deleted one finds the live one
    for req in sorted(state.requirements.values(), key=lambda r: r.deleted):
        if req.id.lower() == wanted or req.title.lower() == wanted:
            return req
    return None


def _auto_position(state: ProjectState, parent_id: str = "") -> dict[str, float]:
    level = [n for n in state.nodes.values() if n.parent_id == parent_id]
    if not level:
        return {"x": 0, "y": 0}
    xs = [n.position.x for n in level]
    ys = [n.position.y for n in level]
    return {"x": (min(xs) + max(xs)) / 2, "y": max(ys) + 160}


BOX_W, BOX_H = 210, 110  # a component on the canvas (190 x ~90) plus a margin


def _free_position(state: ProjectState, parent_id: str, pos: dict[str, float]) -> dict[str, float]:
    """``pos`` if no component on the same diagram level is there, else the nearest free spot to the right, then
    on the rows below (AI agents guess coordinates and often land on existing components)."""
    level = [n.position for n in state.nodes.values() if n.parent_id == parent_id]

    def free(x: float, y: float) -> bool:
        return all(abs(p.x - x) >= BOX_W or abs(p.y - y) >= BOX_H for p in level)

    x0, y0 = float(pos.get("x", 0)), float(pos.get("y", 0))
    if free(x0, y0):
        return {"x": x0, "y": y0}
    for row in range(0, 8):
        for col in range(0, 8):
            for x in ((x0 + col * (BOX_W + 30), x0 - col * (BOX_W + 30)) if col else (x0,)):
                y = y0 + row * (BOX_H + 40)
                if free(x, y):
                    return {"x": x, "y": y}
    return {"x": x0, "y": max(p.y for p in level) + BOX_H + 40}


def descendants(state: ProjectState, node_id: str) -> list[str]:
    """Ids of every node inside ``node_id``'s LLD diagram, recursively (not including ``node_id``)."""
    children: dict[str, list[str]] = {}
    for n in state.nodes.values():
        if n.parent_id:
            children.setdefault(n.parent_id, []).append(n.id)
    out: list[str] = []
    seen = {node_id}
    stack = list(children.get(node_id, []))
    while stack:
        nid = stack.pop()
        if nid in seen:
            continue
        seen.add(nid)
        out.append(nid)
        stack.extend(children.get(nid, []))
    return out


def _diagram_label(state: ProjectState, parent_id: str) -> str:
    parent = state.nodes.get(parent_id) if parent_id else None
    return f"the internals of '{parent.name}'" if parent else "the top-level diagram"


def _check_same_diagram(state: ProjectState, src: Node, tgt: Node) -> None:
    if src.parent_id != tgt.parent_id:
        raise OpError(f"cannot connect '{src.name}' → '{tgt.name}': they are in different diagrams "
                      f"({_diagram_label(state, src.parent_id)} vs {_diagram_label(state, tgt.parent_id)}). "
                      "Connections must stay within one diagram level.")


def _check_parent(state: ProjectState, node_id: str, parent_id: str) -> None:
    if not parent_id:
        return
    if parent_id not in state.nodes:
        raise OpError(f"parent component '{parent_id}' not found")
    if parent_id == node_id or parent_id in descendants(state, node_id):
        raise OpError("a component cannot be placed inside itself")


def _apply_changes(entity, changes: dict[str, Any], base_rev: int | None, new_rev: int,
                   kind: str, conflicts: list[dict[str, Any]]):
    """Validate and apply field changes to a copy of ``entity``; record conflicts."""
    data = entity.model_dump()
    changed_fields = []
    for field, value in changes.items():
        if field in PROTECTED_FIELDS:
            continue
        if field not in data:
            raise OpError(f"unknown field '{field}' on {kind}")
        if data[field] == value:
            continue
        last = entity.field_revs.get(field, 0)
        if base_rev is not None and last > base_rev and field not in NO_CONFLICT_FIELDS:
            conflicts.append({
                "kind": kind, "id": entity.id, "name": getattr(entity, "name", None) or getattr(entity, "title", None),
                "field": field, "theirs": data[field], "yours": value,
                "base_rev": base_rev, "changed_at_rev": last,
            })
            continue
        data[field] = value
        changed_fields.append(field)
    try:
        updated = type(entity).model_validate(data)
    except ValidationError as exc:
        raise OpError(f"invalid {kind} update: {exc.errors()[0]['msg']}") from exc
    for field in changed_fields:
        updated.field_revs[field] = new_rev
    if changed_fields:
        updated.rev = new_rev
    return updated, changed_fields


def apply_ops(state: ProjectState, ops: list[dict[str, Any]]) -> tuple[ProjectState, list[str]]:
    """Apply a batch atomically. Returns the new state and human-readable change summaries."""
    if not ops:
        return state, []
    st = state.model_copy(deep=True)
    new_rev = st.rev + 1
    conflicts: list[dict[str, Any]] = []
    summary: list[str] = []

    for op in ops:
        kind = op.get("op")
        if kind == "add_node":
            raw = dict(op.get("node") or {})
            if not raw.get("name"):
                raise OpError("add_node requires a name")
            if raw.get("id") in st.nodes:
                raise OpError(f"node '{raw['id']}' already exists")
            if not raw.get("id"):
                raw["id"] = new_node_id(st, raw["name"])
            parent = resolve_node(st, raw["parent_id"]) if raw.get("parent_id") else None
            if raw.get("parent_id") and not parent:
                raise OpError(f"parent component '{raw['parent_id']}' not found")
            raw["parent_id"] = parent.id if parent else ""
            if not raw.get("position"):
                raw["position"] = _auto_position(st, raw["parent_id"])
            if isinstance(raw["position"], dict) and raw.get("type") != "note":
                raw["position"] = _free_position(st, raw["parent_id"], raw["position"])
            raw["rev"] = new_rev
            try:
                node = Node.model_validate(raw)
            except ValidationError as exc:
                raise OpError(f"invalid node: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}") from exc
            node.field_revs = {f: new_rev for f in Node.model_fields if f not in PROTECTED_FIELDS}
            st.nodes[node.id] = node
            summary.append(f"added {node.type} '{node.name}'")

        elif kind == "update_node":
            node = resolve_node(st, op.get("id"))
            if not node:
                raise OpError(f"node '{op.get('id')}' not found")
            changes = dict(op.get("changes") or {})
            if changes.get("parent_id"):
                parent = resolve_node(st, changes["parent_id"])
                changes["parent_id"] = parent.id if parent else changes["parent_id"]
            if isinstance(changes.get("requirement_ids"), list):
                # Callers see only active requirements; keep links to deleted ones so a restore brings them back.
                hidden = [r for r in node.requirement_ids if r in st.requirements and st.requirements[r].deleted]
                changes["requirement_ids"] = [*changes["requirement_ids"], *(r for r in hidden if r not in changes["requirement_ids"])]
            updated, fields = _apply_changes(node, changes, op.get("base_rev"), new_rev, "node", conflicts)
            if "parent_id" in fields:
                _check_parent(st, node.id, updated.parent_id)
                if any(node.id in (e.source, e.target) for e in st.edges.values()):
                    raise OpError(f"'{node.name}' has connections; remove them before moving it to another diagram")
            st.nodes[node.id] = updated
            if fields and fields != ["position"]:
                summary.append(f"updated '{updated.name}' ({', '.join(fields)})")
            elif fields:
                summary.append(f"moved '{updated.name}'")

        elif kind == "remove_node":
            node = resolve_node(st, op.get("id"))
            if not node:
                raise OpError(f"node '{op.get('id')}' not found")
            gone = {node.id, *descendants(st, node.id)}
            for nid in gone:
                del st.nodes[nid]
            dropped = [eid for eid, e in st.edges.items() if e.source in gone or e.target in gone]
            for eid in dropped:
                del st.edges[eid]
            inner = len(gone) - 1
            summary.append(f"removed '{node.name}'" + (f" with {inner} internal element(s)" if inner else "")
                           + (f" and {len(dropped)} connection(s)" if dropped else ""))

        elif kind == "add_edge":
            raw = dict(op.get("edge") or {})
            src, tgt = resolve_node(st, raw.get("source")), resolve_node(st, raw.get("target"))
            if not src or not tgt:
                raise OpError(f"cannot connect '{raw.get('source')}' → '{raw.get('target')}': node not found")
            _check_same_diagram(st, src, tgt)
            if any(e.source == src.id and e.target == tgt.id for e in st.edges.values()):
                continue  # idempotent
            raw.update(source=src.id, target=tgt.id, rev=new_rev)
            if not raw.get("id") or raw["id"] in st.edges:
                raw["id"] = new_edge_id(st)
            try:
                edge = Edge.model_validate(raw)
            except ValidationError as exc:
                raise OpError(f"invalid connection: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}") from exc
            edge.field_revs = {f: new_rev for f in Edge.model_fields if f not in PROTECTED_FIELDS}
            st.edges[edge.id] = edge
            summary.append(f"connected '{src.name}' → '{tgt.name}'" + (f" ({edge.protocol})" if edge.protocol else ""))

        elif kind == "update_edge":
            edge = st.edges.get(op.get("id", ""))
            if not edge:
                raise OpError(f"edge '{op.get('id')}' not found")
            changes = dict(op.get("changes") or {})
            for end in ("source", "target"):
                if end in changes:
                    n = resolve_node(st, changes[end])
                    if not n:
                        raise OpError(f"node '{changes[end]}' not found")
                    changes[end] = n.id
            src = st.nodes.get(changes.get("source", edge.source))
            tgt = st.nodes.get(changes.get("target", edge.target))
            if src and tgt:
                _check_same_diagram(st, src, tgt)
            updated, fields = _apply_changes(edge, changes, op.get("base_rev"), new_rev, "edge", conflicts)
            st.edges[edge.id] = updated
            if fields:
                summary.append(f"updated connection {edge.id} ({', '.join(fields)})")

        elif kind == "remove_edge":
            if op.get("id"):
                ids = [op["id"]] if op["id"] in st.edges else []
            else:
                src, tgt = resolve_node(st, op.get("source")), resolve_node(st, op.get("target"))
                ids = [eid for eid, e in st.edges.items()
                       if src and tgt and e.source == src.id and e.target == tgt.id]
            if not ids:
                raise OpError("connection not found")
            for eid in ids:
                e = st.edges.pop(eid)
                summary.append(f"disconnected '{st.nodes[e.source].name}' → '{st.nodes[e.target].name}'")

        elif kind == "add_requirement":
            raw = dict(op.get("requirement") or {})
            if not raw.get("title"):
                raise OpError("add_requirement requires a title")
            if not raw.get("id") or raw["id"] in st.requirements:
                raw["id"] = next_requirement_id(st)
            raw["rev"] = new_rev
            try:
                req = Requirement.model_validate(raw)
            except ValidationError as exc:
                raise OpError(f"invalid requirement: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}") from exc
            req.field_revs = {f: new_rev for f in Requirement.model_fields if f not in PROTECTED_FIELDS}
            st.requirements[req.id] = req
            st.requirement_seq = max(st.requirement_seq, requirement_number(req.id))
            summary.append(f"added {req.id} '{req.title}'")

        elif kind == "update_requirement":
            req = resolve_requirement(st, op.get("id"))
            if not req:
                raise OpError(f"requirement '{op.get('id')}' not found")
            updated, fields = _apply_changes(req, op.get("changes") or {}, op.get("base_rev"), new_rev, "requirement", conflicts)
            st.requirements[req.id] = updated
            if fields:
                summary.append(f"updated {req.id} ({', '.join(fields)})")

        elif kind in ("remove_requirement", "restore_requirement"):
            req = resolve_requirement(st, op.get("id"))
            if not req:
                raise OpError(f"requirement '{op.get('id')}' not found")
            deleted = kind == "remove_requirement"
            if req.deleted != deleted:
                req.deleted = deleted
                req.field_revs["deleted"] = new_rev
                req.rev = new_rev
                summary.append(f"{'removed' if deleted else 'restored'} {req.id} '{req.title}'")

        elif kind == "purge_requirement":
            req = resolve_requirement(st, op.get("id"))
            if not req:
                raise OpError(f"requirement '{op.get('id')}' not found")
            if not req.deleted:
                raise OpError(f"{req.id} is not deleted; delete it before removing it permanently")
            del st.requirements[req.id]
            for node in st.nodes.values():
                if req.id in node.requirement_ids:
                    node.requirement_ids = [r for r in node.requirement_ids if r != req.id]
                    node.field_revs["requirement_ids"] = new_rev
                    node.rev = new_rev
                node.field_revs.pop(REVIEWED + req.id, None)
            summary.append(f"permanently removed {req.id} '{req.title}'")

        elif kind == "mark_reviewed":
            node = resolve_node(st, op.get("id"))
            if not node:
                raise OpError(f"node '{op.get('id')}' not found")
            rids = [op["requirement_id"]] if op.get("requirement_id") else list(node.requirement_ids)
            for rid in rids:
                if rid not in node.requirement_ids:
                    raise OpError(f"'{node.name}' is not linked to {rid}")
                node.field_revs[REVIEWED + rid] = new_rev
            if rids:
                summary.append(f"reviewed '{node.name}' against {', '.join(rids)}")

        else:
            raise OpError(f"unknown op '{kind}'")

    if conflicts:
        raise ConflictError(conflicts)
    st.rev = new_rev
    return st, summary
