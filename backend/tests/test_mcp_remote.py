"""End-to-end tests of the remote MCP server: OAuth 2.1 (DCR + PKCE + consent), MCP calls, scopes, isolation."""

import base64
import hashlib
import json
import os
import secrets
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from authhelp import ORIGIN, member_client, new_client, signup  # noqa: E402

from archflow import auth  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.store import Store  # noqa: E402

BASE = "http://localhost:8000"
REDIRECT = "http://localhost:9999/callback"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-06-18"}


@pytest.fixture()
def env():
    store = Store(":memory:")
    with new_client(create_app(store), base_url=BASE) as client:
        signup(client)
        yield client, store


def oauth_login(client, access="write", org_id="org-default", scope="archflow:read archflow:write"):
    reg = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Test Agent",
                                         "token_endpoint_auth_method": "none",
                                         "grant_types": ["authorization_code", "refresh_token"], "scope": scope})
    assert reg.status_code == 201, reg.text
    client_id = reg.json()["client_id"]
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    r = client.get("/authorize", params={"response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
                                         "code_challenge": challenge, "code_challenge_method": "S256",
                                         "state": "xyz", "scope": scope, "resource": f"{BASE}/mcp"})
    assert r.status_code == 302, r.text
    consent_url = urlparse(r.headers["location"])
    assert consent_url.path == "/oauth/consent"
    request_id = parse_qs(consent_url.query)["request"][0]
    page = client.get("/oauth/consent", params={"request": request_id})
    assert page.status_code == 200 and "Test Agent" in page.text
    r = client.post("/oauth/consent", data={"request": request_id, "decision": "approve", "org_id": org_id,
                                            "access": access})
    assert r.status_code == 302, r.text
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["state"] == ["xyz"]
    tok = client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
                                      "client_id": client_id, "code_verifier": verifier, "resource": f"{BASE}/mcp"})
    assert tok.status_code == 200, tok.text
    # A code is single-use.
    again = client.post("/token", data={"grant_type": "authorization_code", "code": q["code"][0], "redirect_uri": REDIRECT,
                                        "client_id": client_id, "code_verifier": verifier})
    assert again.status_code == 400
    return client_id, tok.json()


def mcp_call(client, token, method, params=None, id_=1):
    r = client.post("/mcp", headers={**MCP_HEADERS, "Authorization": f"Bearer {token}"},
                    json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}})
    return r


def tool(client, token, tool_name, **args):
    r = mcp_call(client, token, "tools/call", {"name": tool_name, "arguments": args})
    assert r.status_code == 200, r.text
    body = r.json()
    res = body["result"]
    text = res["content"][0]["text"] if res.get("content") else ""
    return res.get("isError", False), (res.get("structuredContent") or {}), text


def test_discovery_and_unauthenticated(env):
    client, _ = env
    prm = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert prm["resource"].rstrip("/") == f"{BASE}/mcp"
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"].endswith("/register") and "S256" in meta["code_challenge_methods_supported"]
    r = mcp_call(client, "nope", "tools/list")
    assert r.status_code == 401 and "resource_metadata" in r.headers.get("www-authenticate", "")


def test_oauth_flow_and_tools(env):
    client, store = env
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    _, tokens = oauth_login(client)
    at = tokens["access_token"]

    init = mcp_call(client, at, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                               "clientInfo": {"name": "pytest", "version": "1"}})
    assert init.status_code == 200 and init.json()["result"]["serverInfo"]["name"] == "archflow"
    names = {t["name"] for t in mcp_call(client, at, "tools/list").json()["result"]["tools"]}
    assert {"get_architecture", "add_component", "list_tasks", "get_task_brief", "report_progress"} <= names

    err, who, _ = tool(client, at, "whoami")
    assert not err and who["user"] == "Owner" and who["access"] == "read-write"
    err, out, text = tool(client, at, "get_architecture", project_id=pid)
    assert not err and len(out["components"]) == 10

    err, out, text = tool(client, at, "add_component", project_id=pid, name="Payments", type="service", technology="Go")
    assert not err, text
    state = client.get(f"/api/projects/{pid}").json()["state"]
    assert "payments" in state["nodes"]
    assert client.get(f"/api/projects/{pid}/activity").json()[0]["actor"] == "Owner via MCP"

    err, out, text = tool(client, at, "list_tasks", project_id=pid)
    tasks = json.loads(text) if not out else out.get("result", out)
    assert any(t["id"] == "REQ-003" for t in tasks)
    err, _, brief = tool(client, at, "get_task_brief", project_id=pid, requirement_id="REQ-003")
    assert "REQ-003" in brief and "Order Service" in brief
    err, out, text = tool(client, at, "report_progress", project_id=pid, requirement_id="REQ-003", status="implemented",
                          pr_url="https://github.com/acme/shop/pull/7")
    assert not err, text
    assert client.get(f"/api/projects/{pid}").json()["state"]["requirements"]["REQ-003"]["status"] == "implemented"
    assert "pull/7" in client.get(f"/api/projects/{pid}/activity").json()[0]["summary"][0]

    # Org isolation: a project in another org is invisible.
    other = client.post("/api/orgs", json={"name": "Other"}).json()["id"]
    foreign = client.post("/api/projects", json={"name": "Secret", "org_id": other}).json()["id"]
    err, _, text = tool(client, at, "get_architecture", project_id=foreign)
    assert err and "not found" in text
    err, out, text = tool(client, at, "list_projects")
    assert "Secret" not in text


