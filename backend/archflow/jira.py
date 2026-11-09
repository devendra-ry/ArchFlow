"""Jira Cloud push: turn a branch's requirements and architecture into epics, stories, sub-tasks and issue links.

How the plan is derived (nothing is a fixed template; every item comes from the model):

* **Epics** — one per top-level (HLD) component that *owns* at least one functional requirement. A requirement's
  owner is the component that implements it (linked directly, or through one of its low-level elements) with the
  most "buildable" type (service, agent, security, MCP server, gateway, client, ... data stores last); ties go to
  the component linked to the fewest requirements (the most specific one), then the name. Functional
  requirements no component implements yet go to one "not on the architecture yet" epic.
* **Stories** — one per functional requirement, under its owner's epic: a user story, acceptance criteria as
  Given/When/Then (existing criteria rewritten, plus the ones the architecture implies: a failing dependency,
  quality targets), labels from priority and category, Jira components from the linked canvas components, and a
  story-point suggestion with its basis. Written by the ``jira.stories`` agent; without AI, rules derive the same
  fields from the same data (``engine: "offline"``).
* **Sub-tasks** — one per low-level element (endpoint, table, event, module) inside a component the story's
  requirement is linked to: under the requirements the element itself is linked to, else under the highest-priority
  story its component owns, so no element becomes two sub-tasks.
* **Issue links** — "blocks" links between epics, from connections on the canvas: when epic A's component calls
  (directly or through components that own no epic) epic B's component, B blocks A.
* Non-functional requirements are not stories: they become quality targets in the stories and epics they
  constrain (system-wide when no component implements them).

Idempotency: every pushed object is recorded in ``external_links`` with the issue key and a hash of the ArchFlow
content it came from. A re-push updates changed items, skips unchanged ones and never creates a second issue; an
issue created but not recorded (a crash between the two) is found again by its label ``archflow-<project>-<id>``.
Pushes honour 429/503 ``Retry-After`` with exponential backoff and jitter; a failing item is reported and the rest
continue, so pushing again only retries what failed.

Outbound requests go only to the org's configured site, which must be https and resolve to public addresses
(``ARCHFLOW_ALLOW_PRIVATE_JIRA_URLS=true`` allows a private Jira Data Center). The API token is encrypted with
``secrets_box`` (AES-256-GCM, bound to the org) and only its last 4 characters are ever returned.

Roles: admin+ configures the connection; developer+ previews and pushes; anyone who can read the project sees the
links and the last pulled status. Status is read back into ``external_links`` only: requirement status in
ArchFlow is never changed automatically.
"""

from __future__ import annotations

import asyncio
import email.utils
import hashlib
import ipaddress
import json
import logging
import os
import random
import re
import socket
import threading
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import agents, llm, secrets_box, users
from .models import Node, ProjectState, Requirement

log = logging.getLogger("archflow.jira")

PROMPT = "jira.stories"
PLAN_VERSION = "1"  # part of every content hash: bump it when the pushed fields change shape
FIB = (1, 2, 3, 5, 8, 13)
PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")
OWNER_RANK = {t: i for i, t in enumerate(("service", "agent", "security", "mcp", "gateway", "client", "llm", "external",
                                          "queue", "database", "cache", "storage"))}
PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
UNASSIGNED = "unassigned"
MAX_SUBTASKS = 12
AI_BATCH = 12
LINK_TYPE = "Blocks"


