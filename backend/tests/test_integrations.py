"""Integrations: GitHub Issues push (preview, idempotent re-push, close, adopt by marker, rate limits), Confluence
publishing (create, skip unchanged, update with a new version, reuse of the Jira credentials), Slack / Teams
notifications on events, URL guards, masked secrets and roles. Every service is a fake behind httpx.MockTransport."""

import json
import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import httpx  # noqa: E402
import pytest  # noqa: E402
from authhelp import member_client, new_client, signup  # noqa: E402

from archflow import integrations, jira  # noqa: E402
from archflow.agents import tracing  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.store import Store  # noqa: E402

from test_mcp_remote import BASE  # noqa: E402

TEST_SECRET = "test-only-secret-key-0123456789abcdef"
ORG = "org-default"
GH_TOKEN = "github_pat_test_TOKEN1234"
SLACK = "https://hooks.slack.com/services/T000/B000/XXXXSECRET"


class Fakes:
    """GitHub issues, Confluence pages and webhook deliveries, in memory."""

    def __init__(self):
        self.issues: dict[int, dict] = {}
        self.pages: dict[str, dict] = {}
        self.webhooks: list[tuple[str, dict]] = []
        self.calls: list[tuple[str, str]] = []
        self.rate_limit = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, url, path = request.method, str(request.url), request.url.path
        self.calls.append((method, path))
        body = json.loads(request.content) if request.content else {}
        if request.url.host == "hooks.slack.com" or "webhook.office.com" in request.url.host:
            self.webhooks.append((url, body))
            return httpx.Response(200, text="ok")
        if request.url.host == "api.github.com":
            assert request.headers["authorization"] == f"Bearer {GH_TOKEN}"
            if self.rate_limit:
                self.rate_limit -= 1
                return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "retry-after": "1"},
                                      json={"message": "API rate limit exceeded"})
            if path == "/repos/acme/shop":
                return httpx.Response(200, json={"full_name": "acme/shop", "permissions": {"push": True}})
            if path == "/repos/acme/shop/issues" and method == "GET":
                return httpx.Response(200, json=list(self.issues.values()))
            if path == "/repos/acme/shop/issues" and method == "POST":
                n = len(self.issues) + 1
                self.issues[n] = {"number": n, "state": "open", "html_url": f"https://github.com/acme/shop/issues/{n}",
                                  **body}
                return httpx.Response(201, json=self.issues[n])
            if path.startswith("/repos/acme/shop/issues/") and method == "PATCH":
                n = int(path.rsplit("/", 1)[1])
                if n not in self.issues:
                    return httpx.Response(404, json={"message": "Not Found"})
                self.issues[n].update(body)
                return httpx.Response(200, json=self.issues[n])
        if request.url.host == "acme.atlassian.net":
            assert request.headers["authorization"].startswith("Basic ")
            if path == "/wiki/api/v2/spaces":
                return httpx.Response(200, json={"results": [{"id": 77, "key": "ARCH", "name": "Architecture"}]})
            if path == "/wiki/api/v2/pages" and method == "GET":
                found = [p for p in self.pages.values() if p["title"] == request.url.params.get("title")]
                return httpx.Response(200, json={"results": found})
            if path == "/wiki/api/v2/pages" and method == "POST":
                pid = str(100 + len(self.pages))
                self.pages[pid] = {"id": pid, "title": body["title"], "version": {"number": 1}, "body": body["body"],
                                   "parentId": body.get("parentId"), "_links": {"webui": f"/spaces/ARCH/pages/{pid}"}}
                return httpx.Response(200, json=self.pages[pid])
            if path.startswith("/wiki/api/v2/pages/"):
                pid = path.rsplit("/", 1)[1]
                if pid not in self.pages:
                    return httpx.Response(404, json={"message": "not found"})
                if method == "PUT":
                    assert body["version"]["number"] == self.pages[pid]["version"]["number"] + 1
                    self.pages[pid].update(title=body["title"], body=body["body"], version=body["version"])
                return httpx.Response(200, json=self.pages[pid])
        return httpx.Response(404, json={"message": f"unexpected {method} {url}"})


