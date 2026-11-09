"""Branch merge: a field-level three-way merge of one branch (*theirs*, the source) into another (*ours*, the target).

The merge base is the newest version both branches descend from. Versions form a graph through ``versions.parent``
(the previous version on the branch, or the version the branch was created from) plus the merge points recorded
here: applying a merge saves a version on the target whose message is ``Merged <source> (vN) into <target>``, and
vN counts as a second parent, so the next merge of the same branches starts from there. Without a common ancestor
the base is the version the source branch was created from (and an empty project if it has none).

Per component, connection and requirement, and per field:
- changed on one side only → that side's value is taken (auto-merged);
- changed on both sides to the same value → no conflict;
- changed on both sides differently → a **conflict** (``props`` is merged key by key, so two people editing
  different properties do not conflict);
- list fields (requirement links, actors, acceptance criteria) merge as sets and never conflict;
- ``position`` never conflicts: if both sides moved a component, the target's layout wins;
- deleted on one side and modified on the other → a **conflict** (keep it, or delete it);
- a requirement both sides added under the same id with different titles is renumbered, not merged.

After conflicts are resolved (``ours`` / ``theirs`` / ``custom`` value), the result is made consistent: elements
inside a removed component, and connections whose ends were removed or now sit in different diagrams, are dropped
and reported. The merged state is then turned into ordinary ops (:func:`plan_ops`) and applied by ``ops.apply_ops``
through the API's ``commit_ops``, so it is validated, logged, attributed and pushed to the live document like any
other change. A safety version of the target is saved first, as restore and adopt do.

Optional AI help: the ``merge.resolve`` agent proposes a resolution per conflict, with a rationale; the person still
picks. With no AI configured there is no suggestion.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, ValidationError

from . import agents, llm
from .collab_sync import state_patch
from .diff import diff_states, diff_to_text
from .models import Edge, Node, ProjectState, Requirement, keep_requirement_seq
from .ops import next_requirement_id

log = logging.getLogger("archflow.merge")

PROMPT = "merge.resolve"
BRANCH_PATTERN = r"^[A-Za-z0-9._/-]{1,60}$"
MODELS = {"nodes": Node, "edges": Edge, "requirements": Requirement}
KIND = {"nodes": "component", "edges": "connection", "requirements": "requirement"}
IGNORED = {"id", "rev", "field_revs"}
LIST_FIELDS = {"requirement_ids", "actors", "acceptance_criteria"}  # merged as sets, order kept
DICT_FIELDS = {"props"}  # merged key by key
LAYOUT_FIELDS = {"position"}  # never a conflict: the target's layout wins
MERGED_RE = re.compile(r"^Merged (\S+) \(v(\d+)\) into (\S+)$")
CHOICES = ("ours", "theirs", "custom")


class MergeError(ValueError):
    pass


class StaleMerge(MergeError):
    """A branch changed between the preview the person resolved and the apply."""


# --------------------------------------------------------------------------- merge base
def merge_message(source: str, source_version: int, target: str) -> str:
    return f"Merged {source} (v{source_version}) into {target}"


def _parents(versions: dict[int, dict[str, Any]], number: int) -> list[int]:
    v = versions.get(number)
    if not v:
        return []
    out = [v["parent"]] if v.get("parent") is not None else []
    m = MERGED_RE.match(v.get("message") or "")
    if m and m.group(3) == v["branch"]:
        out.append(int(m.group(2)))
    return out


def ancestors(versions: dict[int, dict[str, Any]], start: int | None) -> set[int]:
    """``start`` and every version it descends from (through parents and recorded merge points)."""
    seen: set[int] = set()
    stack = [start] if start is not None else []
    while stack:
        n = stack.pop()
        if n in seen or n not in versions:
            continue
        seen.add(n)
        stack.extend(_parents(versions, n))
    return seen


def branch_head(store: Any, pid: str, branch: str) -> int | None:
    """The branch's newest version, else the version it was created from."""
    head = store.head_version(pid, branch)
    if head is not None:
        return head
    return next((b["base_version"] for b in store.list_branches(pid) if b["name"] == branch), None)


