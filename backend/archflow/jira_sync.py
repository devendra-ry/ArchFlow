"""Two-way Jira sync: live status from Jira webhooks, an on-demand "sync now", and pulling Jira issues back into
ArchFlow as requirements.

**Webhooks.** An admin generates a per-org secret (shown once, stored encrypted like the API token). Jira calls
``POST /api/integrations/jira/webhook/{org_id}?secret=<secret>``; when the webhook also has Jira's own "Secret"
set to the same value, the ``X-Hub-Signature: sha256=<hmac>`` header is accepted instead of the query parameter.
Both are checked in constant time. ``jira:issue_created`` / ``jira:issue_updated`` update the status of every
linked object in the org's projects; ``jira:issue_deleted`` removes those links (a later push creates the issue
again) and logs it. Each affected project gets a ``{"type": "jira"}`` room message so open canvases refresh.

**Pulling issues in.** The configured project's issues that no object of this ArchFlow project is linked to are
listed page by page (JQL, Jira's ``nextPageToken``), each with a preview of the requirement it would become.
Importing maps summary → title, description (ADF → text) → description, labels / issue type → functional or
non-functional (and its category), priority → priority, and acceptance criteria parsed from the description.
The ``jira.import`` agent can do the mapping when AI is on; the same rules are the offline fallback and fill any
field the AI leaves out. Each imported issue gets an ``external_links`` row, so it is never imported twice, and
the row's content hash matches the push plan, so the next push leaves the issue alone until the requirement
changes in ArchFlow.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import secrets
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from . import agents, auth, jira, llm, secrets_box
from .db.types import now
from .models import ProjectState

log = logging.getLogger("archflow.jira_sync")

WEBHOOK_EVENTS = ("jira:issue_created", "jira:issue_updated", "jira:issue_deleted")
MAX_WEBHOOK_BYTES = 1_000_000
ISSUE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,9}-\d{1,9}$")
IMPORT_PROMPT = "jira.import"
IMPORT_FIELDS = ["summary", "description", "issuetype", "priority", "labels", "status"]
MAX_IMPORT = 50
MAX_PAGE = 50
AI_BATCH = 10
DELETED = "Deleted in Jira"

NFR_LABELS = {"nfr", "non-functional", "nonfunctional", "non_functional", "quality", "quality-attribute",
              "quality-requirement"}
NFR_TYPES = {"non-functional requirement", "non-functional", "nfr", "quality", "quality requirement"}
CATEGORIES = ("performance", "security", "availability", "scalability", "reliability", "usability",
              "accessibility", "compliance", "privacy", "cost", "maintainability", "observability")
CATEGORY_ALIASES = {"latency": "performance", "throughput": "performance", "uptime": "availability",
                    "gdpr": "compliance", "a11y": "accessibility", "monitoring": "observability"}
PRIORITY_MAP = {**dict.fromkeys(("highest", "blocker", "critical", "urgent", "p0"), "critical"),
                **dict.fromkeys(("high", "major", "p1"), "high"),
                **dict.fromkeys(("medium", "normal", "p2"), "medium"),
                **dict.fromkeys(("low", "lowest", "minor", "trivial", "p3", "p4"), "low")}
_AC_HEADER = re.compile(r"^\s*(acceptance criteria|acceptance|ac|definition of done|dod)\s*:?\s*$", re.I)
_SECTION_HEADER = re.compile(r"^[A-Z][\w /&()-]{0,40}:$")
_GWT = re.compile(r"^\s*given\b.+\bwhen\b.+\bthen\b", re.I | re.S)
_ACTOR = re.compile(r"\bas an? ([^,.;]{2,60}?),?\s+i (?:want|need|can|would like)", re.I)


# --------------------------------------------------------------------------- webhook secret
def webhook_aad(org_id: str) -> str:
    return f"org-jira-webhook:{org_id}"


def webhook_url(org_id: str) -> str:
    return f"{auth.PUBLIC_URL}/api/integrations/jira/webhook/{org_id}"


def verify_webhook(conn: dict[str, Any] | None, secret: str, signature: str, body: bytes) -> bool:
    """True when the query secret, or the HMAC-SHA256 signature of the body, matches the org's secret."""
    if not conn or not conn.get("webhook_ciphertext"):
        return False
    try:
        stored = secrets_box.decrypt(conn["webhook_ciphertext"], webhook_aad(conn["org_id"]))
    except (secrets_box.SecretDecryptError, secrets_box.SecretsUnavailable):
        return False
    ok = False
    if secret:
        ok = hmac.compare_digest(secret.encode(), stored.encode())
    algo, _, digest = (signature or "").partition("=")
    if not ok and algo.strip().lower() == "sha256" and digest:
        expected = hmac.new(stored.encode(), body, hashlib.sha256).hexdigest()
        ok = hmac.compare_digest(digest.strip().lower().encode(), expected.encode())
    return ok


