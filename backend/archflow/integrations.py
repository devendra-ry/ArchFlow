"""Integrations: GitHub Issues, Confluence and Slack / Microsoft Teams notifications (Jira lives in jira.py).

Each org connects a service once (admins); the secret (a GitHub token, an Atlassian API token, a webhook URL) is
encrypted with ``secrets_box`` (AES-256-GCM bound to the org and service) and never returned, only its last 4
characters. Settings live in ``org_integrations``.

**GitHub Issues** (developer+ previews and pushes): one issue per active requirement: title "REQ-001: ...", a body
with the description, acceptance criteria as a task list, priority, quality category, the components that
implement it and its source, and labels ``archflow``, ``priority:<p>`` and ``requirement`` / ``nfr:<category>``.
Like the Jira push it is a dry-run preview first, then an explicit push; ``external_links`` (system ``github``)
records each issue with a hash of what was pushed, so a re-push updates changed issues, skips unchanged ones and
never opens a second issue (an issue created but not recorded is found again by the hidden marker
``<!-- archflow:<project>:<requirement> -->`` in its body). A requirement removed in ArchFlow closes its issue.
Status (open / closed) is read back on request. GitHub Enterprise Server works with its ``/api/v3`` URL.

**Confluence** (developer+): publishes the branch's architecture specification (the same document as the
Markdown / PDF export) as one page in a space, optionally under a parent page. Re-publishing updates the same page
(a new Confluence version) and does nothing when the spec has not changed. It can reuse the org's Jira
credentials when Confluence lives on the same Atlassian site.

**Slack / Microsoft Teams** (notifications): an incoming webhook per service receives a short message, with a
link back to the project, when one of the chosen events happens: a version is saved, a review is requested or
answered, a document or meeting is imported, or the project is pushed to Jira / GitHub / Confluence. Messages are
sent in the background and never slow down or fail the action that caused them. Webhook URLs must be Slack's or
Microsoft's (``ARCHFLOW_ALLOW_ANY_WEBHOOK=true`` allows other public https hosts).

Outbound requests only go to https URLs that resolve to public addresses (the Jira guard; private hosts need
``ARCHFLOW_ALLOW_PRIVATE_JIRA_URLS=true``) and never follow redirects. Rate limits (429 / GitHub's secondary limit)
are retried with backoff, honouring ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import exports, jira, secrets_box, users
from .db.types import now
from .models import ProjectState, active_requirements

log = logging.getLogger("archflow.integrations")

KINDS = ("github", "confluence", "slack", "teams")
EVENTS = {"version": "A version is saved", "approval": "A review is requested, approved or sent back",
          "import": "Requirements are imported from a document", "meeting": "A meeting is imported",
          "push": "The project is pushed to Jira, GitHub or Confluence"}
LABEL = {"github": "GitHub", "confluence": "Confluence", "slack": "Slack", "teams": "Microsoft Teams"}
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
SPACE_RE = re.compile(r"^[A-Za-z0-9_~-]{1,255}$")
WEBHOOK_HOSTS = {"slack": ("hooks.slack.com",),
                 "teams": (".webhook.office.com", ".logic.azure.com", ".powerplatform.com", ".powerautomate.com")}
PLAN_VERSION = "1"

# Tests replace these to keep every request in-process.
_transport: httpx.BaseTransport | None = None
_sleep: Callable[[float], None] = time.sleep
_send_inline = False  # tests: send notifications synchronously


class IntegrationError(RuntimeError):
    """A request to an outside service failed (the message is safe to show; it never contains a secret)."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def aad(org_id: str, kind: str) -> str:
    return f"org-integration:{org_id}:{kind}"