class JiraError(RuntimeError):
    """A Jira request failed (the message is safe to show; it never contains the token)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- site URL guard (SSRF)
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


def allow_private() -> bool:
    return os.environ.get("ARCHFLOW_ALLOW_PRIVATE_JIRA_URLS", "").strip().lower() in ("1", "true", "yes")


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"Cannot resolve {host}.") from exc
    return sorted({info[4][0] for info in infos})


def check_site_url(url: str) -> str:
    """Validate a Jira site URL an admin typed; returns it normalized (no trailing slash). https only; the host must
    resolve to public addresses unless ARCHFLOW_ALLOW_PRIVATE_JIRA_URLS is set (link-local and metadata addresses
    are always refused). Raises ValueError."""
    url = (url or "").strip().rstrip("/")
    if len(url) > 300:
        raise ValueError("The site URL is too long.")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ValueError("The Jira site must be an https:// URL, e.g. https://your-team.atlassian.net.")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValueError("Do not put credentials in the site URL.")
    if parts.query or parts.fragment:
        raise ValueError("Enter only the site address, e.g. https://your-team.atlassian.net.")
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError("Invalid port.") from exc
    private_ok = allow_private()
    if port not in (None, 443) and not private_ok:
        raise ValueError("Only the default https port is allowed.")
    if parts.path and not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", parts.path):
        raise ValueError("Unsupported path in the site URL.")
    try:
        ipaddress.ip_address(host)
        literal = True
    except ValueError:
        literal = False
    if host in ("metadata.google.internal", "metadata"):
        raise ValueError("That site is not allowed.")
    if not literal and (not host or not _HOST_RE.match(host)):
        raise ValueError("That site is not allowed.")
    if not literal and not private_ok and (host == "localhost" or host.endswith((".localhost", ".local", ".internal"))):
        raise ValueError("Private and local sites are disabled on this server "
                         "(set ARCHFLOW_ALLOW_PRIVATE_JIRA_URLS=true for a Jira Data Center on your network).")
    addrs = [host] if literal else _resolve(host)
    if not addrs:
        raise ValueError(f"Cannot resolve {host}.")
    for a in addrs:
        ip = ipaddress.ip_address(a)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
            raise ValueError("That site is not allowed (link-local or reserved address).")
        if (ip.is_private or ip.is_loopback or not ip.is_global) and not private_ok:
            raise ValueError("The site resolves to a private or local address, which is disabled on this server "
                             "(set ARCHFLOW_ALLOW_PRIVATE_JIRA_URLS=true for a Jira Data Center on your network).")
    port_part = f":{port}" if port not in (None, 443) else ""
    return f"https://{host}{port_part}{parts.path.rstrip('/')}"


def check_project_key(key: str) -> str:
    key = (key or "").strip().upper()
    if not PROJECT_KEY_RE.match(key):
        raise ValueError("A Jira project key is 2-10 capital letters, digits or underscores, starting with a letter "
                         "(e.g. SHOP).")
    return key


# --------------------------------------------------------------------------- HTTP client
def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        parsed = email.utils.parsedate_tz(value)
        return max(0.0, email.utils.mktime_tz(parsed) - time.time()) if parsed else None


def backoff(attempt: int, retry_after: float | None, *, base: float = 0.5, cap: float = 30.0) -> float:
    """Seconds to wait before retry ``attempt + 1``: at least ``Retry-After`` when given, plus jitter; otherwise
    exponential backoff with jitter (half fixed, half random)."""
    expo = min(cap, base * 2 ** attempt)
    if retry_after is not None:
        return min(cap, retry_after) + random.uniform(0, min(1.0, expo))
    return expo / 2 + random.uniform(0, expo / 2)


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    if not isinstance(data, dict):
        return ""
    msgs = [str(m) for m in data.get("errorMessages") or []]
    msgs += [f"{k}: {v}" for k, v in (data.get("errors") or {}).items()]
    return "; ".join(msgs)[:300] or str(data.get("message") or "")[:200]


class JiraClient:
    """A small Jira Cloud REST v3 client (basic auth: account email + API token)."""

    def __init__(self, site_url: str, email_: str, token: str, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int | None = None,
                 timeout: float | None = None):
        self.site = site_url.rstrip("/")
        self.sleep = sleep
        self.attempts = max(1, max_attempts or int(os.environ.get("ARCHFLOW_JIRA_MAX_ATTEMPTS", 5)))
        self._http = httpx.Client(base_url=self.site, auth=(email_, token), transport=transport,
                                  timeout=timeout or float(os.environ.get("ARCHFLOW_JIRA_TIMEOUT", 30)),
                                  follow_redirects=False, headers={"Accept": "application/json"})

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "JiraClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def request(self, method: str, path: str, *, json_body: Any = None, params: dict[str, Any] | None = None) -> Any:
        """One API call with retries: 429 and 503 always (Retry-After honoured), other 5xx and network errors only
        for idempotent methods (a timed-out POST may have created the issue; the label check finds it later)."""
        idempotent = method in ("GET", "PUT", "DELETE")
        last, status = "", 0
        for attempt in range(self.attempts):
            wait: float | None = None
            try:
                resp = self._http.request(method, path, json=json_body, params=params)
            except httpx.HTTPError as exc:
                last, status = f"network error ({type(exc).__name__})", 0
                if not idempotent:
                    raise JiraError(f"Jira did not answer: {last}") from exc
            else:
                if resp.status_code < 300:
                    if not resp.content:
                        return None
                    try:
                        return resp.json()
                    except ValueError as exc:
                        raise JiraError("Jira returned invalid JSON", resp.status_code) from exc
                status = resp.status_code
                if resp.is_redirect:
                    raise JiraError(f"Jira redirected the request (HTTP {status}): check the site URL", status)
                retryable = status in (429, 503) or (idempotent and status in (500, 502, 504))
                if not retryable:
                    hint = {401: "check the email and API token", 403: "the account lacks permission",
                            404: "not found"}.get(status, "")
                    detail = _detail(resp)
                    raise JiraError(f"Jira: HTTP {status}" + (f" ({hint})" if hint else "")
                                    + (f": {detail}" if detail else ""), status)
                last, wait = f"HTTP {status}", _retry_after(resp)
            if attempt + 1 < self.attempts:
                delay = backoff(attempt, wait)
                log.info("jira %s %s: %s; retrying in %.2fs", method, path, last, delay)
                self.sleep(delay)
        raise JiraError(f"Jira request failed after {self.attempts} attempts ({last})", status)

    # ---- endpoints ----
    def myself(self) -> dict[str, Any]:
        return self.request("GET", "/rest/api/3/myself") or {}

    def project(self, key: str) -> dict[str, Any]:
        return self.request("GET", f"/rest/api/3/project/{key}", params={"expand": "issueTypes"}) or {}

    def fields(self) -> list[dict[str, Any]]:
        return self.request("GET", "/rest/api/3/field") or []

    def search(self, jql: str, fields: list[str], max_results: int = 50) -> list[dict[str, Any]]:
        data = self.request("POST", "/rest/api/3/search/jql",
                            json_body={"jql": jql, "fields": fields, "maxResults": max_results}) or {}
        return data.get("issues") or []

    def search_page(self, jql: str, fields: list[str], max_results: int = 50,
                    page_token: str | None = None) -> tuple[list[dict[str, Any]], str | None]:
        """One page of an enhanced JQL search: (issues, the next page's token or None)."""
        body: dict[str, Any] = {"jql": jql, "fields": fields, "maxResults": max_results}
        if page_token:
            body["nextPageToken"] = page_token
        data = self.request("POST", "/rest/api/3/search/jql", json_body=body) or {}
        token = None if data.get("isLast") else (data.get("nextPageToken") or None)
        return data.get("issues") or [], token

    def get_issues(self, keys: list[str], fields: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
        """(issues with these fields, keys Jira does not know) for up to 100 issues."""
        data = self.request("POST", "/rest/api/3/issue/bulkfetch",
                            json_body={"issueIdsOrKeys": keys, "fields": fields}) or {}
        missing = [str(e.get("issueIdOrKey")) for e in data.get("issueErrors") or [] if e.get("issueIdOrKey")]
        return list(data.get("issues") or []), missing

    def create_issue(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/rest/api/3/issue", json_body={"fields": fields}) or {}

    def update_issue(self, key: str, fields: dict[str, Any]) -> None:
        self.request("PUT", f"/rest/api/3/issue/{key}", json_body={"fields": fields})

    def link_issues(self, link_type: str, inward: str, outward: str) -> None:
        """Jira reads it as "<outward> <outward description> <inward>": for "Blocks", outward blocks inward."""
        self.request("POST", "/rest/api/3/issueLink", json_body={
            "type": {"name": link_type}, "inwardIssue": {"key": inward}, "outwardIssue": {"key": outward}})

    def bulk_status(self, keys: list[str]) -> tuple[dict[str, dict[str, str]], list[str]]:
        """({key: {status, category}}, keys Jira does not know) for up to 100 issues."""
        data = self.request("POST", "/rest/api/3/issue/bulkfetch",
                            json_body={"issueIdsOrKeys": keys, "fields": ["status"]}) or {}
        out: dict[str, dict[str, str]] = {}
        for issue in data.get("issues") or []:
            st = (issue.get("fields") or {}).get("status") or {}
            out[issue.get("key", "")] = {"status": str(st.get("name") or ""),
                                         "category": str((st.get("statusCategory") or {}).get("key") or "")}
        missing = [str(e.get("issueIdOrKey")) for e in data.get("issueErrors") or [] if e.get("issueIdOrKey")]
        return out, missing


# Tests replace these to keep every request in-process.
_transport: httpx.BaseTransport | None = None
_sleep: Callable[[float], None] = time.sleep


def token_aad(org_id: str) -> str:
    return f"org-jira-token:{org_id}"


def make_client(conn: dict[str, Any]) -> JiraClient:
    """A client for an org's stored connection: decrypts the token and re-checks the site (DNS may have changed)."""
    site = check_site_url(conn["site_url"])
    token = secrets_box.decrypt(conn["ciphertext"], token_aad(conn["org_id"]))
    return JiraClient(site, conn["email"], token, transport=_transport, sleep=_sleep)


def browse_url(site: str, key: str) -> str:
    return f"{site.rstrip('/')}/browse/{key}"


# --------------------------------------------------------------------------- reading the model
def slug(text: str, limit: int = 50) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "-", text.lower()).strip("-.")[:limit] or "x"


def content_hash(source: Any) -> str:
    raw = json.dumps([PLAN_VERSION, source], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _lower_first(s: str) -> str:
    s = s.strip().rstrip(".")
    return s[:1].lower() + s[1:] if s[:2] != s[:2].upper() else s


def _node_label(n: Node) -> str:
    return f"{n.name} ({n.type}{', ' + n.technology if n.technology else ''})"


class Model:
    """What the plan needs to know about one branch, derived once."""

    def __init__(self, state: ProjectState):
        self.state = state
        self.top = {nid: n for nid, n in state.nodes.items() if not n.parent_id and n.type != "note"}
        self.functional = sorted((r for r in state.requirements.values() if r.kind == "functional"), key=lambda r: r.id)
        self.nfrs = sorted((r for r in state.requirements.values() if r.kind != "functional"), key=lambda r: r.id)
        self.linked: dict[str, list[str]] = {}  # requirement -> top-level component ids
        for r in state.requirements.values():
            ids: list[str] = []
            for n in state.nodes.values():
                if r.id in n.requirement_ids and n.type != "note":
                    top = self.top_of(n.id)
                    if top and top not in ids:
                        ids.append(top)
            self.linked[r.id] = ids
        self.req_count = {nid: sum(nid in ids for ids in self.linked.values()) for nid in self.top}
        self.owner = {r.id: self._owner(r.id) for r in self.functional}
        # quality targets per component; untraced ones apply to the whole system
        self.constraints: dict[str, list[Requirement]] = {nid: [] for nid in self.top}
        self.system_nfrs: list[Requirement] = []
        for r in self.nfrs:
            if self.linked[r.id]:
                for nid in self.linked[r.id]:
                    self.constraints[nid].append(r)
            else:
                self.system_nfrs.append(r)
        self.out_edges: dict[str, list[str]] = {}
        for e in state.edges.values():
            if e.source in self.top and e.target in self.top and e.source != e.target:
                self.out_edges.setdefault(e.source, []).append(e.target)
        owners = {o for o in self.owner.values() if o}
        self.epics = sorted(owners, key=lambda nid: (self.top[nid].name.lower(), nid))
        if any(o is None for o in self.owner.values()):
            self.epics.append(UNASSIGNED)
        self.epic_deps = {nid: self._reach(nid, owners) for nid in self.epics if nid != UNASSIGNED}
        self.subtasks = self._subtasks()

    def top_of(self, nid: str) -> str | None:
        seen = set()
        while nid in self.state.nodes and self.state.nodes[nid].parent_id and nid not in seen:
            seen.add(nid)
            nid = self.state.nodes[nid].parent_id
        return nid if nid in self.top else None

    def _owner(self, rid: str) -> str | None:
        ids = self.linked.get(rid) or []
        if not ids:
            return None
        return min(ids, key=lambda nid: (OWNER_RANK.get(self.top[nid].type, 99), self.req_count[nid],
                                         self.top[nid].name.lower(), nid))

    def _reach(self, start: str, owners: set[str]) -> list[str]:
        """Epic components ``start`` depends on: reachable through connections, stopping at the first owner."""
        found: list[str] = []
        seen, queue = {start}, list(self.out_edges.get(start, []))
        while queue:
            nid = queue.pop(0)
            if nid in seen:
                continue
            seen.add(nid)
            if nid in owners:
                found.append(nid)
            else:
                queue.extend(self.out_edges.get(nid, []))
        return sorted(found, key=lambda n: self.top[n].name.lower())

    def lld_of(self, nid: str) -> list[Node]:
        return sorted((n for n in self.state.nodes.values() if n.parent_id == nid and n.type != "note"),
                      key=lambda n: (n.type, n.name.lower(), n.id))

    def depends_on(self, rid: str) -> list[str]:
        """Components the requirement's components call that are not part of it (one hop)."""
        mine = set(self.linked.get(rid) or [])
        out: list[str] = []
        for nid in self.linked.get(rid) or []:
            for t in self.out_edges.get(nid, []):
                if t not in mine and t not in out:
                    out.append(t)
        return out

    def constraints_of(self, rid: str) -> list[Requirement]:
        out: list[Requirement] = []
        for nid in self.linked.get(rid) or []:
            for r in self.constraints.get(nid, []):
                if r not in out:
                    out.append(r)
        return out + [r for r in self.system_nfrs if r not in out]

    def _subtasks(self) -> dict[str, list[Node]]:
        """requirement id -> its LLD sub-task elements (each element under one story only)."""
        func = {r.id: r for r in self.functional}
        by_story: dict[str, list[Node]] = {r.id: [] for r in self.functional}
        for nid in self.top:
            elements = self.lld_of(nid)
            if not elements:
                continue
            stories = [r for r in self.functional if nid in self.linked[r.id]]
            stories.sort(key=lambda r: (self.owner[r.id] != nid, PRIORITY_RANK.get(r.priority, 9), r.id))
            for el in elements:
                targets = [rid for rid in el.requirement_ids if rid in func and nid in self.linked[rid]]
                if not targets and stories:
                    targets = [stories[0].id]
                for rid in targets:
                    by_story[rid].append(el)
        return {rid: els[:MAX_SUBTASKS] for rid, els in by_story.items()}


# --------------------------------------------------------------------------- story writing (AI and offline)
STORY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"stories": {"type": "array", "items": {
    "type": "object", "properties": {
        "requirement_id": {"type": "string"}, "summary": {"type": "string"}, "user_story": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "object", "properties": {
            "given": {"type": "string"}, "when": {"type": "string"}, "then": {"type": "string"}},
            "required": ["given", "when", "then"]}},
        "story_points": {"type": "integer", "enum": list(FIB)}, "points_basis": {"type": "string"}},
    "required": ["requirement_id", "summary", "acceptance_criteria", "story_points", "points_basis"]}}},
    "required": ["stories"]}

