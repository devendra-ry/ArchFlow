"""Two-way Jira sync: webhook auth and live status, deleted issues, the on-demand sync, listing unlinked issues,
importing issues as requirements (mapping, ADF to text, no duplicates) and the Jira MCP tools. Jira is a fake
behind httpx.MockTransport (see test_jira.py): no network."""

import hashlib
import hmac
import json
import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import httpx  # noqa: E402
import pytest  # noqa: E402

from archflow import jira, jira_sync  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.store import Store  # noqa: E402

from authhelp import authed_client, member_client, new_client, ws_headers  # noqa: E402
from test_jira import ORG, SITE, TEST_SECRET, FakeJira, connect  # noqa: E402
from test_mcp_remote import BASE, oauth_login, tool  # noqa: E402


def adf_doc(*blocks):
    return {"type": "doc", "version": 1, "content": list(blocks)}


def para(*parts):
    return {"type": "paragraph", "content": [p if isinstance(p, dict) else {"type": "text", "text": p} for p in parts]}


def heading(text):
    return {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": text}]}


def bullets(*items, kind="bulletList"):
    return {"type": kind, "content": [{"type": "listItem", "content": [para(i)]} for i in items]}


class SyncJira(FakeJira):
    """FakeJira plus native issues (with ADF descriptions) and paginated JQL search."""

    def __init__(self):
        super().__init__()
        self.jql: list[str] = []

    def seed(self, key, summary, description=None, *, type_="Story", priority="Medium", labels=(), status="To Do",
             category="new"):
        self.issues[key] = {"summary": summary, "description": description, "issuetype": {"name": type_},
                            "priority": {"name": priority}, "labels": list(labels), "status": status,
                            "category": category}

    def view(self, key):
        f = self.issues[key]
        return {"key": key, "fields": {
            "summary": f["summary"], "description": f.get("description"),
            "issuetype": f.get("issuetype") if "name" in (f.get("issuetype") or {}) else {"name": "Story"},
            "priority": f.get("priority"), "labels": f.get("labels") or [],
            "status": {"name": f["status"], "statusCategory": {"key": f.get("category", "new")}}}}

    def handler(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/rest/api/3/search/jql" and "labels =" not in body.get("jql", ""):
            self.calls.append((request.method, path))
            self.jql.append(body["jql"])
            keys = sorted(self.issues, key=lambda k: -int(k.split("-")[1]))  # ORDER BY created DESC
            start, n = int(body.get("nextPageToken") or 0), body["maxResults"]
            nxt = start + n if start + n < len(keys) else None
            return httpx.Response(200, json={"issues": [self.view(k) for k in keys[start:start + n]],
                                             **({"nextPageToken": str(nxt)} if nxt else {"isLast": True})})
        if path == "/rest/api/3/issue/bulkfetch" and body.get("fields") != ["status"]:
            self.calls.append((request.method, path))
            keys = body["issueIdsOrKeys"]
            return httpx.Response(200, json={"issues": [self.view(k) for k in keys if k in self.issues],
                                             "issueErrors": [{"issueIdOrKey": k} for k in keys if k not in self.issues]})
        return super().handler(request)


@pytest.fixture()
def fake(monkeypatch):
    f = SyncJira()
    monkeypatch.setenv("ARCHFLOW_SECRET_KEY", TEST_SECRET)
    monkeypatch.setattr(jira, "_transport", httpx.MockTransport(f.handler))
    monkeypatch.setattr(jira, "_resolve", lambda host: ["20.0.0.1"])
    monkeypatch.setattr(jira, "_sleep", lambda s: None)
    return f


@pytest.fixture()
def env(fake):
    store = Store(":memory:")
    app = create_app(store)
    owner = authed_client(app)
    dev = member_client(app, owner, "developer", "dev@example.com", "Dev")
    pid = owner.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    assert connect(owner).status_code == 200
    return {"app": app, "store": store, "owner": owner, "dev": dev, "pid": pid, "fake": fake}


def hook(app, org=ORG, secret=None, payload=None, headers=None):
    """Jira calling the webhook: no session cookie."""
    raw = json.dumps(payload or {}).encode()
    params = {"secret": secret} if secret is not None else {}
    jira_side = new_client(app)  # not as a context manager: the app's lifespan is already running elsewhere
    return jira_side.post(f"/api/integrations/jira/webhook/{org}", params=params, content=raw,
                          headers={"Content-Type": "application/json", **(headers or {})})


def issue_event(event, key, status="In Progress", category="indeterminate"):
    return {"webhookEvent": event, "issue": {"key": key, "fields": {
        "summary": "x", "status": {"name": status, "statusCategory": {"key": category}}}}}


# --------------------------------------------------------------------------- ADF → text, mapping (pure)
def test_adf_to_text_and_acceptance_criteria():
    doc = adf_doc(
        para("As a customer, I want to export my orders, so that I can do my taxes."),
        para("Line one", {"type": "hardBreak"}, "line two ", {"type": "mention", "attrs": {"text": "@Ana"}}),
        heading("Acceptance criteria"),
        bullets("CSV has one row per order", "Export finishes within 10 seconds"),
        heading("Notes"),
        bullets("first", "second", kind="orderedList"),
        {"type": "codeBlock", "content": [{"type": "text", "text": "GET /orders.csv"}]},
        {"type": "rule"},
        {"type": "table", "content": [{"type": "tableRow", "content": [
            {"type": "tableCell", "content": [para("a")]}, {"type": "tableCell", "content": [para("b")]}]}]},
        para("Given a signed-in customer, when they click Export, then a file downloads"),
        {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "x"}}]},
    )
    text = jira_sync.adf_to_text(doc)
    assert "Line one\nline two @Ana" in text
    assert "- CSV has one row per order" in text and "- first" in text and "GET /orders.csv" in text
    assert "---" in text and "a | b" in text
    body, criteria = jira_sync.split_criteria(jira_sync.adf_blocks(doc))
    assert criteria == ["CSV has one row per order", "Export finishes within 10 seconds",
                        "Given a signed-in customer, when they click Export, then a file downloads"]
    assert "CSV has one row" not in body and "Notes" in body and "- first" in body
    # Jira wiki text (older API versions) works too
    wiki = "h3. Acceptance criteria\n* works offline\n* syncs later\nh3. Other\nplain"
    assert jira_sync.split_criteria(jira_sync.adf_blocks(wiki)) == ("Other\n\nplain", ["works offline", "syncs later"])
    assert jira_sync.adf_to_text(None) == "" and jira_sync.adf_to_text({"type": "doc", "content": []}) == ""