def content_hash(source: Any) -> str:
    raw = json.dumps([PLAN_VERSION, source], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def project_url(pid: str, branch: str = "main") -> str:
    return f"{users.frontend_url()}/#/p/{pid}/{branch}"


# --------------------------------------------------------------------------- URL guards
def check_api_url(url: str) -> str:
    """A GitHub API or Atlassian site URL: https, public host (jira.check_site_url). Raises ValueError."""
    return jira.check_site_url(url)


def check_webhook_url(url: str, kind: str) -> str:
    """A Slack or Teams incoming-webhook URL. https; the host must be the service's own unless
    ARCHFLOW_ALLOW_ANY_WEBHOOK is set (then any public https host). Raises ValueError."""
    url = (url or "").strip()
    if len(url) > 2000:
        raise ValueError("The webhook URL is too long.")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("The webhook URL must start with https://.")
    if parts.username or parts.password:
        raise ValueError("Do not put credentials in the webhook URL.")
    host = parts.hostname.lower()
    allowed = WEBHOOK_HOSTS[kind]
    if not any(host == h or (h.startswith(".") and host.endswith(h)) for h in allowed):
        if os.environ.get("ARCHFLOW_ALLOW_ANY_WEBHOOK", "").strip().lower() not in ("1", "true", "yes"):
            want = "https://hooks.slack.com/services/..." if kind == "slack" else \
                "a Teams Workflows (Power Automate) or Office 365 connector URL"
            raise ValueError(f"That is not a {LABEL[kind]} webhook URL (expected {want}).")
        jira.check_site_url(f"https://{host}")  # still public addresses only
    return url


# --------------------------------------------------------------------------- HTTP
def _request(client: httpx.Client, method: str, url: str, *, what: str, attempts: int = 4, **kwargs: Any) -> httpx.Response:
    """One call with retries on 429 / 502-504 (and GitHub's 403 rate limit), honouring Retry-After."""
    idempotent = method in ("GET", "PUT", "PATCH", "DELETE")
    last = ""
    for attempt in range(attempts):
        wait: float | None = None
        try:
            resp = client.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            last = f"network error ({type(exc).__name__})"
            if not idempotent:
                raise IntegrationError(f"{what} did not answer: {last}") from exc
        else:
            limited = resp.status_code == 429 or (resp.status_code == 403 and (
                resp.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in resp.text[:500].lower()))
            if resp.status_code < 300:
                return resp
            if resp.is_redirect:
                raise IntegrationError(f"{what} redirected the request (HTTP {resp.status_code}): check the URL",
                                       resp.status_code)
            if not (limited or (idempotent and resp.status_code in (502, 503, 504)) or resp.status_code == 503):
                hint = {401: "check the token", 403: "the token lacks permission", 404: "not found (check the "
                        "repository, space or page, and that the token can see it)", 422: "rejected"}.get(resp.status_code, "")
                detail = _detail(resp)
                raise IntegrationError(f"{what}: HTTP {resp.status_code}" + (f" ({hint})" if hint else "")
                                       + (f": {detail}" if detail else ""), resp.status_code)
            last = f"HTTP {resp.status_code}"
            wait = jira._retry_after(resp)
            reset = resp.headers.get("x-ratelimit-reset")
            if wait is None and reset and reset.isdigit():
                wait = max(0.0, int(reset) - time.time())
        if attempt + 1 < attempts:
            _sleep(jira.backoff(attempt, wait))
    raise IntegrationError(f"{what} failed after {attempts} attempts ({last})")


def _detail(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200]
    if not isinstance(data, dict):
        return ""
    msg = str(data.get("message") or "")
    errs = data.get("errors")
    if isinstance(errs, list):
        msg += "; " + "; ".join(str(e.get("message") or e.get("title") or e) if isinstance(e, dict) else str(e)
                                for e in errs[:3])
    return msg.strip("; ")[:300]


def _client(base_url: str, headers: dict[str, str], auth: Any = None) -> httpx.Client:
    return httpx.Client(base_url=base_url, headers=headers, auth=auth, transport=_transport, follow_redirects=False,
                        timeout=float(os.environ.get("ARCHFLOW_INTEGRATION_TIMEOUT", 30)))


# --------------------------------------------------------------------------- GitHub
class GitHub:
    def __init__(self, api_url: str, repo: str, token: str):
        self.api_url, self.repo = api_url.rstrip("/"), repo
        self.http = _client(self.api_url, {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                           "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "ArchFlow"})

    def __enter__(self) -> "GitHub":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.http.close()

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = _request(self.http, method, path, what="GitHub", **kwargs)
        return resp.json() if resp.content else None

    def repository(self) -> dict[str, Any]:
        return self.call("GET", f"/repos/{self.repo}") or {}

    def issues(self, label: str = "archflow", pages: int = 10) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, pages + 1):
            batch = self.call("GET", f"/repos/{self.repo}/issues",
                              params={"labels": label, "state": "all", "per_page": 100, "page": page}) or []
            out += [i for i in batch if "pull_request" not in i]
            if len(batch) < 100:
                break
        return out

    def create(self, fields: dict[str, Any]) -> dict[str, Any]:
        return self.call("POST", f"/repos/{self.repo}/issues", json=fields) or {}

    def update(self, number: int, fields: dict[str, Any]) -> dict[str, Any]:
        return self.call("PATCH", f"/repos/{self.repo}/issues/{number}", json=fields) or {}


def github_marker(pid: str, rid: str) -> str:
    return f"<!-- archflow:{pid}:{rid} -->"


def _components(state: ProjectState) -> dict[str, list[str]]:
    by_req: dict[str, list[str]] = {}
    for n in state.nodes.values():
        for rid in n.requirement_ids:
            by_req.setdefault(rid, []).append(n.name + (f" ({n.technology})" if n.technology else ""))
    return by_req


def github_issue(state: ProjectState, pid: str, project_name: str, rid: str, comps: list[str]) -> dict[str, Any]:
    r = state.requirements[rid]
    kind = "non-functional" if r.kind == "non_functional" else "functional"
    lines = [r.description.strip() or r.title, ""]
    if r.acceptance_criteria:
        lines += ["### Acceptance criteria", *[f"- [ ] {c}" for c in r.acceptance_criteria], ""]
    lines += ["### Details", f"- **Priority:** {r.priority}", f"- **Kind:** {kind}"
              + (f" ({r.category})" if r.category else "")]
    if r.actors:
        lines.append(f"- **Actors:** {', '.join(r.actors)}")
    if comps:
        lines.append(f"- **Implemented by:** {', '.join(sorted(comps))}")
    src = r.source or {}
    if src.get("ref"):
        where = ", ".join(x for x in (src.get("section"), f"p. {src['page']}" if src.get("page") else "",
                                      src.get("timestamp")) if x)
        lines.append(f"- **Source:** {src.get('type', '')} `{src['ref']}`" + (f" ({where})" if where else ""))
    lines += ["", f"_Synced from the ArchFlow project **{project_name}** ({rid})._", github_marker(pid, rid)]
    labels = ["archflow", f"priority:{r.priority}",
              f"nfr:{r.category}" if r.kind == "non_functional" and r.category else
              ("nfr" if r.kind == "non_functional" else "requirement")]
    return {"title": f"{rid}: {r.title}"[:250], "body": "\n".join(lines)[:60000], "labels": labels}


def github_plan(state: ProjectState, *, pid: str, project_name: str, links: list[dict[str, Any]],
                include_nfr: bool = True) -> dict[str, Any]:
    """create / update / skip per active requirement, and close for linked requirements that were removed."""
    by_id = {x["object_id"]: x for x in links if x["object_kind"] == "requirement"}
    comps = _components(state)
    items = []
    active = active_requirements(state)
    for rid in sorted(active, key=exports._natural):
        r = active[rid]
        if r.kind == "non_functional" and not include_nfr:
            continue
        issue = github_issue(state, pid, project_name, rid, comps.get(rid, []))
        h = content_hash(issue)
        link = by_id.get(rid)
        # An issue closed on GitHub stays closed (it is usually done); only its text is kept up to date.
        action = "create" if not link else ("skip" if link["content_hash"] == h else "update")
        items.append({"ref": rid, "action": action, "title": issue["title"], "labels": issue["labels"],
                      "body": issue["body"], "hash": h, "external_key": link["external_key"] if link else "",
                      "external_url": link["external_url"] if link else "", "status": link["status"] if link else ""})
    for rid, link in by_id.items():
        if rid not in active and link["status"] != "closed":
            title = state.requirements[rid].title if rid in state.requirements else rid
            items.append({"ref": rid, "action": "close", "title": f"{rid}: {title}", "labels": [], "body": "",
                          "hash": link["content_hash"], "external_key": link["external_key"],
                          "external_url": link["external_url"], "status": link["status"]})
    counts = {a: sum(1 for i in items if i["action"] == a) for a in ("create", "update", "skip", "close")}
    return {"items": items, "counts": counts}


def github_push(gh: GitHub, plan: dict[str, Any], *, store: Any, pid: str) -> dict[str, Any]:
    known: dict[str, dict[str, Any]] | None = None
    results = []
    for item in plan["items"]:
        res = {"ref": item["ref"], "title": item["title"], "action": item["action"], "status": "ok",
               "key": item["external_key"], "url": item["external_url"], "error": ""}
        results.append(res)
        if item["action"] == "skip":
            res["status"] = "skipped"
            continue
        try:
            number = int(item["external_key"].lstrip("#")) if item["external_key"] else 0
            if item["action"] == "close":
                issue = gh.update(number, {"state": "closed", "state_reason": "not_planned"})
                store.save_external_link(pid, "requirement", item["ref"], external_key=f"#{number}",
                                         external_url=issue.get("html_url") or item["external_url"],
                                         content_hash=item["hash"], system="github")
                store.set_external_status(pid, f"#{number}", "closed", "done", system="github")
                continue
            fields = {"title": item["title"], "body": item["body"], "labels": item["labels"]}
            issue = None
            if number:
                try:
                    issue = gh.update(number, fields)
                except IntegrationError as exc:
                    if exc.status not in (404, 410):
                        raise
                    store.delete_external_link(pid, "requirement", item["ref"], system="github")
                    number, res["action"] = 0, "create"  # deleted on GitHub: open it again
            if not number:
                if known is None:  # created earlier but never recorded: adopt it instead of duplicating
                    known = {}
                    for i in gh.issues():
                        if m := re.search(r"<!-- archflow:([^:]+):([^ ]+) -->", i.get("body") or ""):
                            if m.group(1) == pid:
                                known.setdefault(m.group(2), i)
                found = known.get(item["ref"])
                if found:
                    issue, res["action"] = gh.update(int(found["number"]), fields), "update"
                else:
                    issue = gh.create(fields)
            if not issue or not issue.get("number"):
                raise IntegrationError("GitHub did not return the issue number")
            key = f"#{issue['number']}"
            store.save_external_link(pid, "requirement", item["ref"], external_key=key,
                                     external_url=issue.get("html_url", ""), content_hash=item["hash"], system="github")
            store.set_external_status(pid, key, issue.get("state", "open"), "done" if issue.get("state") == "closed"
                                      else "new", system="github")
            res["key"], res["url"] = key, issue.get("html_url", "")
        except IntegrationError as exc:
            res["status"], res["error"] = "error", str(exc)
    counts = {"created": sum(r["status"] == "ok" and r["action"] == "create" for r in results),
              "updated": sum(r["status"] == "ok" and r["action"] == "update" for r in results),
              "closed": sum(r["status"] == "ok" and r["action"] == "close" for r in results),
              "unchanged": sum(r["status"] == "skipped" for r in results),
              "failed": sum(r["status"] == "error" for r in results)}
    return {"repo": gh.repo, "results": results, "counts": counts}


def github_refresh(gh: GitHub, store: Any, pid: str) -> int:
    links = {x["external_key"]: x for x in store.list_external_links(pid, "github")}
    n = 0
    for issue in gh.issues():
        key = f"#{issue.get('number')}"
        if key in links:
            state = issue.get("state", "")
            store.set_external_status(pid, key, state, "done" if state == "closed" else "new", system="github")
            n += 1
    return n


# --------------------------------------------------------------------------- Confluence
class Confluence:
    def __init__(self, site_url: str, email: str, token: str):
        self.site = site_url.rstrip("/")
        self.http = _client(self.site, {"Accept": "application/json", "User-Agent": "ArchFlow"}, auth=(email, token))

    def __enter__(self) -> "Confluence":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.http.close()

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        resp = _request(self.http, method, path, what="Confluence", **kwargs)
        return resp.json() if resp.content else None

    def space(self, key: str) -> dict[str, Any]:
        data = self.call("GET", "/wiki/api/v2/spaces", params={"keys": key, "limit": 1}) or {}
        found = (data.get("results") or [])
        if not found:
            raise IntegrationError(f"Confluence space {key} not found (or the account cannot see it)", 404)
        return found[0]

    def page(self, page_id: str) -> dict[str, Any]:
        return self.call("GET", f"/wiki/api/v2/pages/{page_id}") or {}

    def find(self, space_id: str, title: str) -> dict[str, Any] | None:
        data = self.call("GET", "/wiki/api/v2/pages", params={"space-id": space_id, "title": title, "limit": 1}) or {}
        found = data.get("results") or []
        return found[0] if found else None

    def create(self, space_id: str, title: str, body: str, parent_id: str = "") -> dict[str, Any]:
        payload: dict[str, Any] = {"spaceId": space_id, "status": "current", "title": title,
                                   "body": {"representation": "storage", "value": body}}
        if parent_id:
            payload["parentId"] = parent_id
        return self.call("POST", "/wiki/api/v2/pages", json=payload) or {}

    def update(self, page_id: str, title: str, body: str, version: int) -> dict[str, Any]:
        return self.call("PUT", f"/wiki/api/v2/pages/{page_id}", json={
            "id": page_id, "status": "current", "title": title,
            "body": {"representation": "storage", "value": body},
            "version": {"number": version, "message": "Updated from ArchFlow"}}) or {}

    def web_url(self, page: dict[str, Any]) -> str:
        link = ((page.get("_links") or {}).get("webui") or "")
        return f"{self.site}/wiki{link}" if link.startswith("/") else link


def storage_body(html: str) -> str:
    """Confluence storage format from the export HTML: XHTML (void tags closed), no scripts or styles."""
    html = re.sub(r"<(script|style)\b.*?</\1>", "", html, flags=re.S | re.I)
    html = re.sub(r"<(br|hr)\s*>", r"<\1/>", html, flags=re.I)
    return html


def confluence_document(b: exports.Bundle, branch: str) -> tuple[str, str]:
    title = f"{b.name}: architecture specification" + (f" ({branch})" if branch != "main" else "")
    body = storage_body(exports.markdown_to_html(exports.spec_markdown(b)))
    body = (f"<p><em>Published from ArchFlow on {now()[:10]}. Edit the architecture in ArchFlow and publish again; "
            f"changes made here are replaced.</em></p>\n{body}")
    return title[:255], body


# --------------------------------------------------------------------------- notifications
def slack_payload(title: str, text: str, url: str) -> dict[str, Any]:
    return {"text": f"{title}: {text}",
            "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{_mrkdwn(title)}*\n{_mrkdwn(text)}"}},
                       {"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{url}|Open in ArchFlow>"}]}]}