_GWT = re.compile(r"^\s*given\s+(?P<g>.+?),?\s+when\s+(?P<w>.+?),?\s+then\s+(?P<t>.+?)\s*\.?\s*$", re.I | re.S)


def story_input(m: Model, r: Requirement) -> dict[str, Any]:
    """What the story writer (AI or rules) gets for one requirement."""
    return {
        "requirement_id": r.id, "title": r.title, "description": r.description, "priority": r.priority,
        **({"category": r.category} if r.category else {}), "actors": r.actors,
        "acceptance_criteria": r.acceptance_criteria,
        "components": [{"name": m.top[n].name, "type": m.top[n].type,
                        **({"technology": m.top[n].technology} if m.top[n].technology else {})}
                       for n in m.linked[r.id]],
        "low_level_elements": [f"{e.name} ({e.type})" for e in m.subtasks.get(r.id, [])],
        "depends_on": [m.top[n].name for n in m.depends_on(r.id)],
        "constraints": [f"{c.id} {c.title}: {c.description}".strip().rstrip(":.") for c in m.constraints_of(r.id)],
    }


def nearest_points(value: Any) -> int:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 3
    return min(FIB, key=lambda f: (abs(f - v), f))


def to_gwt(criterion: str, actor: str, component: str, title: str) -> dict[str, str]:
    m = _GWT.match(criterion)
    if m:
        return {"given": m["g"].strip(), "when": m["w"].strip(), "then": m["t"].strip().rstrip(".")}
    where = f" using {component}" if component else ""
    return {"given": f"{actor}{where}", "when": f"they use '{title}'", "then": _lower_first(criterion)}