def find_base(store: Any, pid: str, source: str, target: str) -> tuple[int | None, ProjectState, str]:
    """(version number, state, how it was found): ``common-ancestor``, ``branch-point`` or ``none``."""
    versions = {v["number"]: v for v in store.list_versions(pid)}
    common = ancestors(versions, branch_head(store, pid, source)) & ancestors(versions, branch_head(store, pid, target))
    if common:
        n = max(common)
        return n, store.get_version(pid, n)["state"], "common-ancestor"
    created_from = next((b["base_version"] for b in store.list_branches(pid) if b["name"] == source), None)
    if created_from is not None and (v := store.get_version(pid, created_from)):
        return created_from, v["state"], "branch-point"
    return None, ProjectState(), "none"


# --------------------------------------------------------------------------- three-way merge
@dataclass
class MergeResult:
    state: ProjectState
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    dropped: list[dict[str, str]] = field(default_factory=list)
    renumbered: list[dict[str, str]] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)


def _fields(entity: Any) -> dict[str, Any]:
    return entity.model_dump(exclude=IGNORED)


def _label(coll: str, f: dict[str, Any] | None, names: dict[str, str]) -> str:
    if not f:
        return ""
    if coll == "nodes":
        return f.get("name") or ""
    if coll == "requirements":
        return f.get("title") or ""
    return f"{names.get(f.get('source'), f.get('source'))} → {names.get(f.get('target'), f.get('target'))}"