def test_read_only_scope_and_refresh_rotation(env):
    client, _ = env
    pid = client.post("/api/projects", json={"name": "P", "template": "ai-customer-support"}).json()["id"]
    client_id, tokens = oauth_login(client, access="read")
    assert tokens["scope"] == "archflow:read"
    err, _, text = tool(client, tokens["access_token"], "add_component", project_id=pid, name="X")
    assert err and "read-only" in text
    err, _, _ = tool(client, tokens["access_token"], "get_architecture", project_id=pid)
    assert not err

    r = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["refresh_token"] != tokens["refresh_token"]
    reuse = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": client_id})
    assert reuse.status_code == 400  # rotated refresh tokens cannot be replayed


def test_concurrent_refresh_issues_tokens_only_once(env):
    """Two refreshes that both load the token before either rotates it: only the first may mint new tokens."""
    import asyncio

    from mcp.server.auth.provider import TokenError

    client, _ = env
    client_id, tokens = oauth_login(client)
    provider = client.app.state.mcp._archflow_provider

    async def race():
        reg = await provider.get_client(client_id)
        a = await provider.load_refresh_token(reg, tokens["refresh_token"])
        b = await provider.load_refresh_token(reg, tokens["refresh_token"])
        assert a and b
        await provider.exchange_refresh_token(reg, a, [])
        with pytest.raises(TokenError):
            await provider.exchange_refresh_token(reg, b, [])

    asyncio.run(race())


def test_personal_access_token_lifecycle(env):
    client, store = env
    pid = client.post("/api/projects", json={"name": "P", "template": "blank"}).json()["id"]
    created = client.post("/api/orgs/org-default/tokens", json={"name": "CI", "user_name": "ignored", "access": "write"}).json()
    secret = created["token"]
    assert secret.startswith("afp_") and "token_hash" not in created
    err, _, text = tool(client, secret, "add_component", project_id=pid, name="Worker")
    assert not err, text
    listed = client.get("/api/orgs/org-default/tokens").json()
    assert listed[0]["name"] == "CI" and listed[0]["last_used_at"] and "token" not in listed[0]
    assert client.delete(f"/api/orgs/org-default/tokens/{created['id']}").status_code == 204
    assert mcp_call(client, secret, "tools/list").status_code == 401


def test_consent_denied(env):
    client, _ = env
    reg = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Nope", "token_endpoint_auth_method": "none"})
    cid = reg.json()["client_id"]
    r = client.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
                                         "code_challenge": "x" * 43, "code_challenge_method": "S256", "state": "s"})
    rid = parse_qs(urlparse(r.headers["location"]).query)["request"][0]
    r = client.post("/oauth/consent", data={"request": rid, "decision": "deny"})
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["error"] == ["access_denied"] and q["state"] == ["s"]


def start_authorize(client, scope="archflow:read archflow:write"):
    reg = client.post("/register", json={"redirect_uris": [REDIRECT], "client_name": "Agent",
                                         "token_endpoint_auth_method": "none", "scope": scope})
    cid = reg.json()["client_id"]
    r = client.get("/authorize", params={"response_type": "code", "client_id": cid, "redirect_uri": REDIRECT,
                                         "code_challenge": "x" * 43, "code_challenge_method": "S256", "state": "s",
                                         "scope": scope, "resource": f"{BASE}/mcp"})
    return parse_qs(urlparse(r.headers["location"]).query)["request"][0]