def _mrkdwn(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")[:2900]


def teams_payload(title: str, text: str, url: str) -> dict[str, Any]:
    """An Adaptive Card message (Teams Workflows webhooks; Office 365 connectors accept it too)."""
    return {"type": "message", "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None,
        "content": {"$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [{"type": "TextBlock", "text": title[:300], "weight": "Bolder", "wrap": True},
                             {"type": "TextBlock", "text": text[:2000], "wrap": True}],
                    "actions": [{"type": "Action.OpenUrl", "title": "Open in ArchFlow", "url": url}]}}]}


def send_webhook(kind: str, url: str, title: str, text: str, link: str) -> None:
    payload = slack_payload(title, text, link) if kind == "slack" else teams_payload(title, text, link)
    with _client("https://webhook.invalid", {"User-Agent": "ArchFlow"}) as http:
        _request(http, "POST", url, what=LABEL[kind], attempts=2, json=payload)


def notify(store: Any, org_id: str | None, pid: str, event: str, title: str, text: str, *, branch: str = "main") -> None:
    """Tell the org's Slack / Teams channels about an event (when they chose it). Never raises; sends in the
    background."""
    if not org_id or event not in EVENTS:
        return
    try:
        targets = [c for c in store.list_integrations(org_id) if c["kind"] in ("slack", "teams")
                   and event in (c["config"].get("events") or []) and c["ciphertext"]]
    except Exception:  # noqa: BLE001 - notifications must never break the action
        log.exception("could not read the notification settings of org %s", org_id)
        return
    if not targets:
        return
    project = (store.get_project(pid) or {}).get("name") or pid
    link = project_url(pid, branch)

    def run() -> None:
        for c in targets:
            try:
                url = secrets_box.decrypt(c["ciphertext"], aad(org_id, c["kind"]))
                send_webhook(c["kind"], url, f"{project}: {title}", text, link)
                status = {"last_sent_at": now(), "last_error": ""}
            except Exception as exc:  # noqa: BLE001 - recorded for the settings screen
                log.warning("%s notification for org %s failed: %s", c["kind"], org_id, type(exc).__name__)
                status = {"last_error": str(exc)[:300] if isinstance(exc, IntegrationError) else type(exc).__name__,
                          "last_error_at": now()}
            try:
                current = store.get_integration(org_id, c["kind"])
                if current:
                    store.set_integration(org_id, c["kind"], config={**current["config"], **status},
                                          ciphertext=current["ciphertext"], last4=current["last4"],
                                          user_id=current["updated_by"])
            except Exception:  # noqa: BLE001
                log.exception("could not record the notification status")

    if _send_inline:
        run()
    else:
        threading.Thread(target=run, name="archflow-notify", daemon=True).start()


