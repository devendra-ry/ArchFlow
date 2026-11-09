"""Bridge between the API's structured state and the live Yjs documents in the collab server.

Direction 1 — collab → API: the collab server periodically sends the document's projection (a plain
ProjectState JSON). :func:`merge_projection` sanitizes it, bumps revisions for changed fields, and returns
a human-readable summary for the activity log.

Direction 2 — API → collab: when the API itself changes a branch (AI command, MCP agent, restore, design
chat), :func:`state_patch` computes a *field-level* patch which the collab server applies to the live
document. Field-level patches merge with concurrent human edits instead of overwriting them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Any

import httpx
from pydantic import ValidationError

from .diff import diff_states
from .models import NODE_TYPES, Edge, Node, ProjectState, Requirement

log = logging.getLogger("archflow.collab")

COLLAB_URL = os.environ.get("ARCHFLOW_COLLAB_URL", "http://localhost:1234").rstrip("/")
# Shared secret between the API and the collab server. No default: without it, the internal endpoints refuse
# every request and live-document sync is off (the API still works).
INTERNAL_TOKEN = os.environ.get("ARCHFLOW_INTERNAL_TOKEN", "")
if not INTERNAL_TOKEN:
    log.warning("ARCHFLOW_INTERNAL_TOKEN is not set: real-time sync with the collab server is disabled. See .env.example.")
COLLECTIONS = {"nodes": Node, "edges": Edge, "requirements": Requirement}
BOOKKEEPING = {"rev", "field_revs"}


TICKET_TTL = 120  # seconds; the browser fetches a fresh ticket for every (re)connect


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _ticket_sig(body: str) -> str:
    return _b64url(hmac.new(INTERNAL_TOKEN.encode(), f"collab-ticket.{body}".encode(), hashlib.sha256).digest())


def sign_ticket(claims: dict[str, Any], ttl: int = TICKET_TTL, now: float | None = None) -> str:
    """A short-lived ticket for the collab server: ``<base64url(json claims)>.<base64url(HMAC-SHA256)>``.

    Claims: uid, name, color, doc (``<pid>:<branch>``), ro (read-only), exp (unix seconds). Keyed with
    ARCHFLOW_INTERNAL_TOKEN, which the collab server shares (see collab/src/server.ts verifyTicket)."""
    if not INTERNAL_TOKEN:
        raise RuntimeError("ARCHFLOW_INTERNAL_TOKEN is not configured")
    payload = {**claims, "exp": int((now if now is not None else time.time()) + ttl)}
    body = _b64url(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{body}.{_ticket_sig(body)}"


def verify_ticket(ticket: str, doc: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Claims of a valid ticket; raises ValueError if the signature, expiry or document does not match."""
    if not INTERNAL_TOKEN:
        raise ValueError("ARCHFLOW_INTERNAL_TOKEN is not configured")
    body, _, sig = (ticket or "").partition(".")
    if not body or not sig or not hmac.compare_digest(sig, _ticket_sig(body)):
        raise ValueError("bad ticket signature")
    try:
        claims = json.loads(_unb64url(body))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("malformed ticket") from exc
    exp = claims.get("exp") if isinstance(claims, dict) else None
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp < (now if now is not None else time.time()):
        raise ValueError("ticket expired")
    if doc is not None and claims.get("doc") != doc:
        raise ValueError("ticket is for another document")
    return claims


def doc_name(pid: str, branch: str) -> str:
    return f"{pid}:{branch}"


def parse_doc_name(name: str) -> tuple[str, str]:
    pid, _, branch = name.partition(":")
    return pid, branch or "main"


def _fields(entity) -> dict[str, Any]:
    return entity.model_dump(exclude=BOOKKEEPING)


def state_patch(old: ProjectState, new: ProjectState) -> dict[str, Any]:
    """Field-level patch turning ``old`` into ``new``: {coll: {"set": {id: {field: value}}, "del": [ids]}}."""
    patch: dict[str, Any] = {}
    for coll in COLLECTIONS:
        a, b = getattr(old, coll), getattr(new, coll)
        sets: dict[str, dict[str, Any]] = {}
        for eid, ent in b.items():
            new_f = _fields(ent)
            if eid not in a:
                sets[eid] = new_f
                continue
            old_f = _fields(a[eid])
            changed = {k: v for k, v in new_f.items() if old_f.get(k) != v}
            if changed:
                sets[eid] = changed
        dels = [eid for eid in a if eid not in b]
        if sets or dels:
            patch[coll] = {"set": sets, "del": dels}
    return patch