def test_consent_requires_login_and_api_login_form(env):
    client, store = env
    app = client.app
    anon = new_client(app, base_url=BASE)
    rid = start_authorize(anon)
    # Not signed in: the consent page sends you to the API-served sign-in form, which returns you here.
    r = anon.get("/oauth/consent", params={"request": rid})
    assert r.status_code == 303 and r.headers["location"].startswith("/oauth/login?next=")
    login_url = r.headers["location"]
    assert "Sign in" in anon.get(login_url).text
    # Approving without a session is impossible.
    r = anon.post("/oauth/consent", data={"request": rid, "decision": "approve", "org_id": "org-default", "access": "write"})
    assert r.status_code == 303 and "/oauth/login" in r.headers["location"]
    # Wrong password, then the right one; the redirect target is only ever a consent URL (no open redirect).
    nxt = f"/oauth/consent?request={rid}"
    bad = anon.post("/oauth/login", data={"email": "owner@example.com", "password": "wrong password!", "next": nxt})
    assert bad.status_code == 401
    ok = anon.post("/oauth/login", data={"email": "owner@example.com", "password": "correct horse battery staple",
                                         "next": nxt})
    assert ok.status_code == 303 and ok.headers["location"] == nxt
    page = anon.get(nxt)
    assert page.status_code == 200 and "Default" in page.text and "Owner" in page.text
    evil = anon.post("/oauth/login", data={"email": "owner@example.com", "password": "correct horse battery staple",
                                           "next": "https://evil.example/steal"})
    assert evil.status_code == 200 and "location" not in evil.headers
    # A login form posted from another site is rejected (login CSRF).
    cross = new_client(app, base_url=BASE)
    cross.headers["Origin"] = "https://evil.example"
    assert cross.post("/oauth/login", data={"email": "owner@example.com", "password": "x" * 12}).status_code == 403


def test_consent_lists_only_my_orgs_and_caps_scopes_by_role(env):
    client, store = env
    app = client.app
    hidden = client.post("/api/orgs", json={"name": "Owner Private"}).json()["id"]
    viewer = member_client(app, client, "viewer", "viewer@example.com", "Vera")
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    # Consent runs as the viewer; MCP calls use bearer tokens through the lifespan-managed owner client.
    rid = start_authorize(viewer)
    page = viewer.get("/oauth/consent", params={"request": rid}).text
    assert "Default" in page and "read only" in page and "Owner Private" not in page
    # Not a member of that org: refused.
    r = viewer.post("/oauth/consent", data={"request": rid, "decision": "approve", "org_id": hidden, "access": "write"})
    assert r.status_code == 400
    # Asking for write as a viewer yields a read-only token.
    _, tokens = oauth_login(viewer, access="write")
    assert tokens["scope"] == "archflow:read"
    err, _, text = tool(client, tokens["access_token"], "add_component", project_id=pid, name="X")
    assert err and "read-only" in text
    err, who, _ = tool(client, tokens["access_token"], "whoami")
    assert not err and who["user"] == "Vera" and who["access"] == "read-only"


def test_removed_member_loses_mcp_access_immediately(env):
    client, store = env
    app = client.app
    dev = member_client(app, client, "developer", "dev@example.com", "Dev")
    pid = client.post("/api/projects", json={"name": "Shop", "template": "blank"}).json()["id"]
    dev_id = dev.get("/api/me").json()["user"]["id"]
    _, tokens = oauth_login(dev)  # consent as the developer; MCP calls via the lifespan-managed client
    err, _, text = tool(client, tokens["access_token"], "add_component", project_id=pid, name="Worker")
    assert not err, text
    # Demoted to viewer: the same token is now read-only.
    assert client.patch(f"/api/orgs/org-default/members/{dev_id}", json={"role": "viewer"}).status_code == 200
    err, _, text = tool(client, tokens["access_token"], "add_component", project_id=pid, name="Other")
    assert err and "read-only" in text
    # Removed: the token stops working (and the refresh token cannot mint new ones).
    assert client.delete(f"/api/orgs/org-default/members/{dev_id}").status_code == 204
    assert mcp_call(client, tokens["access_token"], "tools/list").status_code == 401


def test_membership_recheck_even_if_tokens_not_revoked(env):
    client, store = env
    app = client.app
    dev = member_client(app, client, "developer", "dev2@example.com", "Dev2")
    dev_id = dev.get("/api/me").json()["user"]["id"]
    _, tokens = oauth_login(dev)
    assert mcp_call(client, tokens["access_token"], "tools/list").status_code == 200
    store.remove_member("org-default", dev_id)  # bypass the API (no token revocation): the re-check still applies
    assert mcp_call(client, tokens["access_token"], "tools/list").status_code == 401