def _same(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Equal apart from layout (moving a component is not a modification for delete-vs-modify)."""
    def strip(x: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in x.items() if k not in LAYOUT_FIELDS}

    return strip(a) == strip(b)


def _merge_list(base: list[Any], ours: list[Any], theirs: list[Any]) -> list[Any]:
    out = [x for x in ours if x in theirs or x not in base]
    return out + [x for x in theirs if x not in base and x not in out]


def _three_way(name: str, b: Any, o: Any, t: Any) -> tuple[Any, bool]:
    """(merged value, conflict?) for one field; on conflict the value is ours."""
    if o == t:
        return o, False
    if b == o:
        return t, False
    if b == t:
        return o, False
    if name in LAYOUT_FIELDS:
        return o, False
    if name in LIST_FIELDS:
        return _merge_list(b or [], o or [], t or []), False
    return o, True


class _Merger:
    def __init__(self, base: ProjectState, ours: ProjectState, theirs: ProjectState,
                 resolutions: dict[str, dict[str, Any]] | None) -> None:
        self.base, self.ours, self.theirs = base, ours, theirs
        self.res = resolutions or {}
        self.names = {**{k: n.name for k, n in base.nodes.items()}, **{k: n.name for k, n in theirs.nodes.items()},
                      **{k: n.name for k, n in ours.nodes.items()}}
        self.out = MergeResult(state=keep_requirement_seq(ProjectState(rev=ours.rev), base, ours, theirs))

    # -- resolutions
    def _pick(self, cid: str, ours: Any, theirs: Any, allow_custom: bool = True) -> Any:
        r = self.res.get(cid)
        if not r:
            self.out.unresolved.append(cid)
            return ours
        choice = r.get("choice")
        if choice == "theirs":
            return theirs
        if choice == "custom" and allow_custom:
            return r.get("value")
        if choice != "ours":
            raise MergeError(f"invalid resolution '{choice}' for {cid}")
        return ours

    def _conflict(self, coll: str, eid: str, fld: str, kind: str, b: Any, o: Any, t: Any, name: str) -> str:
        cid = f"{coll}/{eid}" + (f"/{fld}" if fld else "")
        self.out.conflicts.append({"id": cid, "collection": coll, "entity_id": eid, "entity": KIND[coll],
                                   "name": name, "field": fld, "kind": kind, "base": b, "ours": o, "theirs": t})
        return cid

    # -- one entity present on both sides
    def _entity(self, coll: str, eid: str, b: dict[str, Any] | None, o: dict[str, Any], t: dict[str, Any]) -> dict:
        name = _label(coll, o, self.names) or _label(coll, t, self.names)
        out: dict[str, Any] = {}
        for f in list(o) + [k for k in t if k not in o]:
            bv = b.get(f) if b else None
            ov, tv = o.get(f), t.get(f)
            if f in DICT_FIELDS:
                bd, od, td = bv or {}, ov or {}, tv or {}
                merged: dict[str, Any] = {}
                for key in list(od) + [k for k in td if k not in od]:
                    v, conflict = _three_way(key, bd.get(key), od.get(key), td.get(key))
                    if conflict:
                        cid = self._conflict(coll, eid, f"{f}.{key}", "field", bd.get(key), od.get(key), td.get(key),
                                             name)
                        v = self._pick(cid, od.get(key), td.get(key))
                    if v is not None:
                        merged[key] = v
                out[f] = merged
                continue
            v, conflict = _three_way(f, bv, ov, tv)
            if conflict:
                cid = self._conflict(coll, eid, f, "field", bv, ov, tv, name)
                v = self._pick(cid, ov, tv)
            out[f] = v
        return out

    def _collection(self, coll: str, theirs: ProjectState) -> dict[str, dict[str, Any]]:
        in_base, in_ours, in_theirs = getattr(self.base, coll), getattr(self.ours, coll), getattr(theirs, coll)
        result: dict[str, dict[str, Any]] = {}
        for eid in (list(in_ours) + [k for k in in_theirs if k not in in_ours]
                    + [k for k in in_base if k not in in_ours and k not in in_theirs]):
            b = _fields(in_base[eid]) if eid in in_base else None
            o = _fields(in_ours[eid]) if eid in in_ours else None
            t = _fields(in_theirs[eid]) if eid in in_theirs else None
            if o is not None and t is not None:
                result[eid] = self._entity(coll, eid, b, o, t)
            elif b is None:  # added on one side only
                result[eid] = o if o is not None else t
            elif o is None and t is None:  # deleted on both sides
                continue
            elif o is None:  # we deleted it
                if _same(b, t):
                    continue
                name = _label(coll, t, self.names)
                cid = self._conflict(coll, eid, "", "deleted-in-ours", b, None, t, name)
                if self._pick(cid, "ours", "theirs", allow_custom=False) == "theirs":
                    result[eid] = t
            else:  # they deleted it
                if _same(b, o):
                    continue
                name = _label(coll, o, self.names)
                cid = self._conflict(coll, eid, "", "deleted-in-theirs", b, o, None, name)
                if self._pick(cid, "ours", "theirs", allow_custom=False) == "ours":
                    result[eid] = o
        return result

    # -- requirement ids both sides added
    def _renumber(self) -> ProjectState:
        theirs = self.theirs.model_copy(deep=True)
        taken = keep_requirement_seq(
            ProjectState(requirements={**self.base.requirements, **self.ours.requirements, **theirs.requirements}),
            self.base, self.ours, theirs)
        renames: dict[str, str] = {}
        for rid, req in list(theirs.requirements.items()):
            mine = self.ours.requirements.get(rid)
            if rid in self.base.requirements or mine is None or mine.title.strip().lower() == req.title.strip().lower():
                continue
            new_id = next_requirement_id(taken)
            taken.requirements[new_id] = req
            renames[rid] = new_id
            self.out.renumbered.append({"from": rid, "to": new_id, "title": req.title})
        for old, new in renames.items():
            req = theirs.requirements.pop(old)
            req.id = new
            theirs.requirements[new] = req
        for node in theirs.nodes.values():  # their links meant their requirement
            node.requirement_ids = [renames.get(r, r) for r in node.requirement_ids]
        return theirs

    # -- consistency of the merged model
    def _drop(self, coll: str, eid: str, name: str, reason: str) -> None:
        self.out.dropped.append({"collection": coll, "id": eid, "entity": KIND[coll], "name": name, "reason": reason})

    def _tidy(self, raw: dict[str, dict[str, dict[str, Any]]]) -> None:
        nodes, edges, reqs = raw["nodes"], raw["edges"], raw["requirements"]
        # elements inside a component that no longer exists go with it (as removing a component does)
        changed = True
        while changed:
            changed = False
            for nid, n in list(nodes.items()):
                if n.get("parent_id") and n["parent_id"] not in nodes:
                    del nodes[nid]
                    self._drop("nodes", nid, n.get("name", nid), "the component it belongs to was removed")
                    changed = True
        # both sides re-parented into each other: keep the target's placement
        for nid, n in nodes.items():
            seen, p = {nid}, n.get("parent_id")
            while p and p in nodes and p not in seen:
                seen.add(p)
                p = nodes[p].get("parent_id")
            if p and p in seen:
                n["parent_id"] = self.ours.nodes[nid].parent_id if nid in self.ours.nodes else ""
        for n in nodes.values():
            n["requirement_ids"] = [r for r in n.get("requirement_ids") or [] if r in reqs]
        pairs: set[tuple[str, str]] = set()
        for eid, e in list(edges.items()):
            label = _label("edges", e, self.names)
            src, tgt = nodes.get(e.get("source")), nodes.get(e.get("target"))
            if src is None or tgt is None:
                reason = "a component it connects was removed"
            elif src.get("parent_id", "") != tgt.get("parent_id", ""):
                reason = "its components are now in different diagrams"
            elif (e["source"], e["target"]) in pairs:
                reason = "the same connection already exists"
            else:
                pairs.add((e["source"], e["target"]))
                continue
            del edges[eid]
            self._drop("edges", eid, label, reason)

    def _relink(self, raw: dict[str, dict[str, dict[str, Any]]], theirs: ProjectState) -> None:
        """A requirement one side deleted but the merge keeps also keeps the links that deletion removed."""
        for rid in raw["requirements"]:
            if rid not in self.base.requirements:
                continue
            kept_by = (self.ours if rid not in theirs.requirements
                       else theirs if rid not in self.ours.requirements else None)
            if kept_by is None:
                continue
            for nid, n in raw["nodes"].items():
                node = kept_by.nodes.get(nid)
                if node and rid in node.requirement_ids and rid not in n.get("requirement_ids", []):
                    n["requirement_ids"] = [*n.get("requirement_ids", []), rid]

    def _revive(self, raw: dict[str, dict[str, dict[str, Any]]], theirs: ProjectState) -> None:
        """A component one side deleted but the merge keeps also keeps what that deletion took with it: its
        internal elements and its connections (as the side that kept it has them)."""
        kept: dict[str, tuple[ProjectState, ProjectState]] = {}  # node id → (side that kept it, side that deleted it)
        for c in self.out.conflicts:
            if c["collection"] == "nodes" and c["kind"] != "field" and c["entity_id"] in raw["nodes"]:
                kept[c["entity_id"]] = (theirs, self.ours) if c["kind"] == "deleted-in-ours" else (self.ours, theirs)
        grew = bool(kept)
        while grew:
            grew = False
            for nid, (keeper, deleter) in list(kept.items()):
                for cid, child in keeper.nodes.items():
                    if child.parent_id == nid and cid not in raw["nodes"] and cid in self.base.nodes \
                            and cid not in deleter.nodes:
                        raw["nodes"][cid] = _fields(child)
                        kept[cid] = (keeper, deleter)
                        grew = True
        for nid, (keeper, deleter) in kept.items():
            for eid, e in keeper.edges.items():
                if nid in (e.source, e.target) and eid not in raw["edges"] and eid in self.base.edges \
                        and eid not in deleter.edges:
                    raw["edges"][eid] = _fields(e)

    def run(self) -> MergeResult:
        theirs = self._renumber()
        raw = {coll: self._collection(coll, theirs) for coll in MODELS}
        self._revive(raw, theirs)
        self._relink(raw, theirs)
        self._tidy(raw)
        st = self.out.state
        for coll, cls in MODELS.items():
            target = getattr(st, coll)
            for eid, data in raw[coll].items():
                try:
                    target[eid] = cls.model_validate({**data, "id": eid})
                except ValidationError as exc:
                    err = exc.errors()[0]
                    raise MergeError(f"invalid value for {KIND[coll]} '{eid}' "
                                     f"({'.'.join(str(x) for x in err['loc'])}): {err['msg']}") from exc
        return self.out


def merge_states(base: ProjectState, ours: ProjectState, theirs: ProjectState,
                 resolutions: dict[str, dict[str, Any]] | None = None) -> MergeResult:
    """Three-way merge. Conflicts without a resolution keep ours and are listed in ``unresolved``.

    Soft-deleted requirements merge as if they were absent (so a delete on one side meets a change on the other as a
    delete-vs-modify conflict); one that stays deleted is put back as deleted, with its links, so it can be restored."""
    result = _Merger(_live(base), _live(ours), _live(theirs), resolutions).run()
    st = result.state
    for side in (ours, theirs):
        for rid, r in side.requirements.items():
            if r.deleted and rid not in st.requirements:
                st.requirements[rid] = r.model_copy(deep=True)
    for side in (ours, theirs):
        for nid, n in st.nodes.items():
            if nid in side.nodes:
                n.requirement_ids += [rid for rid in side.nodes[nid].requirement_ids
                                      if rid in st.requirements and st.requirements[rid].deleted
                                      and rid not in n.requirement_ids]
    return result


def _live(state: ProjectState) -> ProjectState:
    """``state`` without soft-deleted requirements (components keep their links; the merge drops dangling ones)."""
    if not any(r.deleted for r in state.requirements.values()):
        return state
    out = state.model_copy(deep=True)
    out.requirements = {rid: r for rid, r in out.requirements.items() if not r.deleted}
    return out


# --------------------------------------------------------------------------- merged state → ops
def _depth(state: ProjectState, nid: str) -> int:
    d, seen, p = 0, {nid}, state.nodes[nid].parent_id
    while p and p in state.nodes and p not in seen:
        seen.add(p)
        d, p = d + 1, state.nodes[p].parent_id
    return d


def plan_ops(cur: ProjectState, new: ProjectState) -> list[dict[str, Any]]:
    """Ops that turn ``cur`` into ``new`` and pass ``apply_ops``' checks (update ops carry ``base_rev`` so a change
    made to the target after this plan is reported as a conflict instead of being overwritten)."""
    ops: list[dict[str, Any]] = []
    base_rev = cur.rev
    moved = {nid for nid, n in new.nodes.items() if nid in cur.nodes and cur.nodes[nid].parent_id != n.parent_id}
    removed_nodes = [nid for nid in cur.nodes if nid not in new.nodes]

    # 1. connections that go away, or that must be re-made because an end moves to another diagram
    readd: set[str] = set()
    for eid, e in cur.edges.items():
        if eid not in new.edges:
            ops.append({"op": "remove_edge", "id": eid})
        elif {e.source, e.target, new.edges[eid].source, new.edges[eid].target} & moved:
            ops.append({"op": "remove_edge", "id": eid})
            readd.add(eid)
    # 2. requirements first, so components can link to them
    for rid, r in new.requirements.items():
        old = cur.requirements.get(rid)
        if old is None:
            if not r.deleted:
                ops.append({"op": "add_requirement", "requirement": _fields(r) | {"id": rid}})
            continue
        if old.deleted and not r.deleted:
            ops.append({"op": "restore_requirement", "id": rid})
        if changes := {k: v for k, v in _changes(old, r).items() if k != "deleted"}:
            ops.append({"op": "update_requirement", "id": rid, "changes": changes, "base_rev": base_rev})
        if r.deleted and not old.deleted:
            ops.append({"op": "remove_requirement", "id": rid})
    # 3. new components, containers before their contents
    for nid in sorted((n for n in new.nodes if n not in cur.nodes), key=lambda n: _depth(new, n)):
        ops.append({"op": "add_node", "node": _fields(new.nodes[nid]) | {"id": nid}})
    for nid, n in new.nodes.items():
        if nid in cur.nodes and (changes := _changes(cur.nodes[nid], n)):
            ops.append({"op": "update_node", "id": nid, "changes": changes, "base_rev": base_rev})
    # 4. removals (removing a component also removes its internals, so skip those)
    gone = set(removed_nodes)

    def parent(nid: str) -> str:
        return (new.nodes[nid] if nid in new.nodes else cur.nodes[nid]).parent_id

    for nid in removed_nodes:
        seen, p = {nid}, parent(nid)
        while p and p not in gone and p not in seen and (p in new.nodes or p in cur.nodes):
            seen.add(p)
            p = parent(p)
        if not (p and p in gone):
            ops.append({"op": "remove_node", "id": nid})
    ops += [{"op": "remove_requirement", "id": rid} for rid, r in cur.requirements.items()
            if rid not in new.requirements and not r.deleted]
    # 5. connections
    for eid, e in new.edges.items():
        if eid not in cur.edges or eid in readd:
            ops.append({"op": "add_edge", "edge": _fields(e) | {"id": eid}})
        elif changes := _changes(cur.edges[eid], e):
            ops.append({"op": "update_edge", "id": eid, "changes": changes, "base_rev": base_rev})
    return ops


def _changes(old: Any, new: Any) -> dict[str, Any]:
    a, b = _fields(old), _fields(new)
    return {k: v for k, v in b.items() if a.get(k) != v}


# --------------------------------------------------------------------------- service
def _states(store: Any, pid: str, source: str, target: str) -> tuple[ProjectState, ProjectState]:
    if source == target:
        raise MergeError("choose two different branches")
    theirs, ours = store.get_state(pid, source), store.get_state(pid, target)
    if theirs is None:
        raise KeyError(source)
    if ours is None:
        raise KeyError(target)
    return ours, theirs


def preview(store: Any, pid: str, source: str, target: str,
            resolutions: dict[str, dict[str, Any]] | None = None) -> tuple[dict[str, Any], MergeResult, ProjectState]:
    """(payload for the UI, merge result, target state). Raises KeyError for a missing branch, MergeError."""
    ours, theirs = _states(store, pid, source, target)
    base_version, base, how = find_base(store, pid, source, target)
    result = merge_states(base, ours, theirs, resolutions)
    d = diff_states(ours, result.state, include_moves=True)
    return {
        "source": source, "target": target, "source_rev": theirs.rev, "target_rev": ours.rev,
        "base": {"version": base_version, "how": how},
        "changes": {**d, "text": diff_to_text(d)},
        "conflicts": result.conflicts, "dropped": result.dropped, "renumbered": result.renumbered,
    }, result, ours


async def apply(store: Any, pid: str, source: str, target: str, resolutions: dict[str, dict[str, Any]], actor: str,
                commit_ops: Callable[..., Awaitable[tuple[ProjectState, list[str]]]],
                expect: tuple[int | None, int | None] = (None, None)) -> dict[str, Any]:
    """Apply a merge whose every conflict is resolved. Raises MergeError (unresolved or stale) and KeyError."""
    payload, result, ours = preview(store, pid, source, target, resolutions)
    source_rev, target_rev = expect
    if (source_rev is not None and source_rev != payload["source_rev"]) or \
            (target_rev is not None and target_rev != payload["target_rev"]):
        raise StaleMerge("a branch changed since the preview; preview the merge again")
    if result.unresolved:
        raise MergeError(f"{len(result.unresolved)} conflict(s) not resolved: {', '.join(result.unresolved[:5])}")
    ops = plan_ops(ours, result.state)
    # The source as merged becomes a version, so the next merge of these branches starts from here.
    theirs = store.get_state(pid, source)
    head = store.head_version(pid, source)
    if head is None or state_patch(store.get_version(pid, head)["state"], theirs):
        head = store.commit(pid, source, f"Snapshot for merging into {target}", actor, theirs)["number"]
    safety = store.commit(pid, target, f"Before merging {source}", actor, ours) if ops else None
    new_state, summary = await commit_ops(pid, target, ops, f"{actor} (merge from {source})") if ops else (ours, [])
    version = store.commit(pid, target, merge_message(source, head, target), actor, new_state)
    return {"version": version["number"], "safety_version": safety["number"] if safety else None,
            "source_version": head, "branch": target, "source_branch": source, "summary": summary,
            "conflicts_resolved": len(result.conflicts), "dropped": result.dropped,
            "renumbered": result.renumbered, "state": new_state.model_dump()}


# --------------------------------------------------------------------------- AI-assisted resolution
SCHEMA: dict[str, Any] = {"type": "object", "properties": {"resolutions": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"},
                                     "choice": {"type": "string", "enum": list(CHOICES)},
                                     "value": {}, "rationale": {"type": "string"}},
    "required": ["id", "choice", "rationale"]}}}, "required": ["resolutions"]}


def _side_summary(state: ProjectState, limit: int = 60) -> dict[str, Any]:
    names = {n.id: n.name for n in state.nodes.values()}
    return {"components": [f"{n.name} ({n.type}{', ' + n.technology if n.technology else ''})"
                           for n in state.nodes.values() if n.type != "note"][:limit],
            "connections": [f"{names.get(e.source, e.source)} -> {names.get(e.target, e.target)}"
                            + (f" ({e.protocol})" if e.protocol else "") for e in state.edges.values()][: limit * 2]}


def suggest(conflicts: list[dict[str, Any]], ours: ProjectState, theirs: ProjectState, source: str, target: str,
            org_id: str | None = None, version: str | None = None) -> list[dict[str, Any]]:
    """One proposed resolution per conflict the model answered. Raises llm.LLMUnavailable without a model."""
    if not conflicts:
        return []
    reqs = {**theirs.requirements, **ours.requirements}
    context = {"requirements": [{"id": r.id, "title": r.title, "kind": r.kind, "priority": r.priority,
                                 **({"category": r.category} if r.category else {})} for r in reqs.values()][:80],
               "target": _side_summary(ours), "source": _side_summary(theirs)}
    items = [{k: c[k] for k in ("id", "entity", "name", "field", "kind", "base", "ours", "theirs")} for c in conflicts]
    data = agents.run_json(PROMPT, SCHEMA, org_id=org_id, version=version,
                           question=f"Resolve {len(conflicts)} merge conflict(s) from {source} into {target}",
                           variables={"source": source, "target": target,
                                      "conflicts_json": json.dumps(items, indent=1, default=str),
                                      "context_json": json.dumps(context, indent=1)})
    return clean(data.get("resolutions") or [], conflicts)


def clean(items: list[dict[str, Any]], conflicts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {c["id"]: c for c in conflicts}
    out, seen = [], set()
    for s in items:
        cid, choice = str(s.get("id") or ""), s.get("choice")
        c = by_id.get(cid)
        if not c or cid in seen or choice not in CHOICES:
            continue
        if choice == "custom" and c["kind"] != "field":
            continue
        seen.add(cid)
        out.append({"id": cid, "choice": choice, **({"value": s.get("value")} if choice == "custom" else {}),
                    "rationale": " ".join(str(s.get("rationale") or "").split())[:400]})
    return out


# --------------------------------------------------------------------------- routes
class Resolution(BaseModel):
    id: str = Field(min_length=1, max_length=300)
    choice: str = Field(pattern="^(ours|theirs|custom)$")
    value: Any = None


class MergeIn(BaseModel):
    source: str = Field(pattern=BRANCH_PATTERN)
    target: str = Field("main", pattern=BRANCH_PATTERN)


class MergeApplyIn(MergeIn):
    resolutions: list[Resolution] = Field([], max_length=2000)
    source_rev: int | None = None  # from the preview: refuse if a branch changed in between
    target_rev: int | None = None


def register_routes(app: FastAPI, store: Any, *, authz: Any,
                    commit_ops: Callable[..., Awaitable[tuple[ProjectState, list[str]]]],
                    broadcast: Callable[[str, str, dict[str, Any]], Awaitable[None]]) -> None:
    """Merging changes a branch, so every route needs developer or above."""
    edit = authz.project("edit")

    def run(fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except KeyError as exc:
            raise HTTPException(404, f"branch '{exc.args[0]}' not found") from exc
        except StaleMerge as exc:
            raise HTTPException(409, str(exc)) from exc
        except MergeError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/projects/{pid}/merge/preview")
    def merge_preview(pid: str, body: MergeIn, a=Depends(edit)):
        return run(lambda: preview(store, pid, body.source, body.target)[0])

    @app.post("/api/projects/{pid}/merge/suggest")
    async def merge_suggest(pid: str, body: MergeIn, a=Depends(edit)):
        payload, _, ours = run(lambda: preview(store, pid, body.source, body.target))
        if not llm.available(a.org_id):
            return {"suggestions": [], "engine": "none"}
        if not payload["conflicts"]:
            return {"suggestions": [], "engine": "llm"}
        theirs = store.get_state(pid, body.source)
        try:
            items = await asyncio.to_thread(suggest, payload["conflicts"], ours, theirs, body.source, body.target,
                                            a.org_id)
        except (llm.LLMUnavailable, llm.LLMRefused) as exc:
            log.info("merge suggestions unavailable: %s", type(exc).__name__)
            return {"suggestions": [], "engine": "none"}
        return {"suggestions": items, "engine": "llm"}

    @app.post("/api/projects/{pid}/merge/apply")
    async def merge_apply(pid: str, body: MergeApplyIn, a=Depends(edit)):
        resolutions = {r.id: r.model_dump() for r in body.resolutions}
        try:
            out = await apply(store, pid, body.source, body.target, resolutions, a.actor, commit_ops,
                              (body.source_rev, body.target_rev))
        except KeyError as exc:
            raise HTTPException(404, f"branch '{exc.args[0]}' not found") from exc
        except StaleMerge as exc:
            raise HTTPException(409, str(exc)) from exc
        except MergeError as exc:
            raise HTTPException(422, str(exc)) from exc
        for branch in {body.source, body.target}:
            await broadcast(pid, branch, {"type": "versions", "actor": a.actor})
        return out


__all__ = ["merge_states", "plan_ops", "find_base", "preview", "apply", "suggest", "register_routes", "MergeError"]