@pytest.fixture()
def fakes(monkeypatch):
    f = Fakes()
    monkeypatch.setenv("ARCHFLOW_SECRET_KEY", TEST_SECRET)
    monkeypatch.setattr(integrations, "_transport", httpx.MockTransport(f.handler))
    monkeypatch.setattr(jira, "_transport", httpx.MockTransport(f.handler))
    monkeypatch.setattr(jira, "_resolve", lambda host: ["20.0.0.1"])  # public, without DNS
    monkeypatch.setattr(integrations, "_sleep", lambda s: None)
    monkeypatch.setattr(integrations, "_send_inline", True)
    return f


@pytest.fixture()
def app():
    yield create_app(Store(":memory:"))
    tracing.set_sink(None)


@pytest.fixture()
def client(app):
    with new_client(app, base_url=BASE) as c:
        signup(c)
        yield c


def project(client) -> str:
    pid = client.post("/api/projects", json={"name": "Shop", "template": "blank"}).json()["id"]
    client.post(f"/api/projects/{pid}/ops", json={"ops": [
        {"op": "add_requirement", "requirement": {"id": "REQ-001", "title": "Checkout by card", "priority": "high",
                                                  "description": "Customers pay by card.",
                                                  "acceptance_criteria": ["A paid order is marked paid"]}},
        {"op": "add_requirement", "requirement": {"id": "REQ-002", "title": "Fast checkout", "kind": "non_functional",
                                                  "category": "performance", "description": "Under 300 ms at p95."}},
        {"op": "add_node", "node": {"id": "co", "name": "Checkout Service", "type": "service", "technology": "Go",
                                    "requirement_ids": ["REQ-001"]}}]})
    return pid


def connect_github(client, **cfg):
    r = client.put(f"/api/orgs/{ORG}/integrations/github",
                   json={"config": {"repo": "https://github.com/acme/shop", **cfg}, "secret": GH_TOKEN})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- settings
def test_settings_are_masked_validated_and_admin_only(app, client, fakes):
    out = connect_github(client)
    gh = out["integrations"]["github"]
    assert gh["config"]["repo"] == "acme/shop" and gh["last4"] == "1234" and GH_TOKEN not in json.dumps(out)
    assert client.post(f"/api/orgs/{ORG}/integrations/github/test").json() == {"ok": True, "detail": "acme/shop"}
    bad = client.put(f"/api/orgs/{ORG}/integrations/github", json={"config": {"repo": "not a repo"}})
    assert bad.status_code == 422
    r = client.put(f"/api/orgs/{ORG}/integrations/slack", json={"config": {"events": ["version"]},
                                                               "secret": "https://evil.example.com/hook"})
    assert r.status_code == 422 and "Slack webhook" in r.json()["detail"]
    r = client.put(f"/api/orgs/{ORG}/integrations/slack", json={"config": {"events": []}, "secret": SLACK})
    assert r.status_code == 422
    dev = member_client(app, client, "developer", "dev@example.com")
    assert dev.get(f"/api/orgs/{ORG}/integrations").status_code == 403
    assert client.get("/api/projects/x/github/links").status_code in (403, 404)


def test_needs_the_secret_key(client, monkeypatch):
    monkeypatch.delenv("ARCHFLOW_SECRET_KEY", raising=False)
    r = client.put(f"/api/orgs/{ORG}/integrations/github", json={"config": {"repo": "acme/shop"}, "secret": GH_TOKEN})
    assert r.status_code == 503