def test_pat_requires_admin_and_is_owned_by_creator(env):
    client, store = env
    app = client.app
    dev = member_client(app, client, "developer", "dev3@example.com", "Dev3")
    assert dev.post("/api/orgs/org-default/tokens", json={"name": "CI", "access": "write"}).status_code == 403
    assert dev.get("/api/orgs/org-default/tokens").status_code == 403
    created = client.post("/api/orgs/org-default/tokens", json={"name": "CI", "user_name": "spoof", "access": "write"})
    assert created.status_code == 201
    rec = created.json()
    assert rec["user_name"] == "Owner" and rec["user_id"] == client.get("/api/me").json()["user"]["id"]
    assert ORIGIN  # clients send the trusted dev origin


def test_memory_and_conversation_tools(env):
    client, store = env
    pid = client.post("/api/projects", json={"name": "Shop", "template": "blank"}).json()["id"]
    _, tokens = oauth_login(client)
    at = tokens["access_token"]
    err, out, text = tool(client, at, "remember", project_id=pid, text="All data stays in the EU.",
                          category="constraint", pinned=True)
    assert not err, text
    assert client.get(f"/api/projects/{pid}/memory").json()[0]["source"] == "mcp"
    err, _, text = tool(client, at, "remember", project_id=pid, text="all data stays in the EU")
    assert err and "already" in text
    err, _, text = tool(client, at, "get_project_memory", project_id=pid)
    assert not err and "All data stays in the EU." in text

    conv = client.post(f"/api/projects/{pid}/conversations", json={"kind": "assistant"}).json()
    store.add_message(conv["id"], "user", "ask", "Why Redis?", {}, "Owner")
    store.add_message(conv["id"], "assistant", "ask", "For caching.", {}, "ArchFlow AI")
    err, _, text = tool(client, at, "list_conversations", project_id=pid)
    assert not err and conv["id"] in text
    err, out, text = tool(client, at, "get_conversation", project_id=pid, conversation_id=conv["id"])
    assert not err and json.loads(text)["transcript"] == "User (Owner): Why Redis?\nArchFlow: For caching."

    _, ro = oauth_login(client, access="read", scope="archflow:read")
    err, _, text = tool(client, ro["access_token"], "remember", project_id=pid, text="Use Kafka")
    assert err and "read-only" in text


def test_generate_change_spec_tool(env, monkeypatch):
    from archflow import llm
    monkeypatch.setattr(llm, "available", lambda org_id=None: False)
    client, _ = env
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    _, ro = oauth_login(client, access="read", scope="archflow:read")
    err, _, text = tool(client, ro["access_token"], "generate_change_spec", project_id=pid)
    assert err and "read-only" in text
    _, rw = oauth_login(client)
    err, out, text = tool(client, rw["access_token"], "generate_change_spec", project_id=pid, requirement_ids=["REQ-003"])
    assert not err, text
    files = json.loads(text)["files"] if not out.get("files") else out["files"]
    assert any(k.endswith("/tasks.md") and k.startswith("openspec/changes/") for k in files)
    err, _, text = tool(client, rw["access_token"], "get_openspec", project_id=pid)
    assert not err and "changes" in text
    path = next(k for k in files if k.endswith("/proposal.md"))
    err, _, text = tool(client, rw["access_token"], "get_openspec_file", project_id=pid, path=path)
    assert not err and "## Why" in text


def test_org_sign_in_rules_apply_to_agents(env):
    """Tokens granted before an admin turned on "require two-step sign-in" or "require SSO" used to keep working over
    MCP, although the same person's browser session was blocked."""
    client, store = env
    pid = client.post("/api/projects", json={"name": "P", "template": "blank"}).json()["id"]
    dev = member_client(client.app, client, "developer", "dev@example.com", "Dev")
    _, dev_tokens = oauth_login(dev)  # granted from a password session
    _, owner_tokens = oauth_login(client)
    assert not tool(client, dev_tokens["access_token"], "get_architecture", project_id=pid)[0]

    store.set_auth_settings("org-default", None, require_mfa=True)  # Dev has no second factor
    err, _, text = tool(client, dev_tokens["access_token"], "get_architecture", project_id=pid)
    assert err and "two-step sign-in" in text
    store.set_auth_settings("org-default", None, require_mfa=False, sso_enabled=True, require_sso=True)
    err, _, text = tool(client, dev_tokens["access_token"], "get_architecture", project_id=pid)
    assert err and "single sign-on" in text and "Reconnect" in text
    # owners stay exempt from require-SSO, as on the web (break-glass)
    assert not tool(client, owner_tokens["access_token"], "get_architecture", project_id=pid)[0]
    # the token remembers how its user had signed in when it was granted
    assert store.get_token(auth.hash_secret(dev_tokens["access_token"]))["auth_method"] == "password"