def test_issue_mapping_rules():
    raw = {"key": "SHOP-7", "fields": {
        "summary": "Checkout p95 under 300 ms", "issuetype": {"name": "Task"}, "priority": {"name": "Highest"},
        "labels": ["NFR", "latency"], "status": {"name": "Done", "statusCategory": {"key": "done"}},
        "description": adf_doc(para("Measured at the gateway."), heading("Definition of done"), bullets("Load test passes"))}}
    info = jira_sync.issue_info(raw, SITE)
    assert info["url"] == f"{SITE}/browse/SHOP-7"
    m = jira_sync.map_issue(info)
    assert m == {"title": "Checkout p95 under 300 ms", "description": "Measured at the gateway.",
                 "kind": "non_functional", "category": "performance", "priority": "critical", "actors": [],
                 "acceptance_criteria": ["Load test passes"], "status": "implemented"}
    story = jira_sync.map_issue(jira_sync.issue_info({"key": "SHOP-8", "fields": {
        "summary": "Track my parcel", "issuetype": {"name": "Story"}, "priority": {"name": "Low"},
        "labels": ["performance"], "description": "As a customer, I want to see where my parcel is."}}, SITE))
    # a quality-area label alone does not make a story non-functional
    assert story["kind"] == "functional" and story["category"] == "" and story["priority"] == "low"
    assert story["actors"] == ["customer"] and story["status"] == "draft"