def sanitize(raw: dict[str, Any]) -> tuple[ProjectState, list[str]]:
    """Build a valid ProjectState from an untrusted projection, dropping (and reporting) invalid entities."""
    dropped: list[str] = []
    out = ProjectState()
    for coll, cls in COLLECTIONS.items():
        items = raw.get(coll) or {}
        for eid, data in items.items() if isinstance(items, dict) else []:
            if not isinstance(data, dict):
                dropped.append(f"{coll}/{eid}")
                continue
            data = {k: v for k, v in data.items() if k not in BOOKKEEPING}
            data["id"] = eid
            if coll == "nodes" and data.get("type") not in NODE_TYPES:
                data["type"] = "service"
            try:
                getattr(out, coll)[eid] = cls.model_validate(data)
            except ValidationError:
                dropped.append(f"{coll}/{eid}")
    # an element whose parent vanished (e.g. deleted concurrently) is shown on the top-level diagram
    for node in out.nodes.values():
        if node.parent_id and (node.parent_id not in out.nodes or node.parent_id == node.id):
            node.parent_id = ""
    # connections must point at existing components of the same diagram
    for eid in [e for e, edge in out.edges.items() if edge.source not in out.nodes or edge.target not in out.nodes
                or out.nodes[edge.source].parent_id != out.nodes[edge.target].parent_id]:
        del out.edges[eid]
        dropped.append(f"edges/{eid}")
    return out, dropped


def merge_projection(current: ProjectState, incoming: ProjectState) -> tuple[ProjectState, list[str]]:
    """Adopt the live document's state, carrying revision bookkeeping forward. Returns (state, summary)."""
    patch = state_patch(current, incoming)
    if not patch:
        return current, []
    new_rev = current.rev + 1
    merged = incoming.model_copy(deep=True)
    merged.rev = new_rev
    for coll in COLLECTIONS:
        prev = getattr(current, coll)
        changed = patch.get(coll, {}).get("set", {})
        for eid, ent in getattr(merged, coll).items():
            if eid in prev:
                ent.field_revs = dict(prev[eid].field_revs)
                ent.rev = prev[eid].rev
            if eid in changed:
                for f in changed[eid]:
                    ent.field_revs[f] = new_rev
                ent.rev = new_rev
    return merged, summarize(current, merged)


def summarize(old: ProjectState, new: ProjectState) -> list[str]:
    d = diff_states(old, new, include_moves=True)
    out: list[str] = []
    labels = {"nodes": "component", "edges": "connection", "requirements": "requirement"}
    for key, label in labels.items():
        sec = d[key]
        out += [f"added {label} '{x['name']}'" for x in sec["added"]]
        out += [f"removed {label} '{x['name']}'" for x in sec["removed"]]
        for x in sec["changed"]:
            fields = [f["field"] for f in x["fields"]]
            out.append(f"moved '{x['name']}'" if fields == ["position"] else f"updated '{x['name']}' ({', '.join(fields)})")
    return out


async def push_patch(pid: str, branch: str, patch: dict[str, Any], actor: str) -> bool:
    """Apply a server-side change to the live document. Returns False if the collab server is unreachable."""
    if not COLLAB_URL or not patch:
        return True
    url = f"{COLLAB_URL}/internal/docs/{doc_name(pid, branch)}/patch"
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(url, json={"patch": patch, "actor": actor},
                                  headers={"x-internal-token": INTERNAL_TOKEN})
        if r.status_code >= 400:
            log.warning("collab patch for %s:%s failed: %s %s", pid, branch, r.status_code, r.text[:200])
            return False
        return True
    except httpx.HTTPError as exc:
        log.info("collab server unreachable (%s); live document will be rebuilt from state", exc)
        return False