def offline_story(inp: dict[str, Any]) -> dict[str, Any]:
    """Rules that derive the story from the same data the AI gets (used without AI, and to fill gaps)."""
    actor_name = (inp["actors"] or ["user"])[0]
    actor = f"a {actor_name}" if not actor_name[:1].isupper() else actor_name
    comp = inp["components"][0]["name"] if inp["components"] else ""
    title = inp["title"]
    ac = [to_gwt(c, actor, comp, title) for c in inp["acceptance_criteria"]]
    if not ac:
        ac.append({"given": f"{actor}{' using ' + comp if comp else ''}", "when": f"they use '{title}'",
                   "then": _lower_first(inp["description"] or f"{title} works as described")})
    for dep in inp["depends_on"][:2]:
        ac.append({"given": f"{dep} is unavailable", "when": f"{actor} uses '{title}'",
                   "then": "the failure is reported clearly and nothing is left half-done"})
    for c in inp["constraints"][:2]:
        ac.append({"given": "normal production load", "when": f"{actor} uses '{title}'", "then": f"it meets {c}"})
    ac = ac[:6]
    n_comp, n_lld, n_dep = len(inp["components"]), len(inp["low_level_elements"]), len(inp["depends_on"])
    work = (0.3 * len(ac) + 0.75 * n_comp + 0.4 * n_lld + 0.5 * n_dep + 0.25 * len(inp["constraints"])
            + {"critical": 1.0, "high": 0.5}.get(inp["priority"], 0.0))
    points = next((f for f in FIB if f >= work), FIB[-1])
    basis = ", ".join(p for p in [
        f"{n_comp} component{'s' if n_comp != 1 else ''}" + (f" ({', '.join(c['name'] for c in inp['components'][:4])})" if n_comp else ""),
        f"{n_lld} low-level element{'s' if n_lld != 1 else ''}" if n_lld else "",
        f"{n_dep} integration{'s' if n_dep != 1 else ''} ({', '.join(inp['depends_on'][:3])})" if n_dep else "",
        f"{len(ac)} acceptance criteria",
        f"{len(inp['constraints'])} quality target{'s' if len(inp['constraints']) != 1 else ''}" if inp["constraints"] else "",
        f"{inp['priority']} priority" if inp["priority"] in ("critical", "high") else "",
    ] if p)
    want = _lower_first(title)
    benefit = _lower_first(inp["description"]) if inp["description"] else f"{want} works"
    return {"summary": title[:120], "user_story": f"As {actor}, I want {want}, so that {benefit}.",
            "acceptance_criteria": ac, "story_points": points, "points_basis": f"Rule-based: {basis}."}