def test_ai_mapping_is_validated_and_gaps_filled(monkeypatch):
    info = jira_sync.issue_info({"key": "SHOP-9", "fields": {"summary": "Login with SSO", "priority": {"name": "High"},
                                                             "description": "Use the company IdP."}}, SITE)
    monkeypatch.setattr(jira_sync.llm, "available", lambda org_id=None: True)
    monkeypatch.setattr(jira_sync.agents, "run_json", lambda *a, **kw: {"requirements": [
        {"key": "SHOP-9", "title": "Sign in with single sign-on", "kind": "weird", "priority": "urgent",
         "acceptance_criteria": ["Users sign in through the company IdP", 42], "actors": ["employee"]},
        {"key": "OTHER-1", "title": "not asked for", "kind": "functional", "priority": "low", "acceptance_criteria": []}]})
    mapped, engine = jira_sync.map_issues([info], use_ai=True, org_id=None)
    assert engine == "llm" and set(mapped) == {"SHOP-9"}
    m = mapped["SHOP-9"]
    assert m["title"] == "Sign in with single sign-on" and m["kind"] == "functional"  # invalid kind: rules
    assert m["priority"] == "high"  # invalid priority: rules (High → high)
    assert m["acceptance_criteria"] == ["Users sign in through the company IdP"] and m["actors"] == ["employee"]
    assert m["description"] == "Use the company IdP."


# --------------------------------------------------------------------------- webhooks
def test_webhook_secret_is_shown_once_and_checked(env):
    app, owner, dev, store = env["app"], env["owner"], env["dev"], env["store"]
    event = issue_event("jira:issue_updated", "SHOP-1")
    assert hook(app, secret="anything", payload=event).status_code == 401  # no secret set up yet
    assert dev.post(f"/api/orgs/{ORG}/jira/webhook").status_code == 403  # admins only
    r = owner.post(f"/api/orgs/{ORG}/jira/webhook")
    assert r.status_code == 200, r.text
    out = r.json()
    secret = out["secret"]
    assert len(secret) >= 32 and out["url"] == f"http://localhost:8000/api/integrations/jira/webhook/{ORG}?secret={secret}"
    assert out["jql"] == 'project = "SHOP"' and out["settings"]["webhook"]["configured"]
    assert secret not in owner.get(f"/api/orgs/{ORG}/jira").text  # never shown again
    assert secret not in store.get_jira_connection(ORG)["webhook_ciphertext"]  # stored encrypted

    assert hook(app, secret="wrong", payload=event).status_code == 401
    assert hook(app, payload=event).status_code == 401
    assert hook(app, org="org-other", secret=secret, payload=event).status_code == 401
    assert hook(app, secret=secret, payload=event).status_code == 200
    # Jira's own "Secret" field: an HMAC-SHA256 signature of the body
    raw = json.dumps(event).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    assert hook(app, payload=event, headers={"X-Hub-Signature": sig}).status_code == 200
    assert hook(app, payload=event, headers={"X-Hub-Signature": "sha256=" + "0" * 64}).status_code == 401

    # a new secret replaces the old one; deleting turns the webhook off
    new = owner.post(f"/api/orgs/{ORG}/jira/webhook").json()["secret"]
    assert hook(app, secret=secret, payload=event).status_code == 401
    assert hook(app, secret=new, payload=event).status_code == 200
    assert owner.delete(f"/api/orgs/{ORG}/jira/webhook").status_code == 204
    assert hook(app, secret=new, payload=event).status_code == 401
    assert not owner.get(f"/api/orgs/{ORG}/jira").json()["webhook"]["configured"]


def test_webhook_updates_status_and_broadcasts(env):
    app, owner, pid, store = env["app"], env["owner"], env["pid"], env["store"]
    owner.post(f"/api/projects/{pid}/jira/push", json={"confirm": True})
    secret = owner.post(f"/api/orgs/{ORG}/jira/webhook").json()["secret"]
    key = next(x["external_key"] for x in store.list_external_links(pid) if x["object_id"] == "REQ-003")
    with owner.websocket_connect(f"/ws/{pid}/main", headers=ws_headers(owner)) as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "presence"
        r = hook(app, secret=secret, payload=issue_event("jira:issue_updated", key, "Done", "done"))
        assert r.status_code == 200 and r.json()["updated"] == 1 and r.json()["projects"] == 1
        msg = ws.receive_json()
        assert msg == {"type": "jira", "actor": "Jira", "key": key, "event": "jira:issue_updated"}
    row = next(x for x in owner.get(f"/api/projects/{pid}/jira/links").json()["links"] if x["external_key"] == key)
    assert row["status"] == "Done" and row["status_category"] == "done" and row["status_checked_at"]
    # requirement status in ArchFlow is not changed by Jira
    assert owner.get(f"/api/projects/{pid}").json()["state"]["requirements"]["REQ-003"]["status"] != "implemented"
    # unknown issues and other events are ignored
    r = hook(app, secret=secret, payload=issue_event("jira:issue_updated", "SHOP-999")).json()
    assert r["updated"] == 0 and not r["ignored"]
    assert hook(app, secret=secret, payload={"webhookEvent": "comment_created"}).json()["ignored"]
    assert hook(app, secret=secret, payload=issue_event("jira:issue_updated", "not a key")).json()["ignored"]
    bad = new_client(app).post(f"/api/integrations/jira/webhook/{ORG}", params={"secret": secret}, content=b"{nope",
                               headers={"Content-Type": "application/json"})
    assert bad.status_code == 400