# ---------------------------------------------------------------- GitHub
def test_github_preview_push_repush_update_and_close(client, fakes):
    connect_github(client)
    pid = project(client)
    plan = client.post(f"/api/projects/{pid}/github/plan", json={}).json()
    assert plan["dry_run"] and plan["counts"] == {"create": 2, "update": 0, "skip": 0, "close": 0}
    first = plan["items"][0]
    assert first["title"] == "REQ-001: Checkout by card" and "- [ ] A paid order is marked paid" in first["body"]
    assert "Checkout Service (Go)" in first["body"] and f"<!-- archflow:{pid}:REQ-001 -->" in first["body"]
    assert set(plan["items"][1]["labels"]) == {"archflow", "priority:medium", "nfr:performance"}
    assert fakes.issues == {}  # a preview sends nothing

    assert client.post(f"/api/projects/{pid}/github/push", json={}).status_code == 422  # needs confirm
    fakes.rate_limit = 1  # the first write is rate limited and retried
    out = client.post(f"/api/projects/{pid}/github/push", json={"confirm": True}).json()
    assert out["counts"]["created"] == 2 and len(fakes.issues) == 2
    links = client.get(f"/api/projects/{pid}/github/links").json()["links"]
    assert {x["external_key"] for x in links} == {"#1", "#2"} and links[0]["status"] == "open"

    again = client.post(f"/api/projects/{pid}/github/push", json={"confirm": True}).json()
    assert again["counts"]["unchanged"] == 2 and len(fakes.issues) == 2

    client.post(f"/api/projects/{pid}/ops", json={"ops": [
        {"op": "update_requirement", "id": "REQ-001", "changes": {"title": "Checkout by card or PayPal"}},
        {"op": "remove_requirement", "id": "REQ-002"}]})
    plan = client.post(f"/api/projects/{pid}/github/plan", json={}).json()
    assert plan["counts"] == {"create": 0, "update": 1, "skip": 0, "close": 1}
    out = client.post(f"/api/projects/{pid}/github/push", json={"confirm": True}).json()
    assert out["counts"]["updated"] == 1 and out["counts"]["closed"] == 1
    assert fakes.issues[1]["title"] == "REQ-001: Checkout by card or PayPal" and fakes.issues[2]["state"] == "closed"
    acts = " ".join(" ".join(x["summary"]) for x in client.get(f"/api/projects/{pid}/activity").json())
    assert "pushed to GitHub acme/shop" in acts


def test_github_adopts_an_unrecorded_issue_and_recreates_a_deleted_one(app, client, fakes):
    connect_github(client, include_nfr=False)
    pid = project(client)
    fakes.issues[1] = {"number": 1, "state": "open", "html_url": "https://github.com/acme/shop/issues/1",
                       "title": "old", "body": f"x\n<!-- archflow:{pid}:REQ-001 -->", "labels": ["archflow"]}
    out = client.post(f"/api/projects/{pid}/github/push", json={"confirm": True}).json()
    assert out["counts"] == {"created": 0, "updated": 1, "closed": 0, "unchanged": 0, "failed": 0}  # NFR left out
    assert len(fakes.issues) == 1 and fakes.issues[1]["title"].startswith("REQ-001")
    del fakes.issues[1]
    client.post(f"/api/projects/{pid}/ops", json={"ops": [
        {"op": "update_requirement", "id": "REQ-001", "changes": {"description": "Card or wallet."}}]})
    out = client.post(f"/api/projects/{pid}/github/push", json={"confirm": True}).json()
    assert out["counts"]["created"] == 1 and len(fakes.issues) == 1
    fakes.issues[1]["state"] = "closed"
    assert client.post(f"/api/projects/{pid}/github/refresh").json()["links"][0]["status"] == "closed"