def clean_story(raw: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """Validate one AI story; anything missing or malformed comes from the offline story."""
    def text(v: Any, limit: int) -> str:
        return " ".join(str(v or "").split())[:limit]

    ac = []
    for c in raw.get("acceptance_criteria") or []:
        if isinstance(c, dict):
            g, w, t = text(c.get("given"), 300), text(c.get("when"), 300), text(c.get("then"), 400)
            if g and w and t:
                ac.append({"given": g, "when": w, "then": t})
    return {"summary": text(raw.get("summary"), 120) or fallback["summary"],
            "user_story": text(raw.get("user_story"), 400) or fallback["user_story"],
            "acceptance_criteria": ac[:8] or fallback["acceptance_criteria"],
            "story_points": nearest_points(raw.get("story_points")) if raw.get("story_points") is not None
            else fallback["story_points"],
            "points_basis": text(raw.get("points_basis"), 240) or fallback["points_basis"]}


def ai_stories(inputs: list[dict[str, Any]], project_name: str, *, org_id: str | None,
               version: str | None = None) -> dict[str, dict[str, Any]]:
    """{requirement id: raw story} from the ``jira.stories`` agent, in batches. Raises llm.LLMUnavailable etc."""
    out: dict[str, dict[str, Any]] = {}
    wanted = {i["requirement_id"] for i in inputs}
    with llm.usage_scope(feature="jira"):
        for start in range(0, len(inputs), AI_BATCH):
            batch = inputs[start:start + AI_BATCH]
            data = agents.run_json(PROMPT, STORY_SCHEMA, org_id=org_id, version=version,
                                   question=f"Write Jira stories for {', '.join(i['requirement_id'] for i in batch)}",
                                   variables={"project_name": project_name,
                                              "requirements_json": json.dumps(batch, indent=1)})
            for s in data.get("stories") or []:
                if isinstance(s, dict) and s.get("requirement_id") in wanted:
                    out.setdefault(s["requirement_id"], s)
    return out


def write_stories(m: Model, project_name: str, *, use_ai: bool, org_id: str | None,
                  version: str | None = None) -> tuple[dict[str, dict[str, Any]], str]:
    """({requirement id: story}, engine). The AI writes the stories when available; the rules fill any gap."""
    inputs = [story_input(m, r) for r in m.functional]
    offline = {i["requirement_id"]: offline_story(i) for i in inputs}
    if not inputs or not use_ai or not llm.available(org_id):
        return offline, "offline"
    try:
        raw = ai_stories(inputs, project_name, org_id=org_id, version=version)
    except (llm.LLMUnavailable, llm.LLMRefused, llm.LLMInvalidOutput) as exc:
        log.info("jira stories: AI unavailable (%s), using rules", type(exc).__name__)
        return offline, "offline"
    return {rid: clean_story(raw[rid], fb) if rid in raw else fb for rid, fb in offline.items()}, "llm"


# --------------------------------------------------------------------------- the plan
def gwt_text(c: dict[str, str]) -> str:
    return f"Given {c['given']}, when {c['when']}, then {c['then'].rstrip('.')}."


def dedupe_label(pid: str, object_id: str) -> str:
    return f"archflow-{slug(pid, 20)}-{slug(object_id, 60)}"


def _footer(project_name: str, ref: str) -> list[str]:
    return ["---", f"Synced from ArchFlow (project '{project_name}', {ref}). Edit it in ArchFlow: the next push "
                   "overwrites this summary and description."]


def build_plan(state: ProjectState, *, project_id: str, project_name: str, links: list[dict[str, Any]],
               stories: dict[str, dict[str, Any]] | None = None, engine: str = "offline",
               model: Model | None = None) -> dict[str, Any]:
    """The full push plan with create / update / skip per item (no network). ``stories`` defaults to the rules."""
    m = model or Model(state)
    if stories is None:
        stories = {r.id: offline_story(story_input(m, r)) for r in m.functional}
    linked = {(x["object_kind"], x["object_id"]): x for x in links}
    items: list[dict[str, Any]] = []

    def add(item: dict[str, Any], source: Any) -> None:
        item["hash"] = content_hash(source)
        row = linked.get((item["object_kind"], item["object_id"]))
        if item["type"] == "link":
            item["action"] = "skip" if row else "create"
        else:
            item["action"] = "create" if not row else "skip" if row["content_hash"] == item["hash"] else "update"
        item["external_key"] = row["external_key"] if row else ""
        item["external_url"] = row["external_url"] if row else ""
        items.append(item)

    # ---- epics
    for nid in m.epics:
        owned = [r for r in m.functional if m.owner[r.id] == (None if nid == UNASSIGNED else nid)]
        if nid == UNASSIGNED:
            summary = f"{project_name}: requirements not on the architecture yet"
            lines = ["Functional requirements no component implements yet. Link them to components in ArchFlow to "
                     "move their stories under the right epic."]
            components, labels, source = [], [], {"owned": [r.id for r in owned]}
        else:
            n = m.top[nid]
            summary = n.name
            lines = [n.description or f"Build {_node_label(n)}."]
            lines += ["", "## Component", f"- {_node_label(n)}"]
            deps = m.epic_deps.get(nid) or []
            if deps:
                lines += ["", "## Depends on"] + [f"- {m.top[d].name}" for d in deps]
            if m.constraints.get(nid):
                lines += ["", "## Quality targets"] + [f"- {c.id} {c.title}: {c.description}".rstrip(": ")
                                                        for c in m.constraints[nid]]
            components, labels = [n.name], [f"type-{n.type}"]
            source = {"node": [n.name, n.type, n.technology, n.description], "deps": [m.top[d].name for d in deps],
                      "nfr": [[c.id, c.title, c.description] for c in m.constraints.get(nid, [])],
                      "owned": [r.id for r in owned]}
        if m.system_nfrs:
            lines += ["", "## System-wide quality targets"] + [f"- {c.id} {c.title}: {c.description}".rstrip(": ")
                                                               for c in m.system_nfrs]
            source["system_nfr"] = [[c.id, c.title, c.description] for c in m.system_nfrs]
        lines += ["", "## Stories"] + [f"- {r.id} {r.title}" for r in owned]
        oid = f"epic-{nid}"
        add({"ref": f"epic:{nid}", "type": "epic", "object_kind": "epic", "object_id": nid, "summary": summary[:200],
             "description": "\n".join(lines + [""] + _footer(project_name, summary)),
             "labels": ["archflow", dedupe_label(project_id, oid), *labels], "components": components,
             "parent": None, "requirement_ids": [r.id for r in owned]}, source)

    # ---- stories and their sub-tasks
    subtask_items: list[tuple[dict[str, Any], Any]] = []
    for r in m.functional:
        st, inp = stories[r.id], story_input(m, r)
        epic = m.owner[r.id] or UNASSIGNED
        lines = [st["user_story"]]
        if r.description:
            lines += ["", r.description]
        lines += ["", "## Acceptance criteria"] + [f"- {gwt_text(c)}" for c in st["acceptance_criteria"]]
        if inp["components"]:
            lines += ["", "## Architecture"] + [f"- {_node_label(m.top[n])}" for n in m.linked[r.id]]
        if inp["low_level_elements"]:
            lines += ["", "## Low-level design"] + [f"- {e}" for e in inp["low_level_elements"]]
        if inp["depends_on"]:
            lines += ["", "## Depends on"] + [f"- {d}" for d in inp["depends_on"]]
        if inp["constraints"]:
            lines += ["", "## Quality targets"] + [f"- {c}" for c in inp["constraints"]]
        lines += ["", "## Estimate", f"- {st['story_points']} story points: {st['points_basis']}"]
        labels = ["archflow", dedupe_label(project_id, r.id), f"priority-{r.priority}"]
        if r.category:
            labels.append(f"category-{slug(r.category, 40)}")
        add({"ref": f"story:{r.id}", "type": "story", "object_kind": "requirement", "object_id": r.id,
             "summary": f"{st['summary']}"[:200], "description": "\n".join(lines + [""] + _footer(project_name, r.id)),
             "labels": labels, "components": [m.top[n].name for n in m.linked[r.id]],
             "acceptance_criteria": st["acceptance_criteria"], "story_points": st["story_points"],
             "points_basis": st["points_basis"], "parent": f"epic:{epic}", "requirement_ids": [r.id]},
            # the hash covers the ArchFlow content, not the AI's wording: an unchanged requirement is skipped
            {"req": r.model_dump(include={"title", "description", "priority", "category", "actors",
                                          "acceptance_criteria"}),
             "input": inp, "epic": epic})
        for el in m.subtasks.get(r.id, []):
            parent = m.top[m.top_of(el.id) or ""] if m.top_of(el.id) else None
            props = ", ".join(f"{k}: {v}" for k, v in sorted(el.props.items()) if k in ("method", "path", "columns", "channel"))
            desc = [el.description or f"Implement the {el.type} {el.name} for {r.title}."]
            if props:
                desc += ["", "## Details", f"- {props}"]
            desc += ["", "## Part of", f"- {parent.name if parent else ''} ({r.id} {r.title})"]
            oid = f"{r.id}:{el.id}"
            subtask_items.append(({"ref": f"subtask:{oid}", "type": "subtask", "object_kind": "subtask",
                                   "object_id": oid, "summary": f"{el.type.capitalize()}: {el.name}"[:200],
                                   "description": "\n".join(desc + [""] + _footer(project_name, oid)),
                                   "labels": ["archflow", dedupe_label(project_id, oid), f"lld-{el.type}"],
                                   "components": [parent.name] if parent else [], "parent": f"story:{r.id}",
                                   "requirement_ids": [r.id], "node_id": el.id},
                                  {"el": [el.name, el.type, el.description, props], "req": [r.id, r.title]}))
    for item, source in subtask_items:
        add(item, source)

    # ---- dependencies between epics
    for nid, deps in m.epic_deps.items():
        for d in deps:
            oid = f"{d}->{nid}"
            add({"ref": f"link:{oid}", "type": "link", "object_kind": "link", "object_id": oid,
                 "summary": f"{m.top[d].name} blocks {m.top[nid].name}", "link_type": LINK_TYPE,
                 "blocker": f"epic:{d}", "blocked": f"epic:{nid}", "parent": None, "requirement_ids": []},
                [d, nid])

    counts = {a: sum(i["action"] == a for i in items) for a in ("create", "update", "skip")}
    by_type = {t: sum(i["type"] == t for i in items) for t in ("epic", "story", "subtask", "link")}
    return {"engine": engine, "items": items, "counts": counts, "by_type": by_type,
            "hash": content_hash([i["hash"] for i in items]),
            "mapping": "One epic per component that owns functional requirements; one story per functional "
                       "requirement; sub-tasks from the low-level design; 'blocks' links from connections."}


# --------------------------------------------------------------------------- ADF (Jira's description format)
def adf(text: str) -> dict[str, Any]:
    """Our small Markdown subset (## headings, - bullets, --- rules, paragraphs) as an Atlassian Document."""
    content: list[dict[str, Any]] = []
    bullets: list[dict[str, Any]] = []
    para: list[str] = []

    def t(s: str) -> list[dict[str, Any]]:
        return [{"type": "text", "text": s}] if s else []

    def flush() -> None:
        nonlocal bullets, para
        if para:
            content.append({"type": "paragraph", "content": t(" ".join(para))})
            para = []
        if bullets:
            content.append({"type": "bulletList", "content": bullets})
            bullets = []

    for line in text.splitlines():
        s = line.rstrip()
        if s.startswith("- "):
            if para:
                content.append({"type": "paragraph", "content": t(" ".join(para))})
                para = []
            bullets.append({"type": "listItem", "content": [{"type": "paragraph", "content": t(s[2:])}]})
            continue
        if not s:
            flush()
        elif s.startswith("## "):
            flush()
            content.append({"type": "heading", "attrs": {"level": 3}, "content": t(s[3:])})
        elif s == "---":
            flush()
            content.append({"type": "rule"})
        else:
            if bullets:
                flush()
            para.append(s)
    flush()
    return {"type": "doc", "version": 1, "content": content}


# --------------------------------------------------------------------------- pushing
def project_meta(client: JiraClient, project_key: str) -> dict[str, Any]:
    """Issue types, existing components and the story-point / epic-name fields of the target project."""
    p = client.project(project_key)
    types = p.get("issueTypes") or []

    def pick(*tests: Callable[[dict[str, Any]], bool]) -> str | None:
        for test in tests:
            for it in types:
                if test(it):
                    return str(it.get("id"))
        return None

    meta = {
        "key": p.get("key") or project_key, "name": p.get("name") or project_key,
        "epic": pick(lambda t: t.get("name", "").lower() == "epic", lambda t: t.get("hierarchyLevel") == 1),
        "story": pick(lambda t: t.get("name", "").lower() == "story", lambda t: t.get("name", "").lower() == "task",
                      lambda t: t.get("hierarchyLevel") == 0 and not t.get("subtask")),
        "subtask": pick(lambda t: bool(t.get("subtask")), lambda t: t.get("hierarchyLevel") == -1),
        "components": {c.get("name") for c in p.get("components") or [] if c.get("name")},
        "points_field": None, "epic_name_field": None,
    }
    try:
        for f in client.fields():
            name = str(f.get("name") or "").lower()
            if name in ("story point estimate", "story points") and not meta["points_field"]:
                meta["points_field"] = f.get("id")
            elif name == "epic name":
                meta["epic_name_field"] = f.get("id")
    except JiraError as exc:  # not fatal: the points stay in the description
        log.info("jira fields unavailable: %s", exc)
    return meta


def issue_fields(item: dict[str, Any], meta: dict[str, Any], parent_key: str | None, *, create: bool) -> dict[str, Any]:
    labels = list(item["labels"])
    comps = [c for c in item.get("components") or [] if c in meta["components"]]
    labels += [f"component-{slug(c, 40)}" for c in item.get("components") or [] if c not in meta["components"]]
    fields: dict[str, Any] = {"summary": item["summary"], "description": adf(item["description"]),
                              "labels": list(dict.fromkeys(labels)), "components": [{"name": c} for c in comps]}
    if item["type"] == "story" and meta.get("points_field") and item.get("story_points"):
        fields[meta["points_field"]] = item["story_points"]
    if parent_key and (create or item["type"] == "story"):  # a sub-task cannot change parent through an edit
        fields["parent"] = {"key": parent_key}
    if create:
        fields["project"] = {"key": meta["key"]}
        fields["issuetype"] = {"id": meta[item["type"]]}
        if item["type"] == "epic" and meta.get("epic_name_field"):
            fields[meta["epic_name_field"]] = item["summary"][:250]
    return fields


def find_by_label(client: JiraClient, project_key: str, label: str) -> str | None:
    issues = client.search(f'project = "{project_key}" AND labels = "{label}" ORDER BY created ASC', ["summary"], 2)
    return issues[0].get("key") if issues else None


def push(client: JiraClient, plan: dict[str, Any], *, store: Any, pid: str, project_key: str) -> dict[str, Any]:
    """Create or update every item that needs it, in order (epics, stories, sub-tasks, links). Each success is
    recorded immediately, so a failure part-way leaves the rest linked and a re-push resumes."""
    meta = project_meta(client, project_key)
    missing = [t for t in ("epic", "story", "subtask") if not meta[t]]
    if missing:
        raise JiraError(f"Jira project {project_key} has no {' / '.join(missing)} issue type.")
    keys: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    for item in plan["items"]:
        ref, action = item["ref"], item["action"]
        res = {"ref": ref, "type": item["type"], "summary": item["summary"], "action": action, "status": "ok",
               "key": item.get("external_key", ""), "url": item.get("external_url", ""), "error": ""}
        results.append(res)
        if action == "skip":
            res["status"] = "skipped"
            if item["external_key"]:
                keys[ref] = item["external_key"]
            continue
        try:
            if item["type"] == "link":
                blocker, blocked = keys.get(item["blocker"]), keys.get(item["blocked"])
                if not blocker or not blocked:
                    raise JiraError("an epic it links did not push")
                client.link_issues(LINK_TYPE, inward=blocked, outward=blocker)
                res["key"] = f"{blocker} blocks {blocked}"
                store.save_external_link(pid, "link", item["object_id"], external_key=res["key"],
                                         content_hash=item["hash"])
                continue
            parent_key = keys.get(item["parent"]) if item.get("parent") else None
            if item.get("parent") and not parent_key:
                raise JiraError(f"its parent ({item['parent'].split(':', 1)[1]}) did not push")
            key = item["external_key"] if action == "update" else ""
            if key:
                try:
                    client.update_issue(key, issue_fields(item, meta, parent_key, create=False))
                except JiraError as exc:
                    if exc.status != 404:
                        raise
                    store.delete_external_link(pid, item["object_kind"], item["object_id"])
                    key, res["action"] = "", "create"  # deleted in Jira: create it again
            if not key:
                found = find_by_label(client, meta["key"], item["labels"][1])
                if found:  # created earlier but never recorded: adopt it instead of duplicating
                    client.update_issue(found, issue_fields(item, meta, parent_key, create=False))
                    key, res["action"] = found, "update"
                else:
                    key = str(client.create_issue(issue_fields(item, meta, parent_key, create=True)).get("key") or "")
                    if not key:
                        raise JiraError("Jira did not return the new issue's key")
            url = browse_url(client.site, key)
            store.save_external_link(pid, item["object_kind"], item["object_id"], external_key=key,
                                     external_url=url, content_hash=item["hash"])
            keys[ref], res["key"], res["url"] = key, key, url
        except JiraError as exc:
            res["status"], res["error"] = "error", str(exc)
    counts = {"created": sum(r["status"] == "ok" and r["action"] == "create" for r in results),
              "updated": sum(r["status"] == "ok" and r["action"] == "update" for r in results),
              "unchanged": sum(r["status"] == "skipped" for r in results),
              "failed": sum(r["status"] == "error" for r in results)}
    return {"project_key": meta["key"], "results": results, "counts": counts}


def refresh_status(client: JiraClient, store: Any, pid: str) -> int:
    """Read every linked issue's status back into ``external_links``; returns how many were updated."""
    keys = list(dict.fromkeys(x["external_key"] for x in store.list_external_links(pid) if x["object_kind"] != "link"))
    n = 0
    for start in range(0, len(keys), 100):
        found, missing = client.bulk_status(keys[start:start + 100])
        for key, st in found.items():
            store.set_external_status(pid, key, st["status"], st["category"])
            n += 1
        for key in missing:
            store.set_external_status(pid, key, "Not found in Jira", "")
    return n


# --------------------------------------------------------------------------- routes
class JiraConnectionIn(BaseModel):
    site_url: str = Field(min_length=8, max_length=300)
    email: str = Field(min_length=3, max_length=200)
    api_token: str | None = Field(None, max_length=500)  # None or "": keep the stored token
    project_key: str = Field("", max_length=10)


class PlanIn(BaseModel):
    project_key: str | None = Field(None, max_length=10)
    ai: bool = True


class PushIn(PlanIn):
    confirm: bool = False


class _StoryCache:
    """AI-written stories per (project, branch content): preview and push show the same wording."""

    def __init__(self, size: int = 64, seconds: float = 1800) -> None:
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.size, self.seconds = size, seconds

    def get(self, key: str) -> Any:
        with self._lock:
            hit = self._data.get(key)
            if not hit or time.monotonic() - hit[0] > self.seconds:
                return None
            self._data.move_to_end(key)
            return hit[1]

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            while len(self._data) > self.size:
                self._data.popitem(last=False)


def settings_payload(store: Any, org_id: str) -> dict[str, Any]:
    """What the Jira settings screen shows. The token and webhook secret are never returned (only the token's
    last 4 characters, and whether a webhook secret exists)."""
    from . import jira_sync

    c = store.get_jira_connection(org_id)
    return {"enabled": secrets_box.enabled(), "configured": bool(c),
            "site_url": c["site_url"] if c else "", "email": c["email"] if c else "",
            "project_key": c["project_key"] if c else "", "last4": c["last4"] if c else None,
            "updated_at": c["updated_at"] if c else None,
            "webhook": {"configured": bool(c and c.get("webhook_ciphertext")),
                        "created_at": c.get("webhook_created_at") if c else None,
                        "url": jira_sync.webhook_url(org_id)}}


# Errors from reaching Jira with an org's stored connection.
CONNECTION_ERRORS = (JiraError, ValueError, secrets_box.SecretDecryptError, secrets_box.SecretsUnavailable)


class JiraBusy(JiraError):
    """Another push or import for the project is running."""


_stories_cache = _StoryCache()
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def project_lock(pid: str) -> threading.Lock:
    """One Jira push or import at a time per project."""
    with _locks_guard:
        return _locks.setdefault(pid, threading.Lock())


def make_plan(store: Any, pid: str, state: ProjectState, *, org_id: str, ai: bool) -> dict[str, Any]:
    """The push plan of a branch (AI-written stories are cached per content, so preview and push agree)."""
    project = store.get_project(pid) or {}
    m = Model(state)
    inputs = [story_input(m, r) for r in m.functional]
    ckey = content_hash([pid, project.get("name"), inputs, ai])
    cached = _stories_cache.get(ckey)
    if cached is None:
        cached = write_stories(m, project.get("name") or pid, use_ai=ai, org_id=org_id)
        if cached[1] == "llm":
            _stories_cache.put(ckey, cached)
    stories, engine = cached
    return build_plan(state, project_id=pid, project_name=project.get("name") or pid,
                      links=store.list_external_links(pid), stories=stories, engine=engine, model=m)


def push_project(store: Any, conn: dict[str, Any], pid: str, branch: str, get_state: Callable[[], ProjectState], *,
                 project_key: str, ai: bool, org_id: str, actor: str) -> dict[str, Any]:
    """Plan and push one branch, then log it. Raises JiraBusy, or one of CONNECTION_ERRORS."""
    lock = project_lock(pid)
    if not lock.acquire(blocking=False):
        raise JiraBusy("A Jira push or import for this project is already running.")
    try:
        plan = make_plan(store, pid, get_state(), org_id=org_id, ai=ai)
        with make_client(conn) as client:
            out = push(client, plan, store=store, pid=pid, project_key=project_key)
        c = out["counts"]
        line = (f"pushed to Jira {out['project_key']}: {c['created']} created, {c['updated']} updated, "
                f"{c['unchanged']} unchanged, {c['failed']} failed")
        store.log_activity(pid, branch, actor, [line], get_state().rev)
        from . import integrations  # imported here: integrations builds on this module
        integrations.notify(store, org_id, pid, "push", f"{actor} pushed to Jira", line, branch=branch)
        return {**out, "engine": plan["engine"]}
    finally:
        lock.release()


def sync_status(store: Any, conn: dict[str, Any], pid: str) -> int:
    """Pull every linked issue's status now (the fallback when webhooks are not set up)."""
    with make_client(conn) as client:
        return refresh_status(client, store, pid)


def register_routes(app: FastAPI, store: Any, *, authz: Any, state_or_404: Callable[[str, str], ProjectState],
                    broadcast: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None = None,
                    commit_ops: Callable[..., Awaitable[Any]] | None = None) -> None:
    read, edit, org_admin = authz.project("read"), authz.project("edit"), authz.org("admin")

    def connection_or_409(org_id: str) -> dict[str, Any]:
        c = store.get_jira_connection(org_id)
        if not c:
            raise HTTPException(409, "Jira is not connected for this organization: an admin connects it in the "
                                     "organization's Jira settings.")
        return c

    def target_key(body: PlanIn, conn: dict[str, Any]) -> str:
        try:
            return check_project_key(body.project_key or conn["project_key"])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    def plan_for(pid: str, branch: str, body: PlanIn, org_id: str) -> dict[str, Any]:
        return make_plan(store, pid, state_or_404(pid, branch), org_id=org_id, ai=body.ai)

    # ---- org connection (admin+) ----
    @app.get("/api/orgs/{org_id}/jira")
    def get_jira(a=Depends(org_admin)):
        return settings_payload(store, a.org_id)

    @app.put("/api/orgs/{org_id}/jira")
    def put_jira(body: JiraConnectionIn, a=Depends(org_admin)):
        if not secrets_box.enabled():
            raise HTTPException(503, "Storing the Jira token needs ARCHFLOW_SECRET_KEY (a long random string) in the "
                                     "API's environment. Set it and restart the API.")
        try:
            site = check_site_url(body.site_url)
            key = check_project_key(body.project_key) if body.project_key.strip() else ""
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        email_ = users.normalize_email(body.email)
        if not users.valid_email(email_):
            raise HTTPException(422, "Enter the email address of the Atlassian account that owns the API token.")
        token = (body.api_token or "").strip()
        current = store.get_jira_connection(a.org_id)
        if token:
            if len(token) < 8:
                raise HTTPException(422, "The API token looks too short.")
            ciphertext, last4 = secrets_box.encrypt(token, token_aad(a.org_id)), token[-4:]
        elif current:
            ciphertext, last4 = current["ciphertext"], current["last4"]
        else:
            raise HTTPException(422, "Enter an API token (create one at https://id.atlassian.com/manage-profile/"
                                     "security/api-tokens).")
        store.set_jira_connection(a.org_id, site_url=site, email=email_, ciphertext=ciphertext, last4=last4,
                                  project_key=key, user_id=a.user["id"])
        return settings_payload(store, a.org_id)

    @app.delete("/api/orgs/{org_id}/jira", status_code=204)
    def delete_jira(a=Depends(org_admin)):
        store.delete_jira_connection(a.org_id)

    @app.post("/api/orgs/{org_id}/jira/test")
    async def test_jira(a=Depends(org_admin)):
        """Check the saved settings: sign in as the account and open the default project."""
        conn = connection_or_409(a.org_id)

        def run() -> dict[str, Any]:
            try:
                with make_client(conn) as client:
                    me = client.myself()
                    out: dict[str, Any] = {"ok": True, "account": me.get("displayName") or me.get("emailAddress") or ""}
                    if conn["project_key"]:
                        meta = project_meta(client, conn["project_key"])
                        out["project"] = {"key": meta["key"], "name": meta["name"],
                                          "issue_types": [t for t in ("epic", "story", "subtask") if meta[t]],
                                          "story_points": bool(meta["points_field"])}
                    return out
            except (JiraError, ValueError, secrets_box.SecretDecryptError, secrets_box.SecretsUnavailable) as exc:
                return {"ok": False, "error": str(exc)}

        return await asyncio.to_thread(run)

    # ---- per project ----
    @app.get("/api/projects/{pid}/jira/links")
    def jira_links(pid: str, a=Depends(read)):
        conn = store.get_jira_connection(a.org_id)
        return {"connected": bool(conn), "site_url": conn["site_url"] if conn else "",
                "project_key": conn["project_key"] if conn else "", "links": store.list_external_links(pid)}

    @app.post("/api/projects/{pid}/jira/plan")
    async def jira_plan(pid: str, body: PlanIn, branch: str = "main", a=Depends(edit)):
        """Dry run: the full plan with create / update / skip per item. Makes no request to Jira."""
        conn = connection_or_409(a.org_id)
        key = target_key(body, conn)
        plan = await asyncio.to_thread(plan_for, pid, branch, body, a.org_id)
        return {**plan, "project_key": key, "site_url": conn["site_url"], "dry_run": True}

    @app.post("/api/projects/{pid}/jira/push")
    async def jira_push(pid: str, body: PushIn, branch: str = "main", a=Depends(edit)):
        if not body.confirm:
            raise HTTPException(422, "Preview the plan first, then push with confirm: true.")
        conn = connection_or_409(a.org_id)
        key = target_key(body, conn)

        def run() -> dict[str, Any]:
            try:
                return push_project(store, conn, pid, branch, lambda: state_or_404(pid, branch), project_key=key,
                                    ai=body.ai, org_id=a.org_id, actor=a.actor)
            except JiraBusy as exc:
                raise HTTPException(409, str(exc)) from exc
            except CONNECTION_ERRORS as exc:
                raise HTTPException(502, f"Jira push failed: {exc}") from exc

        out = await asyncio.to_thread(run)
        if broadcast:
            await broadcast(pid, branch, {"type": "jira", "actor": a.actor})
            await broadcast(pid, branch, {"type": "activity", "actor": a.actor})
        return out

    @app.post("/api/projects/{pid}/jira/refresh")
    async def jira_refresh(pid: str, branch: str = "main", a=Depends(edit)):
        """Pull each linked issue's status from Jira (ArchFlow requirement status is left as it is)."""
        conn = connection_or_409(a.org_id)

        def run() -> int:
            try:
                return sync_status(store, conn, pid)
            except CONNECTION_ERRORS as exc:
                raise HTTPException(502, f"Could not read Jira: {exc}") from exc

        n = await asyncio.to_thread(run)
        if broadcast:
            await broadcast(pid, branch, {"type": "jira", "actor": a.actor})
        return {"updated": n, "links": store.list_external_links(pid)}

    # ---- two-way sync: webhooks, pulling issues in as requirements (jira_sync.py) ----
    from . import jira_sync

    jira_sync.register_routes(app, store, authz=authz, commit_ops=commit_ops, broadcast=broadcast,
                              connection_or_409=connection_or_409)