def test_webhook_deleted_issue_unlinks_and_push_recreates(env):
    app, owner, pid, store, fake = env["app"], env["owner"], env["pid"], env["store"], env["fake"]
    owner.post(f"/api/projects/{pid}/jira/push", json={"confirm": True})
    secret = owner.post(f"/api/orgs/{ORG}/jira/webhook").json()["secret"]
    key = next(x["external_key"] for x in store.list_external_links(pid) if x["object_id"] == "REQ-004")
    del fake.issues[key]
    r = hook(app, secret=secret, payload=issue_event("jira:issue_deleted", key)).json()
    assert r["removed"] == 1
    assert all(x["external_key"] != key for x in store.list_external_links(pid))
    acts = owner.get(f"/api/projects/{pid}/activity").json()
    assert acts[0]["actor"] == "Jira" and key in acts[0]["summary"][0] and "REQ-004" in acts[0]["summary"][0]
    plan = owner.post(f"/api/projects/{pid}/jira/plan", json={}).json()
    assert [i["ref"] for i in plan["items"] if i["action"] == "create"] == ["story:REQ-004"]


def test_sync_now_reuses_refresh(env):
    owner, pid, fake = env["owner"], env["pid"], env["fake"]
    owner.post(f"/api/projects/{pid}/jira/push", json={"confirm": True})
    for i in fake.issues.values():
        i["status"] = "In Review"
    r = owner.post(f"/api/projects/{pid}/jira/refresh").json()
    assert r["updated"] == 7 and {x["status"] for x in r["links"] if x["object_kind"] != "link"} == {"In Review"}


# --------------------------------------------------------------------------- pulling issues in
def seed_backlog(fake):
    fake.seed("SHOP-101", "Export orders as CSV", adf_doc(
        para("As a customer, I want to export my orders."), heading("Acceptance criteria"),
        bullets("One row per order", "Includes totals")), priority="High")
    fake.seed("SHOP-102", "Encrypt data at rest", adf_doc(para("All stores encrypt with KMS keys.")),
              labels=["nfr", "security"], priority="Highest")
    fake.seed("SHOP-103", "Gift cards", None, priority="Low", status="Done", category="done")


def test_list_unlinked_issues_paginated_with_preview(env):
    owner, dev, pid, fake = env["owner"], env["dev"], env["pid"], env["fake"]
    owner.post(f"/api/projects/{pid}/jira/push", json={"confirm": True})  # SHOP-1..7: linked, so hidden
    seed_backlog(fake)
    r = dev.get(f"/api/projects/{pid}/jira/issues", params={"limit": 2})
    assert r.status_code == 200, r.text
    page = r.json()
    assert [i["key"] for i in page["issues"]] == ["SHOP-103", "SHOP-102"] and page["next_page_token"]
    assert 'project = "SHOP"' in page["jql"] and "subTaskIssueTypes()" in page["jql"]
    nfr = page["issues"][1]
    assert nfr["requirement"]["kind"] == "non_functional" and nfr["requirement"]["category"] == "security"
    assert nfr["requirement"]["priority"] == "critical" and nfr["url"] == f"{SITE}/browse/SHOP-102"
    nxt = dev.get(f"/api/projects/{pid}/jira/issues", params={"limit": 2, "page_token": page["next_page_token"]}).json()
    assert [i["key"] for i in nxt["issues"]] == ["SHOP-101"] and nxt["hidden_linked"] == 7
    assert nxt["next_page_token"] is None
    assert nxt["issues"][0]["requirement"]["acceptance_criteria"] == ["One row per order", "Includes totals"]
    # text search goes into the JQL, quotes stripped
    dev.get(f"/api/projects/{pid}/jira/issues", params={"q": 'csv" OR 1=1'})
    assert fake.jql[-1].startswith('project = "SHOP"') and 'text ~ "csv  OR 1=1"' in fake.jql[-1]
    viewer = member_client(env["app"], owner, "viewer", "v@example.com", "V")
    assert viewer.get(f"/api/projects/{pid}/jira/issues").status_code == 403