# ---------------------------------------------------------------- Confluence
def test_confluence_publish_skip_and_update(client, fakes):
    r = client.put(f"/api/orgs/{ORG}/integrations/confluence", json={"config": {
        "site_url": "https://acme.atlassian.net", "email": "bot@acme.com", "space_key": "ARCH", "parent_id": "42"},
        "secret": "atlassian-token-ABCD"})
    assert r.status_code == 200, r.text
    pid = project(client)
    dry = client.post(f"/api/projects/{pid}/confluence/publish", json={}).json()
    assert dry["dry_run"] and dry["action"] == "create" and fakes.pages == {}
    assert dry["title"] == "Shop: architecture specification" and "Checkout by card" in dry["preview"]
    out = client.post(f"/api/projects/{pid}/confluence/publish", json={"dry_run": False}).json()
    page = fakes.pages[out["page_id"]]
    assert out["action"] == "create" and page["parentId"] == "42" and "<br>" not in page["body"]["value"]
    assert out["url"] == f"https://acme.atlassian.net/wiki/spaces/ARCH/pages/{out['page_id']}"
    same = client.post(f"/api/projects/{pid}/confluence/publish", json={"dry_run": False}).json()
    assert same["action"] == "skip" and page["version"]["number"] == 1
    client.post(f"/api/projects/{pid}/ops", json={"ops": [
        {"op": "update_requirement", "id": "REQ-001", "changes": {"title": "Checkout by wallet"}}]})
    upd = client.post(f"/api/projects/{pid}/confluence/publish", json={"dry_run": False}).json()
    assert upd["action"] == "update" and page["version"]["number"] == 2 and "Checkout by wallet" in page["body"]["value"]


def test_confluence_can_reuse_the_jira_credentials(client, fakes):
    r = client.put(f"/api/orgs/{ORG}/jira", json={"site_url": "https://acme.atlassian.net", "email": "bot@acme.com",
                                                  "api_token": "jira-token-WXYZ", "project_key": "SHOP"})
    assert r.status_code == 200, r.text
    r = client.put(f"/api/orgs/{ORG}/integrations/confluence",
                   json={"config": {"space_key": "ARCH", "use_jira_credentials": True}})
    assert r.status_code == 200, r.text
    assert client.post(f"/api/orgs/{ORG}/integrations/confluence/test").json()["ok"] is True
    pid = project(client)
    assert client.post(f"/api/projects/{pid}/confluence/publish", json={"dry_run": False}).json()["action"] == "create"


# ---------------------------------------------------------------- notifications
def test_slack_and_teams_are_told_about_chosen_events(client, fakes):
    teams = "https://acme.webhook.office.com/webhookb2/abc/IncomingWebhook/def"
    assert client.put(f"/api/orgs/{ORG}/integrations/slack",
                      json={"config": {"events": ["version", "push"], "channel": "#arch"}, "secret": SLACK}).status_code == 200
    assert client.put(f"/api/orgs/{ORG}/integrations/teams",
                      json={"config": {"events": ["push"]}, "secret": teams}).status_code == 200
    pid = project(client)
    client.post(f"/api/projects/{pid}/commit", json={"message": "First cut"})
    assert len(fakes.webhooks) == 1
    url, body = fakes.webhooks[0]
    assert url == SLACK and "saved v1 on main" in body["text"] and f"/#/p/{pid}/main" in json.dumps(body)

    connect_github(client)
    client.post(f"/api/projects/{pid}/github/push", json={"confirm": True})
    kinds = [u for u, _ in fakes.webhooks[1:]]
    assert kinds == [SLACK, teams]
    card = fakes.webhooks[-1][1]["attachments"][0]
    assert card["contentType"] == "application/vnd.microsoft.card.adaptive"
    settings = client.get(f"/api/orgs/{ORG}/integrations").json()["integrations"]["slack"]
    assert settings["config"]["last_sent_at"] and SLACK not in json.dumps(settings) and settings["has_secret"]
    assert client.post(f"/api/orgs/{ORG}/integrations/teams/test").json()["ok"] is True


def test_a_failing_webhook_never_breaks_the_action(client, fakes, monkeypatch):
    client.put(f"/api/orgs/{ORG}/integrations/slack", json={"config": {"events": ["version"]}, "secret": SLACK})
    monkeypatch.setattr(integrations, "send_webhook", lambda *a, **k: (_ for _ in ()).throw(
        integrations.IntegrationError("Slack: HTTP 404")))
    pid = project(client)
    assert client.post(f"/api/projects/{pid}/commit", json={"message": "v"}).status_code == 201
    slack = client.get(f"/api/orgs/{ORG}/integrations").json()["integrations"]["slack"]
    assert slack["config"]["last_error"] == "Slack: HTTP 404"