# --------------------------------------------------------------------------- settings
def masked(c: dict[str, Any] | None, kind: str) -> dict[str, Any]:
    cfg = dict(c["config"]) if c else {}
    return {"kind": kind, "label": LABEL[kind], "configured": bool(c), "config": cfg,
            "has_secret": bool(c and c["ciphertext"]), "last4": c["last4"] if c else None,
            "updated_at": c["updated_at"] if c else None}


def settings_payload(store: Any, org_id: str) -> dict[str, Any]:
    by_kind = {c["kind"]: c for c in store.list_integrations(org_id)}
    jc = store.get_jira_connection(org_id)
    return {"encryption": secrets_box.enabled(), "events": EVENTS,
            "jira": {"configured": bool(jc), "site_url": jc["site_url"] if jc else "", "email": jc["email"] if jc else ""},
            "integrations": {k: masked(by_kind.get(k), k) for k in KINDS}}


def clean_config(kind: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Validate the non-secret settings of one service. Raises ValueError."""
    if kind == "github":
        repo = str(cfg.get("repo") or "").strip().removeprefix("https://github.com/").strip("/")
        if not REPO_RE.match(repo):
            raise ValueError("Enter the repository as owner/name, e.g. acme/shop.")
        api = str(cfg.get("api_url") or "").strip() or "https://api.github.com"
        return {"repo": repo, "api_url": check_api_url(api), "include_nfr": bool(cfg.get("include_nfr", True))}
    if kind == "confluence":
        space = str(cfg.get("space_key") or "").strip()
        if not SPACE_RE.match(space):
            raise ValueError("Enter the key of the Confluence space, e.g. ARCH.")
        parent = str(cfg.get("parent_id") or "").strip()
        if parent and not parent.isdigit():
            raise ValueError("The parent page id is the number in the page's URL.")
        use_jira = bool(cfg.get("use_jira_credentials"))
        out = {"space_key": space, "parent_id": parent, "use_jira_credentials": use_jira}
        if not use_jira:
            email = users.normalize_email(str(cfg.get("email") or ""))
            if not users.valid_email(email):
                raise ValueError("Enter the email of the Atlassian account that owns the API token.")
            out.update(site_url=check_api_url(str(cfg.get("site_url") or "")), email=email)
        return out
    events = [e for e in (cfg.get("events") or []) if e in EVENTS]
    if not events:
        raise ValueError("Choose at least one event to be notified about.")
    return {"events": events, "channel": str(cfg.get("channel") or "").strip()[:80]}


def secret_of(store: Any, org_id: str, kind: str) -> tuple[dict[str, Any], str]:
    """(config, decrypted secret) of a configured service; raises HTTPException 409 when it is not set up."""
    c = store.get_integration(org_id, kind)
    if not c or not c["ciphertext"] and not c["config"].get("use_jira_credentials"):
        raise HTTPException(409, f"{LABEL[kind]} is not connected for this organization: an admin connects it in "
                                 "the organization's integration settings.")
    if kind == "confluence" and c["config"].get("use_jira_credentials"):
        jc = store.get_jira_connection(org_id)
        if not jc:
            raise HTTPException(409, "Confluence is set to use the Jira connection, but Jira is not connected.")
        token = secrets_box.decrypt(jc["ciphertext"], jira.token_aad(org_id))
        return {**c["config"], "site_url": jc["site_url"], "email": jc["email"]}, token
    try:
        return c["config"], secrets_box.decrypt(c["ciphertext"], aad(org_id, kind))
    except (secrets_box.SecretDecryptError, secrets_box.SecretsUnavailable) as exc:
        raise HTTPException(503, f"The stored {LABEL[kind]} secret cannot be read: {exc}") from exc


# --------------------------------------------------------------------------- routes
class IntegrationIn(BaseModel):
    config: dict[str, Any] = {}
    secret: str | None = Field(None, max_length=2000)  # None or "": keep the stored secret


class GitHubIn(BaseModel):
    include_nfr: bool | None = None


class GitHubPushIn(GitHubIn):
    confirm: bool = False


class PublishIn(BaseModel):
    dry_run: bool = True


def register_routes(app: FastAPI, store: Any, *, authz: Any, state_or_404: Callable[[str, str], ProjectState],
                    broadcast: Callable[[str, str, dict[str, Any]], Any] | None = None) -> None:
    read, edit, org_admin = authz.project("read"), authz.project("edit"), authz.org("admin")
    locks: dict[str, threading.Lock] = {}
    guard = threading.Lock()

    def lock_for(key: str) -> threading.Lock:
        with guard:
            return locks.setdefault(key, threading.Lock())

    def log_line(pid: str, branch: str, actor: str, line: str) -> None:
        state = store.get_state(pid, branch)
        store.log_activity(pid, branch, actor, [line], state.rev if state else 0)

    def kind_or_404(kind: str) -> str:
        if kind not in KINDS:
            raise HTTPException(404, "unknown integration")
        return kind

    # ---- org settings (admin+) ----
    @app.get("/api/orgs/{org_id}/integrations")
    def get_integrations(a=Depends(org_admin)):
        return settings_payload(store, a.org_id)

    @app.put("/api/orgs/{org_id}/integrations/{kind}")
    def put_integration(kind: str, body: IntegrationIn, a=Depends(org_admin)):
        kind = kind_or_404(kind)
        if not secrets_box.enabled():
            raise HTTPException(503, "Storing integration secrets needs ARCHFLOW_SECRET_KEY (a long random string) "
                                     "in the API's environment. Set it and restart the API.")
        try:
            cfg = clean_config(kind, body.config)
            secret = (body.secret or "").strip()
            if secret and kind in ("slack", "teams"):
                secret = check_webhook_url(secret, kind)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        current = store.get_integration(a.org_id, kind)
        if current:  # keep the delivery status of notifications
            cfg = {**{k: v for k, v in current["config"].items() if k.startswith("last_")}, **cfg}
        if secret:
            if kind in ("github", "confluence") and len(secret) < 8:
                raise HTTPException(422, "The token looks too short.")
            ciphertext, last4 = secrets_box.encrypt(secret, aad(a.org_id, kind)), secret[-4:]
        elif current and current["ciphertext"]:
            ciphertext, last4 = current["ciphertext"], current["last4"]
        elif kind == "confluence" and cfg.get("use_jira_credentials"):
            if not store.get_jira_connection(a.org_id):
                raise HTTPException(422, "Connect Jira first, or enter Confluence credentials.")
            ciphertext, last4 = "", ""
        else:
            what = {"github": "a token (a fine-grained token with Issues: read and write on the repository)",
                    "confluence": "an Atlassian API token", "slack": "the webhook URL",
                    "teams": "the webhook URL"}[kind]
            raise HTTPException(422, f"Enter {what}.")
        store.set_integration(a.org_id, kind, config=cfg, ciphertext=ciphertext, last4=last4, user_id=a.user["id"])
        return settings_payload(store, a.org_id)

    @app.delete("/api/orgs/{org_id}/integrations/{kind}", status_code=204)
    def delete_integration(kind: str, a=Depends(org_admin)):
        store.delete_integration(a.org_id, kind_or_404(kind))

    @app.post("/api/orgs/{org_id}/integrations/{kind}/test")
    async def test_integration(kind: str, a=Depends(org_admin)):
        kind = kind_or_404(kind)
        cfg, secret = secret_of(store, a.org_id, kind)

        def run() -> dict[str, Any]:
            try:
                if kind == "github":
                    with GitHub(cfg["api_url"], cfg["repo"], secret) as gh:
                        repo = gh.repository()
                        perms = repo.get("permissions") or {}
                        return {"ok": True, "detail": f"{repo.get('full_name', cfg['repo'])}"
                                + (" (no write access: pushes will fail)" if perms and not perms.get("push") else "")}
                if kind == "confluence":
                    with Confluence(check_api_url(cfg["site_url"]), cfg["email"], secret) as cf:
                        sp = cf.space(cfg["space_key"])
                        return {"ok": True, "detail": f"Space {sp.get('key')}: {sp.get('name', '')}"}
                send_webhook(kind, secret, "ArchFlow test message",
                             f"{a.actor} connected this channel to ArchFlow notifications.", users.frontend_url())
                return {"ok": True, "detail": "Test message sent."}
            except (IntegrationError, ValueError) as exc:
                return {"ok": False, "error": str(exc)}

        return await asyncio.to_thread(run)

    # ---- GitHub (per project) ----
    def gh_plan(pid: str, branch: str, include_nfr: bool | None, cfg: dict[str, Any]) -> dict[str, Any]:
        state = state_or_404(pid, branch)
        project = store.get_project(pid) or {}
        nfr = cfg.get("include_nfr", True) if include_nfr is None else include_nfr
        plan = github_plan(state, pid=pid, project_name=project.get("name") or pid,
                           links=store.list_external_links(pid, "github"), include_nfr=nfr)
        return {**plan, "repo": cfg["repo"], "include_nfr": nfr}

    @app.get("/api/projects/{pid}/github/links")
    def github_links(pid: str, a=Depends(read)):
        c = store.get_integration(a.org_id, "github")
        return {"connected": bool(c and c["ciphertext"]), "repo": c["config"].get("repo", "") if c else "",
                "links": store.list_external_links(pid, "github")}

    @app.post("/api/projects/{pid}/github/plan")
    def github_plan_route(pid: str, body: GitHubIn, branch: str = "main", a=Depends(edit)):
        cfg, _ = secret_of(store, a.org_id, "github")
        return {**gh_plan(pid, branch, body.include_nfr, cfg), "dry_run": True}

    @app.post("/api/projects/{pid}/github/push")
    async def github_push_route(pid: str, body: GitHubPushIn, branch: str = "main", a=Depends(edit)):
        if not body.confirm:
            raise HTTPException(422, "Preview the plan first, then push with confirm: true.")
        cfg, token = secret_of(store, a.org_id, "github")

        def run() -> dict[str, Any]:
            lock = lock_for(f"github:{pid}")
            if not lock.acquire(blocking=False):
                raise HTTPException(409, "A GitHub push for this project is already running.")
            try:
                plan = gh_plan(pid, branch, body.include_nfr, cfg)
                try:
                    check_api_url(cfg["api_url"])
                    with GitHub(cfg["api_url"], cfg["repo"], token) as gh:
                        return github_push(gh, plan, store=store, pid=pid)
                except (IntegrationError, ValueError) as exc:
                    raise HTTPException(502, f"GitHub push failed: {exc}") from exc
            finally:
                lock.release()

        out = await asyncio.to_thread(run)
        c = out["counts"]
        line = (f"pushed to GitHub {out['repo']}: {c['created']} created, {c['updated']} updated, {c['closed']} "
                f"closed, {c['unchanged']} unchanged, {c['failed']} failed")
        log_line(pid, branch, a.actor, line)
        notify(store, a.org_id, pid, "push", f"{a.actor} pushed requirements to GitHub", line, branch=branch)
        if broadcast:
            await broadcast(pid, branch, {"type": "github", "actor": a.actor})
            await broadcast(pid, branch, {"type": "activity", "actor": a.actor})
        return out

    @app.post("/api/projects/{pid}/github/refresh")
    async def github_refresh_route(pid: str, branch: str = "main", a=Depends(edit)):
        cfg, token = secret_of(store, a.org_id, "github")

        def run() -> int:
            try:
                with GitHub(check_api_url(cfg["api_url"]), cfg["repo"], token) as gh:
                    return github_refresh(gh, store, pid)
            except (IntegrationError, ValueError) as exc:
                raise HTTPException(502, f"Could not read GitHub: {exc}") from exc

        n = await asyncio.to_thread(run)
        return {"updated": n, "links": store.list_external_links(pid, "github")}

    # ---- Confluence (per project) ----
    @app.get("/api/projects/{pid}/confluence/links")
    def confluence_links(pid: str, a=Depends(read)):
        c = store.get_integration(a.org_id, "confluence")
        return {"connected": bool(c), "space_key": c["config"].get("space_key", "") if c else "",
                "links": store.list_external_links(pid, "confluence")}

    @app.post("/api/projects/{pid}/confluence/publish")
    async def confluence_publish(pid: str, body: PublishIn, branch: str = "main", a=Depends(edit)):
        cfg, token = secret_of(store, a.org_id, "confluence")
        state = state_or_404(pid, branch)

        def run() -> dict[str, Any]:
            title, html = confluence_document(exports.collect(store, pid, branch, state), branch)
            # The date line changes every day: hash the spec itself, so an unchanged spec is not re-published.
            h = content_hash([title, html.split("\n", 1)[1]])
            link = next((x for x in store.list_external_links(pid, "confluence")
                         if x["object_kind"] == "spec" and x["object_id"] == branch), None)
            action = "create" if not link else ("skip" if link["content_hash"] == h else "update")
            out = {"title": title, "space_key": cfg["space_key"], "action": action, "chars": len(html),
                   "url": link["external_url"] if link else "", "page_id": link["external_key"] if link else ""}
            if body.dry_run or action == "skip":
                return {**out, "dry_run": body.dry_run, "preview": html[:20000]}
            lock = lock_for(f"confluence:{pid}:{branch}")
            if not lock.acquire(blocking=False):
                raise HTTPException(409, "Publishing to Confluence is already running for this branch.")
            try:
                with Confluence(check_api_url(cfg["site_url"]), cfg["email"], token) as cf:
                    page: dict[str, Any] | None = None
                    if link:
                        try:
                            current = cf.page(link["external_key"])
                            number = int(((current.get("version") or {}).get("number") or 1)) + 1
                            page = cf.update(link["external_key"], title, html, number)
                        except IntegrationError as exc:
                            if exc.status != 404:
                                raise
                            store.delete_external_link(pid, "spec", branch, system="confluence")
                            out["action"] = "create"
                    if page is None:
                        space = cf.space(cfg["space_key"])
                        existing = cf.find(str(space["id"]), title)
                        if existing:  # a page with this title already exists in the space: update it
                            current = cf.page(str(existing["id"]))
                            number = int(((current.get("version") or {}).get("number") or 1)) + 1
                            page, out["action"] = cf.update(str(existing["id"]), title, html, number), "update"
                        else:
                            page = cf.create(str(space["id"]), title, html, cfg.get("parent_id") or "")
                    url = cf.web_url(page)
                    store.save_external_link(pid, "spec", branch, external_key=str(page.get("id") or ""),
                                             external_url=url, content_hash=h, system="confluence")
                    return {**out, "dry_run": False, "url": url, "page_id": str(page.get("id") or ""),
                            "version": (page.get("version") or {}).get("number")}
            except (IntegrationError, ValueError) as exc:
                raise HTTPException(502, f"Publishing to Confluence failed: {exc}") from exc
            finally:
                lock.release()

        out = await asyncio.to_thread(run)
        if not out["dry_run"] and out["action"] != "skip":
            line = f"published the spec to Confluence ({out['space_key']}): {out['action']}d “{out['title']}”"
            log_line(pid, branch, a.actor, line)
            notify(store, a.org_id, pid, "push", f"{a.actor} published the spec to Confluence",
                   f"{out['title']}: {out['url']}", branch=branch)
            if broadcast:
                await broadcast(pid, branch, {"type": "activity", "actor": a.actor})
        return out