def setup_instructions(org_id: str, secret: str, project_key: str) -> dict[str, Any]:
    url = f"{webhook_url(org_id)}?secret={secret}"
    jql = f'project = "{project_key}"' if project_key else "project = <YOUR PROJECT KEY>"
    return {"secret": secret, "url": url, "jql": jql, "events": list(WEBHOOK_EVENTS),
            "steps": ["In Jira, open Settings (gear) → System → WebHooks, and click Create a WebHook.",
                      "Paste the URL below. Optionally paste the secret into Jira's Secret field too.",
                      f"Under Issue related events, enter the JQL filter: {jql}",
                      "Tick Issue: created, updated and deleted, then save.",
                      "Copy the URL now: ArchFlow will not show the secret again."]}


# --------------------------------------------------------------------------- webhook events
def handle_webhook(store: Any, org_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one Jira event to the org's links. Returns the affected projects (to broadcast to)."""
    event = str(payload.get("webhookEvent") or "")
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    key = str(issue.get("key") or "")
    if event not in WEBHOOK_EVENTS or not ISSUE_KEY_RE.match(key):
        return {"event": event, "key": key, "ignored": True, "projects": [], "updated": 0, "removed": 0}
    rows = store.find_external_links_by_key(org_id, key)
    projects = sorted({r["project_id"] for r in rows})
    out = {"event": event, "key": key, "ignored": False, "projects": projects, "updated": 0, "removed": 0}
    if event == "jira:issue_deleted":
        for pid in projects:
            objs = [r["object_id"] for r in rows if r["project_id"] == pid]
            out["removed"] += store.delete_external_links_by_key(pid, key)
            state = store.get_state(pid, "main")
            store.log_activity(pid, "main", "Jira", [f"Jira issue {key} was deleted; unlinked {', '.join(objs)}"],
                               state.rev if state else 0)
        return out
    st = (issue.get("fields") or {}).get("status") or {}
    name = str(st.get("name") or "")
    category = str((st.get("statusCategory") or {}).get("key") or "")
    if name:
        for pid in projects:
            store.set_external_status(pid, key, name, category)
            out["updated"] += sum(r["project_id"] == pid for r in rows)
    return out


# --------------------------------------------------------------------------- ADF → text
def _inline(nodes: list[Any]) -> str:
    parts: list[str] = []
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        t = n.get("type")
        attrs = n.get("attrs") or {}
        if t == "text":
            parts.append(str(n.get("text") or ""))
        elif t == "hardBreak":
            parts.append("\n")
        elif t == "mention":
            parts.append(str(attrs.get("text") or "@someone"))
        elif t == "emoji":
            parts.append(str(attrs.get("text") or attrs.get("shortName") or ""))
        elif t in ("inlineCard", "blockCard", "embedCard"):
            parts.append(str(attrs.get("url") or ""))
        elif t == "status":
            parts.append(str(attrs.get("text") or ""))
        elif t == "date":
            parts.append(str(attrs.get("timestamp") or ""))
        elif n.get("content"):
            parts.append(_inline(n["content"]))
    return "".join(parts)


def adf_blocks(doc: Any) -> list[tuple[str, str]]:
    """An Atlassian Document (or Jira wiki text) as (kind, text) blocks: heading | para | item | code | rule."""
    if isinstance(doc, str):
        out: list[tuple[str, str]] = []
        for line in doc.splitlines():
            s = line.strip()
            if not s:
                continue
            if m := re.match(r"^h[1-6]\.\s*(.+)$", s):
                out.append(("heading", m.group(1).strip()))
            elif m := re.match(r"^(?:[*#-]+|\d+[.)])\s+(.+)$", s):
                out.append(("item", m.group(1).strip()))
            elif s.startswith("##"):
                out.append(("heading", s.lstrip("#").strip()))
            else:
                out.append(("para", s))
        return out
    blocks: list[tuple[str, str]] = []

    def walk(nodes: list[Any], depth: int = 0) -> None:
        for n in nodes or []:
            if not isinstance(n, dict):
                continue
            t = n.get("type")
            if t == "heading":
                blocks.append(("heading", _inline(n.get("content")).strip()))
            elif t == "paragraph":
                text = _inline(n.get("content")).strip()
                if text:
                    blocks.append(("para", text))
            elif t in ("bulletList", "orderedList", "taskList"):
                for item in n.get("content") or []:
                    inner = item.get("content") or [] if isinstance(item, dict) else []
                    text = _inline([c for c in inner if c.get("type") not in ("bulletList", "orderedList")]
                                   if item.get("type") == "listItem" else [item]).strip()
                    if text:
                        blocks.append(("item", ("  " * depth) + text))
                    walk([c for c in inner if c.get("type") in ("bulletList", "orderedList", "taskList")], depth + 1)
            elif t == "codeBlock":
                blocks.append(("code", _inline(n.get("content"))))
            elif t == "rule":
                blocks.append(("rule", ""))
            elif t == "table":
                for row in n.get("content") or []:
                    cells = [_inline(c.get("content") and [x for p in c["content"] for x in (p.get("content") or [])])
                             .strip() for c in row.get("content") or [] if isinstance(c, dict)]
                    if any(cells):
                        blocks.append(("para", " | ".join(cells)))
            elif t in ("blockquote", "panel", "expand", "nestedExpand", "layoutSection", "layoutColumn", "doc"):
                walk(n.get("content") or [], depth)
            elif t in ("mediaSingle", "mediaGroup", "media"):
                continue
            elif n.get("content"):
                walk(n["content"], depth)

    if isinstance(doc, dict):
        walk(doc.get("content") or [] if doc.get("type") == "doc" else [doc])
    return blocks


def blocks_text(blocks: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    for kind, text in blocks:
        if kind == "item":
            lines.append(" " * (len(text) - len(text.lstrip())) + "- " + text.strip())
            continue
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("---" if kind == "rule" else text.rstrip())
    return "\n".join(lines).strip()


def adf_to_text(doc: Any) -> str:
    """Plain text of a Jira description: paragraphs, headings, "- " list items, code and tables."""
    return blocks_text(adf_blocks(doc))


def split_criteria(blocks: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """(description without its acceptance-criteria section, the criteria). Criteria are the items (or short
    paragraphs) under an "Acceptance criteria" / "Definition of done" heading, plus any Given/When/Then line."""
    body: list[tuple[str, str]] = []
    criteria: list[str] = []
    in_ac = False
    for kind, text in blocks:
        clean = text.strip()
        if kind in ("heading", "para") and _AC_HEADER.match(clean):
            in_ac = True
            continue
        if in_ac:
            if kind == "heading" or kind == "rule" or (kind == "para" and _SECTION_HEADER.match(clean)):
                in_ac = False
            elif kind in ("item", "para") and clean and len(clean) <= 400:
                criteria.append(re.sub(r"^\[[ xX]?\]\s*", "", clean))
                continue
        if kind in ("para", "item") and _GWT.match(clean):
            criteria.append(clean)
            continue
        body.append((kind, text))
    return blocks_text(body), list(dict.fromkeys(c for c in criteria if c))[:12]


# --------------------------------------------------------------------------- issue → requirement (rules)
def issue_info(issue: dict[str, Any], site: str) -> dict[str, Any]:
    """The parts of a Jira issue the import uses, normalized."""
    f = issue.get("fields") or {}
    st = f.get("status") or {}
    key = str(issue.get("key") or "")
    blocks = adf_blocks(f.get("description"))
    return {"key": key, "url": jira.browse_url(site, key) if site and key else "",
            "summary": str(f.get("summary") or "").strip(),
            "issue_type": str((f.get("issuetype") or {}).get("name") or ""),
            "priority": str((f.get("priority") or {}).get("name") or ""),
            "labels": [str(x) for x in f.get("labels") or []],
            "status": str(st.get("name") or ""),
            "status_category": str((st.get("statusCategory") or {}).get("key") or ""),
            "description": blocks_text(blocks), "_blocks": blocks}


def _category(words: list[str]) -> str:
    for w in words:
        w = w.lower().removeprefix("category-").removeprefix("nfr-")
        w = CATEGORY_ALIASES.get(w, w)
        if w in CATEGORIES:
            return w
    return ""


def map_issue(info: dict[str, Any]) -> dict[str, Any]:
    """The requirement an issue becomes, by rules."""
    labels = [x.lower() for x in info["labels"]]
    body, criteria = split_criteria(info.get("_blocks") or adf_blocks(info.get("description") or ""))
    nfr = bool(NFR_LABELS & set(labels)) or info["issue_type"].lower() in NFR_TYPES \
        or any(x.startswith("nfr-") for x in labels)
    category = _category(labels) if nfr else ""
    actors = [m.group(1).strip() for m in _ACTOR.finditer(f"{info['summary']}\n{body}")][:4]
    return {"title": (info["summary"] or info["key"])[:200], "description": body[:4000],
            "kind": "non_functional" if nfr else "functional", "category": category,
            "priority": PRIORITY_MAP.get(info["priority"].lower(), "medium"),
            "actors": list(dict.fromkeys(actors)), "acceptance_criteria": criteria,
            "status": "implemented" if info["status_category"] == "done" else "draft"}


IMPORT_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"requirements": {"type": "array", "items": {
    "type": "object", "properties": {
        "key": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"},
        "kind": {"type": "string", "enum": ["functional", "non_functional"]}, "category": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "actors": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}}},
    "required": ["key", "title", "kind", "priority", "acceptance_criteria"]}}},
    "required": ["requirements"]}