def test_import_maps_issues_and_never_duplicates(env):
    owner, dev, pid, fake, store = env["owner"], env["dev"], env["pid"], env["fake"], env["store"]
    seed_backlog(fake)
    r = dev.post(f"/api/projects/{pid}/jira/import", json={"keys": ["SHOP-101", "shop-102", "SHOP-404"], "ai": False})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["engine"] == "offline" and out["skipped"] == [{"key": "SHOP-404", "reason": "not found in Jira"}]
    got = {i["key"]: i for i in out["imported"]}
    assert set(got) == {"SHOP-101", "SHOP-102"}
    reqs = owner.get(f"/api/projects/{pid}").json()["state"]["requirements"]
    csv = reqs[got["SHOP-101"]["requirement_id"]]
    assert csv["title"] == "Export orders as CSV" and csv["priority"] == "high" and csv["kind"] == "functional"
    assert csv["acceptance_criteria"] == ["One row per order", "Includes totals"]
    assert csv["description"] == "As a customer, I want to export my orders." and csv["actors"] == ["customer"]
    assert csv["source"]["type"] == "jira" and csv["source"]["ref"] == "SHOP-101"
    assert csv["source"]["url"] == f"{SITE}/browse/SHOP-101"
    enc = reqs[got["SHOP-102"]["requirement_id"]]
    assert enc["kind"] == "non_functional" and enc["category"] == "security" and enc["priority"] == "critical"
    links = {x["external_key"]: x for x in store.list_external_links(pid)}
    assert links["SHOP-101"]["object_id"] == csv["id"] and links["SHOP-101"]["status"] == "To Do"
    assert "Dev (Jira import)" in owner.get(f"/api/projects/{pid}/activity").json()[0]["actor"]

    # importing again: skipped, nothing added
    again = dev.post(f"/api/projects/{pid}/jira/import", json={"keys": ["SHOP-101", "SHOP-103"], "ai": False}).json()
    assert [i["key"] for i in again["imported"]] == ["SHOP-103"]
    assert again["skipped"] == [{"key": "SHOP-101", "reason": "already linked"}]
    after = owner.get(f"/api/projects/{pid}").json()["state"]["requirements"]
    assert sum(r["source"].get("ref") == "SHOP-101" for r in after.values()) == 1
    assert after[again["imported"][0]["requirement_id"]]["status"] == "implemented"  # Done in Jira
    # and they no longer show as unlinked
    assert dev.get(f"/api/projects/{pid}/jira/issues").json()["issues"] == []

    # the next push leaves imported issues alone (no overwrite of the Jira description) ...
    plan = owner.post(f"/api/projects/{pid}/jira/plan", json={}).json()
    by_ref = {i["ref"]: i for i in plan["items"]}
    assert by_ref[f"story:{csv['id']}"]["action"] == "skip" and by_ref[f"story:{csv['id']}"]["external_key"] == "SHOP-101"
    # ... until the requirement changes in ArchFlow
    owner.post(f"/api/projects/{pid}/ops", json={"ops": [
        {"op": "update_requirement", "id": csv["id"], "changes": {"title": "Export orders as CSV or Excel"}}]})
    plan = owner.post(f"/api/projects/{pid}/jira/plan", json={}).json()
    assert next(i for i in plan["items"] if i["ref"] == f"story:{csv['id']}")["action"] == "update"

    # bad input
    assert dev.post(f"/api/projects/{pid}/jira/import", json={"keys": ["not-a-key!"]}).status_code == 422
    assert dev.post(f"/api/projects/{pid}/jira/import", json={"keys": []}).status_code == 422