def clean_mapping(raw: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """An AI mapping, validated; anything missing or invalid comes from the rules."""
    def text(v: Any, limit: int) -> str:
        return str(v).strip()[:limit] if isinstance(v, str) else ""

    def strings(v: Any, limit: int, n: int) -> list[str]:
        return [s for s in (text(x, limit) for x in v or [] if isinstance(x, str)) if s][:n] if isinstance(v, list) else []

    kind = raw.get("kind") if raw.get("kind") in ("functional", "non_functional") else fallback["kind"]
    category = _category([text(raw.get("category"), 40)]) if kind == "non_functional" else ""
    return {"title": text(raw.get("title"), 200) or fallback["title"],
            "description": text(raw.get("description"), 4000) or fallback["description"],
            "kind": kind, "category": category or (fallback["category"] if kind == "non_functional" else ""),
            "priority": raw.get("priority") if raw.get("priority") in jira.PRIORITY_RANK else fallback["priority"],
            "actors": strings(raw.get("actors"), 60, 4) or fallback["actors"],
            "acceptance_criteria": strings(raw.get("acceptance_criteria"), 400, 12) or fallback["acceptance_criteria"],
            "status": fallback["status"]}


def map_issues(infos: list[dict[str, Any]], *, use_ai: bool, org_id: str | None,
               version: str | None = None) -> tuple[dict[str, dict[str, Any]], str]:
    """({issue key: requirement fields}, engine). The ``jira.import`` agent maps when AI is on; rules otherwise."""
    offline = {i["key"]: map_issue(i) for i in infos}
    if not infos or not use_ai or not llm.available(org_id):
        return offline, "offline"
    raw: dict[str, dict[str, Any]] = {}
    try:
        with llm.usage_scope(feature="jira"):
            for start in range(0, len(infos), AI_BATCH):
                batch = [{k: i[k] for k in ("key", "summary", "issue_type", "priority", "labels", "description")}
                         for i in infos[start:start + AI_BATCH]]
                for b in batch:
                    b["description"] = b["description"][:6000]
                data = agents.run_json(IMPORT_PROMPT, IMPORT_SCHEMA, org_id=org_id, version=version,
                                       question=f"Map Jira issues {', '.join(b['key'] for b in batch)} to requirements",
                                       variables={"issues_json": json.dumps(batch, indent=1)})
                for r in data.get("requirements") or []:
                    if isinstance(r, dict) and r.get("key") in offline:
                        raw.setdefault(r["key"], r)
    except (llm.LLMUnavailable, llm.LLMRefused, llm.LLMInvalidOutput) as exc:
        log.info("jira import: AI unavailable (%s), using rules", type(exc).__name__)
        return offline, "offline"
    return {k: clean_mapping(raw[k], fb) if k in raw else fb for k, fb in offline.items()}, "llm"


# --------------------------------------------------------------------------- listing and importing
def _jql_text(q: str) -> str:
    return re.sub(r'["\\]', " ", q).strip()[:100]


def linked_keys(store: Any, pid: str) -> set[str]:
    return {x["external_key"] for x in store.list_external_links(pid)}


def list_unlinked(client: jira.JiraClient, store: Any, pid: str, project_key: str, *, page_token: str | None = None,
                  limit: int = 25, query: str = "") -> dict[str, Any]:
    """One page of the project's issues that nothing in this ArchFlow project links to, each with a preview of the
    requirement it would become (rules). Linked issues are skipped, so a page may read more than one Jira page."""
    limit = max(1, min(limit, MAX_PAGE))
    linked = linked_keys(store, pid)
    jql = f'project = "{project_key}" AND issuetype not in subTaskIssueTypes()'
    if _jql_text(query):
        jql += f' AND text ~ "{_jql_text(query)}"'
    jql += " ORDER BY created DESC"
    issues: list[dict[str, Any]] = []
    hidden, token, reads = 0, page_token, 0
    while len(issues) < limit and reads < 5:
        page, token = client.search_page(jql, IMPORT_FIELDS, limit, token)
        reads += 1
        for raw in page:
            if raw.get("key") in linked:
                hidden += 1
                continue
            info = issue_info(raw, client.site)
            mapped = map_issue(info)
            info.pop("_blocks", None)
            info["description"] = info["description"][:600]
            issues.append({**info, "requirement": mapped})
        if not token:
            break
    return {"project_key": project_key, "jql": jql, "issues": issues, "next_page_token": token,
            "hidden_linked": hidden}


async def import_issues(store: Any, conn: dict[str, Any], pid: str, branch: str, keys: list[str], *, ai: bool,
                        org_id: str, actor: str,
                        commit_ops: Callable[..., Awaitable[tuple[ProjectState, list[str]]]],
                        get_state: Callable[[], ProjectState]) -> dict[str, Any]:
    """Import these issues as requirements on ``branch``. Issues already linked to this project (or imported on
    this branch) are skipped. Raises jira.JiraBusy, ImportInputError, or one of jira.CONNECTION_ERRORS."""
    wanted = list(dict.fromkeys(k.strip().upper() for k in keys if k and k.strip()))
    bad = [k for k in wanted if not ISSUE_KEY_RE.match(k)]
    if bad:
        raise ImportInputError(f"Not a Jira issue key: {', '.join(bad[:5])}")
    if not wanted:
        raise ImportInputError("Pick at least one issue to import.")
    if len(wanted) > MAX_IMPORT:
        raise ImportInputError(f"Import at most {MAX_IMPORT} issues at a time.")
    lock = jira.project_lock(pid)
    if not lock.acquire(blocking=False):
        raise jira.JiraBusy("A Jira push or import for this project is already running.")
    try:
        state = get_state()
        on_branch = {r.source.get("ref") for r in state.requirements.values()
                     if not r.deleted and r.source.get("type") == "jira"}
        linked = linked_keys(store, pid)
        skipped = [{"key": k, "reason": "already linked"} for k in wanted if k in linked or k in on_branch]
        todo = [k for k in wanted if k not in linked and k not in on_branch]
        infos: list[dict[str, Any]] = []
        if todo:
            def fetch() -> tuple[list[dict[str, Any]], list[str]]:
                with jira.make_client(conn) as client:
                    found, missing = client.get_issues(todo, IMPORT_FIELDS)
                    return [issue_info(i, client.site) for i in found], missing

            infos, missing = await asyncio.to_thread(fetch)
            skipped += [{"key": k, "reason": "not found in Jira"} for k in missing]
            infos = [i for i in infos if i["key"] in todo]
        mapped, engine = await asyncio.to_thread(map_issues, infos, use_ai=ai, org_id=org_id) if infos \
            else ({}, "offline")
        at = now()
        ops = [{"op": "add_requirement", "requirement": {
            **mapped[i["key"]], "source": {"type": "jira", "ref": i["key"], "url": i["url"], "at": at}}}
            for i in infos]
        imported: list[dict[str, Any]] = []
        if ops:
            state, _ = await commit_ops(pid, branch, ops, f"{actor} (Jira import)")
            new = {r.source.get("ref"): r for r in state.requirements.values()
                   if r.rev == state.rev and r.source.get("type") == "jira"}
            # the hash the push plan computes for each story: the next push leaves the issue as it is
            plan = jira.build_plan(state, project_id=pid, project_name=(store.get_project(pid) or {}).get("name") or pid,
                                   links=[])
            hashes = {i["object_id"]: i["hash"] for i in plan["items"] if i["object_kind"] == "requirement"}
            for info in infos:
                r = new.get(info["key"])
                if not r:
                    continue
                store.save_external_link(pid, "requirement", r.id, external_key=info["key"], external_url=info["url"],
                                         content_hash=hashes.get(r.id, ""))
                if info["status"]:
                    store.set_external_status(pid, info["key"], info["status"], info["status_category"])
                imported.append({"key": info["key"], "requirement_id": r.id, "title": r.title, "kind": r.kind})
        return {"imported": imported, "skipped": skipped, "engine": engine}
    finally:
        lock.release()


# --------------------------------------------------------------------------- routes
class ImportInputError(ValueError):
    """The import request itself is wrong (not Jira's answer)."""


class ImportIn(BaseModel):
    keys: list[str] = Field(min_length=1, max_length=MAX_IMPORT)
    ai: bool = True


def register_routes(app: FastAPI, store: Any, *, authz: Any,
                    commit_ops: Callable[..., Awaitable[tuple[ProjectState, list[str]]]] | None,
                    broadcast: Callable[[str, str, dict[str, Any]], Awaitable[None]] | None,
                    connection_or_409: Callable[[str], dict[str, Any]]) -> None:
    edit, org_admin = authz.project("edit"), authz.org("admin")

    async def tell(pid: str, msg: dict[str, Any], branches: list[str] | None = None) -> None:
        if not broadcast:
            return
        for b in branches or [x["name"] for x in store.list_branches(pid)]:
            await broadcast(pid, b, msg)

    # ---- webhook secret (admin+): shown once ----
    @app.post("/api/orgs/{org_id}/jira/webhook")
    def create_webhook_secret(a=Depends(org_admin)):
        """Create (or replace) the org's webhook secret. The answer is the only time it is shown."""
        conn = connection_or_409(a.org_id)
        if not secrets_box.enabled():
            raise HTTPException(503, "Storing the webhook secret needs ARCHFLOW_SECRET_KEY in the API's environment.")
        secret = secrets.token_urlsafe(32)
        store.set_jira_webhook(a.org_id, secrets_box.encrypt(secret, webhook_aad(a.org_id)))
        return {**setup_instructions(a.org_id, secret, conn["project_key"]),
                "settings": jira.settings_payload(store, a.org_id)}

    @app.delete("/api/orgs/{org_id}/jira/webhook", status_code=204)
    def delete_webhook_secret(a=Depends(org_admin)):
        store.set_jira_webhook(a.org_id, "")

    # ---- the webhook itself (Jira calls it; authenticated by the secret only) ----
    @app.post("/api/integrations/jira/webhook/{org_id}")
    async def jira_webhook(org_id: str, request: Request, secret: str = ""):
        body = await request.body()
        if len(body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(413, "Payload too large")
        conn = await asyncio.to_thread(store.get_jira_connection, org_id)
        if not verify_webhook(conn, secret, request.headers.get("x-hub-signature", ""), body):
            raise HTTPException(401, "Invalid webhook secret")
        try:
            payload = json.loads(body or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "Expected a JSON body") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "Expected a JSON object")
        out = await asyncio.to_thread(handle_webhook, store, org_id, payload)
        for pid in out["projects"]:
            await tell(pid, {"type": "jira", "actor": "Jira", "key": out["key"], "event": out["event"]})
            if out["removed"]:
                await tell(pid, {"type": "activity", "actor": "Jira"}, ["main"])
        return {k: out[k] for k in ("event", "key", "ignored", "updated", "removed")} | {"projects": len(out["projects"])}

    # ---- pulling issues in (developer+) ----
    @app.get("/api/projects/{pid}/jira/issues")
    async def jira_unlinked(pid: str, page_token: str | None = None, limit: int = 25, q: str = "",
                            project_key: str | None = None, a=Depends(edit)):
        """Issues of the Jira project that nothing here links to yet, with the requirement each would become."""
        conn = connection_or_409(a.org_id)
        try:
            key = jira.check_project_key(project_key or conn["project_key"])
        except ValueError as exc:
            raise HTTPException(422, "Set a Jira project key first.") from exc

        def run() -> dict[str, Any]:
            try:
                with jira.make_client(conn) as client:
                    return list_unlinked(client, store, pid, key, page_token=page_token, limit=limit, query=q)
            except jira.CONNECTION_ERRORS as exc:
                raise HTTPException(502, f"Could not read Jira: {exc}") from exc

        return await asyncio.to_thread(run)

    @app.post("/api/projects/{pid}/jira/import")
    async def jira_import(pid: str, body: ImportIn, branch: str = "main", a=Depends(edit)):
        """Import the chosen issues as requirements, linked so they are not imported twice."""
        if commit_ops is None:
            raise HTTPException(501, "Importing from Jira is not available on this server.")
        conn = connection_or_409(a.org_id)

        def get_state() -> ProjectState:
            st = store.get_state(pid, branch)
            if st is None:
                raise HTTPException(404, f"branch '{branch}' not found")
            return st

        try:
            out = await import_issues(store, conn, pid, branch, body.keys, ai=body.ai, org_id=a.org_id,
                                      actor=a.actor, commit_ops=commit_ops, get_state=get_state)
        except jira.JiraBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except ImportInputError as exc:
            raise HTTPException(422, str(exc)) from exc
        except jira.CONNECTION_ERRORS as exc:
            raise HTTPException(502, f"Could not read Jira: {exc}") from exc
        if out["imported"]:
            await tell(pid, {"type": "jira", "actor": a.actor})
        return {**out, "links": store.list_external_links(pid)}