def test_import_skips_issue_already_imported_on_the_branch(env):
    dev, pid, fake, store = env["dev"], env["pid"], env["fake"], env["store"]
    seed_backlog(fake)
    out = dev.post(f"/api/projects/{pid}/jira/import", json={"keys": ["SHOP-101"], "ai": False}).json()
    rid = out["imported"][0]["requirement_id"]
    store.delete_external_link(pid, "requirement", rid)  # the link is lost: the requirement's source still knows
    again = dev.post(f"/api/projects/{pid}/jira/import", json={"keys": ["SHOP-101"], "ai": False}).json()
    assert again["imported"] == [] and again["skipped"][0]["key"] == "SHOP-101"


# --------------------------------------------------------------------------- MCP tools
@pytest.fixture()
def mcp_env(fake):
    store = Store(":memory:")
    app = create_app(store)
    with new_client(app, base_url=BASE) as client:
        from authhelp import signup

        signup(client)
        pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
        yield client, store, pid, fake


def test_mcp_jira_tools(mcp_env):
    client, store, pid, fake = mcp_env
    _, tokens = oauth_login(client)
    at = tokens["access_token"]
    err, _, text = tool(client, at, "jira_push_preview", project_id=pid)
    assert err and "not connected" in text
    assert connect(client).status_code == 200

    err, plan, text = tool(client, at, "jira_push_preview", project_id=pid, ai=False)
    assert not err, text
    assert plan["project_key"] == "SHOP" and plan["counts"]["create"] == 8 and fake.calls == []
    err, _, text = tool(client, at, "jira_push", project_id=pid, ai=False)
    assert err and "confirm=true" in text and fake.calls == []
    err, out, text = tool(client, at, "jira_push", project_id=pid, confirm=True, ai=False)
    assert not err, text
    assert out["counts"]["created"] == 8 and out["failed"] == []
    assert client.get(f"/api/projects/{pid}/activity").json()[0]["actor"] == "Owner via MCP"

    key = next(x["external_key"] for x in store.list_external_links(pid) if x["object_id"] == "REQ-003")
    fake.issues[key]["status"] = "Done"
    err, out, text = tool(client, at, "jira_sync_status", project_id=pid)
    assert not err, text
    assert out["updated"] == 7 and any(x["external_key"] == key and x["status"] == "Done" for x in out["links"])

    seed_backlog(fake)
    err, out, text = tool(client, at, "jira_list_unlinked_issues", project_id=pid, limit=10)
    assert not err, text
    assert [i["key"] for i in out["issues"]] == ["SHOP-103", "SHOP-102", "SHOP-101"]
    err, out, text = tool(client, at, "jira_import_issues", project_id=pid, keys=["SHOP-101", "SHOP-101", "SHOP-102"],
                          ai=False)
    assert not err, text
    assert [i["key"] for i in out["imported"]] == ["SHOP-101", "SHOP-102"]
    err, out, text = tool(client, at, "jira_import_issues", project_id=pid, keys=["SHOP-101"], ai=False)
    assert not err and out["imported"] == [] and out["skipped"][0]["reason"] == "already linked"
    err, _, text = tool(client, at, "jira_import_issues", project_id=pid, keys=["bad key"])
    assert err and "Not a Jira issue key" in text

    # read-only connections cannot use the Jira tools (developer+ only, like the REST routes)
    _, ro = oauth_login(client, access="read")
    for name, args in (("jira_push_preview", {}), ("jira_push", {"confirm": True}), ("jira_sync_status", {}),
                       ("jira_list_unlinked_issues", {}), ("jira_import_issues", {"keys": ["SHOP-103"]})):
        err, _, text = tool(client, ro["access_token"], name, project_id=pid, **args)
        assert err and "read-only" in text, name
    # other orgs' projects are invisible
    other = client.post("/api/orgs", json={"name": "Other"}).json()["id"]
    foreign = client.post("/api/projects", json={"name": "Secret", "org_id": other}).json()["id"]
    err, _, text = tool(client, at, "jira_sync_status", project_id=foreign)
    assert err and "not found" in text
