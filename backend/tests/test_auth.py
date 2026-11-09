"""Accounts, sessions, roles, invites, CSRF, collab tickets and org AI keys (BYOK)."""

import os
import sqlite3

from sqlalchemy import inspect, text
import time
from types import SimpleNamespace

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from starlette.websockets import WebSocketDisconnect  # noqa: E402

from archflow import collab_sync, llm, secrets_box, users  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.store import DEFAULT_ORG_ID, Store  # noqa: E402

from authhelp import PASSWORD, authed_client, member_client, new_client, signup, ws_headers  # noqa: E402

TEST_SECRET = "test-only-secret-key-not-for-production"


@pytest.fixture()
def app():
    return create_app(Store(":memory:"))


@pytest.fixture()
def owner(app):
    return authed_client(app)


def make_project(client, org_id=DEFAULT_ORG_ID, template="ai-customer-support"):
    r = client.post("/api/projects", json={"name": "Shop", "template": template, "org_id": org_id})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------- passwords and rate limiting
def test_password_hashing():
    h = users.hash_password("a long enough password")
    assert h.startswith("scrypt$16384$8$1$") and "a long enough password" not in h
    assert users.verify_password("a long enough password", h)
    assert not users.verify_password("a long enough passworD", h)
    assert users.hash_password("same password!") != users.hash_password("same password!")  # salted
    assert not users.verify_password("anything", None) and not users.verify_password("x", "garbage")
    assert users.check_password_rules("short") and users.check_password_rules("ten chars!") is None


def test_login_rate_limit(app):
    signup(new_client(app), "rl@example.com", "RL")
    c = new_client(app)
    for _ in range(10):
        assert c.post("/api/auth/login", json={"email": "rl@example.com", "password": "wrong password"}).status_code == 401
    r = c.post("/api/auth/login", json={"email": "rl@example.com", "password": PASSWORD})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0  # even the right password waits
    users.login_limiter.reset()
    assert c.post("/api/auth/login", json={"email": "RL@example.com", "password": PASSWORD}).status_code == 200


def test_limiter_window():
    lim = users.InMemoryLoginLimiter(max_failures=2, window=60)
    lim.failure("k")
    assert lim.check("k") == 0
    lim.failure("k")
    assert lim.check("k") > 0 and lim.check("other") == 0
    lim.success("k")
    assert lim.check("k") == 0


# ---------------------------------------------------------------- signup, login, logout, me
def test_signup_login_logout_me(app):
    c = new_client(app)
    assert c.get("/api/me").status_code == 401
    assert c.get("/api/projects").status_code == 401
    assert c.post("/api/auth/signup", json={"email": "bad", "name": "X", "password": PASSWORD}).status_code == 422
    assert c.post("/api/auth/signup", json={"email": "a@example.com", "name": "X", "password": "short"}).status_code == 422
    me = signup(c, "Alice@Example.com", "Alice")
    assert me["user"]["email"] == "alice@example.com" and "password_hash" not in me["user"]
    cookie = c.cookies.get(users.SESSION_COOKIE)
    assert cookie and cookie.startswith("afs_")
    raw = c.post("/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
    set_cookie = raw.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie and "max-age=1209600" in set_cookie
    assert c.get("/api/me").json()["user"]["name"] == "Alice"
    assert c.post("/api/auth/signup", json={"email": "alice@example.com", "name": "A", "password": PASSWORD}).status_code == 409
    # The session token is stored only as a hash.
    store = app.state.store
    assert store.get_session(users.session_hash(c.cookies.get(users.SESSION_COOKIE)))
    assert c.post("/api/auth/logout").status_code == 204
    assert c.get("/api/me").status_code == 401
    assert new_client(app).post("/api/auth/login", json={"email": "alice@example.com", "password": "nope nope nope"}).status_code == 401


def test_bootstrap_first_signup_claims_legacy_orgs(app):
    store = app.state.store
    legacy = store.create_org("Legacy Co")
    pid = store.create_project("Old project", org_id=DEFAULT_ORG_ID)["id"]
    first = authed_client(app, "first@example.com", "First")
    orgs = {o["id"]: o["role"] for o in first.get("/api/me").json()["orgs"]}
    assert orgs == {DEFAULT_ORG_ID: "owner", legacy["id"]: "owner"}
    assert first.get(f"/api/projects/{pid}").status_code == 200
    # Later signups without an invite get their own workspace and see nothing else.
    second = authed_client(app, "second@example.com", "Sam")
    orgs2 = second.get("/api/me").json()["orgs"]
    assert [(o["name"], o["role"]) for o in orgs2] == [("Sam's workspace", "owner")]
    assert second.get(f"/api/projects/{pid}").status_code == 404
    assert second.get("/api/projects").json() == []
    assert second.get(f"/api/projects?org_id={DEFAULT_ORG_ID}").status_code == 404


def test_invite_only_signup(app, monkeypatch):
    authed_client(app)  # first user always allowed
    monkeypatch.setenv("ARCHFLOW_OPEN_SIGNUP", "false")
    c = new_client(app)
    assert c.post("/api/auth/signup", json={"email": "x@example.com", "name": "X", "password": PASSWORD}).status_code == 403


# ---------------------------------------------------------------- roles
def test_role_matrix(app, owner):
    pid = make_project(owner)
    viewer = member_client(app, owner, "viewer", "viewer@example.com")
    reviewer = member_client(app, owner, "reviewer", "reviewer@example.com")
    dev = member_client(app, owner, "developer", "dev@example.com", "Dev")
    admin = member_client(app, owner, "admin", "admin@example.com")
    outsider = authed_client(app, "out@example.com", "Out")
    op = {"ops": [{"op": "add_node", "node": {"name": "Cache", "type": "cache"}}]}

    for c in (viewer, reviewer, dev, admin):
        assert c.get(f"/api/projects/{pid}").status_code == 200
        assert c.get(f"/api/projects/{pid}/analysis").status_code == 200
        assert c.get("/api/orgs/org-default/members").status_code == 200
    assert viewer.get(f"/api/projects/{pid}").json()["role"] == "viewer"
    # Non-members: 404 everywhere (no existence leak).
    assert outsider.get(f"/api/projects/{pid}").status_code == 404
    assert outsider.post(f"/api/projects/{pid}/ops", json=op).status_code == 404
    assert outsider.get("/api/orgs/org-default/members").status_code == 404

    # Editing: developer and above.
    for c in (viewer, reviewer):
        assert c.post(f"/api/projects/{pid}/ops", json=op).status_code == 403
        assert c.post(f"/api/projects/{pid}/commit", json={"message": "x"}).status_code == 403
        assert c.post(f"/api/projects/{pid}/branches", json={"name": "b"}).status_code == 403
        assert c.post(f"/api/projects/{pid}/ai/command", json={"text": "add Redis"}).status_code == 403
        assert c.post(f"/api/projects/{pid}/convert", json={"target": "aws"}).status_code == 403
        assert c.post(f"/api/projects/{pid}/nodes/order-service/lld", json={}).status_code == 403
        assert c.post(f"/api/projects/{pid}/decisions", json={"title": "x"}).status_code == 403
        assert c.post("/api/projects", json={"name": "N", "org_id": "org-default"}).status_code == 403
    r = dev.post(f"/api/projects/{pid}/ops", json={**op, "actor": "someone else"})
    assert r.status_code == 200
    assert owner.get(f"/api/projects/{pid}/activity").json()[0]["actor"] == "Dev"
    assert dev.post(f"/api/projects/{pid}/commit", json={"message": "v", "author": "spoof"}).json()["author"] == "Dev"

    # Reviewers may change a decision's status only.
    did = dev.post(f"/api/projects/{pid}/decisions", json={"title": "Use Kafka"}).json()["id"]
    assert reviewer.patch(f"/api/projects/{pid}/decisions/{did}", json={"status": "accepted"}).status_code == 200
    assert reviewer.patch(f"/api/projects/{pid}/decisions/{did}", json={"title": "Changed"}).status_code == 403
    assert viewer.patch(f"/api/projects/{pid}/decisions/{did}", json={"status": "rejected"}).status_code == 403

    # Members, invites, tokens, AI settings: admin and above.
    for c in (viewer, reviewer, dev):
        assert c.post("/api/orgs/org-default/invites", json={"role": "viewer"}).status_code == 403
        assert c.get("/api/orgs/org-default/invites").status_code == 403
        assert c.get("/api/orgs/org-default/tokens").status_code == 403
        assert c.delete(f"/api/projects/{pid}").status_code == 403
    assert admin.get("/api/orgs/org-default/invites").status_code == 200
    viewer_id = viewer.get("/api/me").json()["user"]["id"]
    assert admin.patch(f"/api/orgs/org-default/members/{viewer_id}", json={"role": "developer"}).status_code == 200
    assert viewer.post(f"/api/projects/{pid}/ops", json=op).status_code == 200  # promoted
    # Org settings: owner only.
    assert admin.patch("/api/orgs/org-default", json={"name": "Renamed"}).status_code == 403
    assert owner.patch("/api/orgs/org-default", json={"name": "Renamed"}).json()["name"] == "Renamed"
    # Admins may delete projects.
    assert admin.delete(f"/api/projects/{pid}").status_code == 204


def test_owner_rules_and_last_owner_protection(app, owner):
    admin = member_client(app, owner, "admin", "admin@example.com")
    owner_id = owner.get("/api/me").json()["user"]["id"]
    admin_id = admin.get("/api/me").json()["user"]["id"]
    # Admins cannot touch owners or grant ownership.
    assert admin.patch(f"/api/orgs/org-default/members/{owner_id}", json={"role": "viewer"}).status_code == 403
    assert admin.patch(f"/api/orgs/org-default/members/{admin_id}", json={"role": "owner"}).status_code == 403
    assert admin.delete(f"/api/orgs/org-default/members/{owner_id}").status_code == 403
    assert admin.post("/api/orgs/org-default/invites", json={"role": "owner"}).status_code == 403
    # The last owner can neither be demoted nor removed (not even by themselves).
    assert owner.patch(f"/api/orgs/org-default/members/{owner_id}", json={"role": "admin"}).status_code == 409
    assert owner.delete(f"/api/orgs/org-default/members/{owner_id}").status_code == 409
    # With a second owner, the first may step down.
    assert owner.patch(f"/api/orgs/org-default/members/{admin_id}", json={"role": "owner"}).status_code == 200
    assert owner.patch(f"/api/orgs/org-default/members/{owner_id}", json={"role": "admin"}).status_code == 200
    # Anyone can leave.
    assert owner.delete(f"/api/orgs/org-default/members/{owner_id}").status_code == 204
    assert owner.get("/api/orgs/org-default/members").status_code == 404


def test_org_creation_and_switching(app, owner):
    other = authed_client(app, "o@example.com", "Olga")
    r = other.post("/api/orgs", json={"name": "Acme", "owner": "ignored"})
    assert r.status_code == 201 and r.json()["role"] == "owner"
    acme = r.json()["id"]
    assert {o["name"] for o in other.get("/api/orgs").json()} == {"Olga's workspace", "Acme"}
    assert acme not in {o["id"] for o in owner.get("/api/orgs").json()}
    pid = make_project(other, acme, "blank")
    assert owner.get(f"/api/projects/{pid}").status_code == 404
    assert owner.post("/api/projects", json={"name": "x", "org_id": acme}).status_code == 404


# ---------------------------------------------------------------- invites
def test_invites(app, owner):
    r = owner.post("/api/orgs/org-default/invites", json={"role": "reviewer", "email": "Rita@Example.com"})
    assert r.status_code == 201
    inv = r.json()
    assert inv["link"].endswith(f"/#/invite/{inv['token']}") and "token_hash" not in inv
    assert [i["id"] for i in owner.get("/api/orgs/org-default/invites").json()] == [inv["id"]]
    info = new_client(app).get(f"/api/invites/{inv['token']}").json()
    assert info["role"] == "reviewer" and info["org_name"] == "Default"
    # Bound to an email address.
    c = new_client(app)
    bad = c.post("/api/auth/signup", json={"email": "other@example.com", "name": "O", "password": PASSWORD,
                                           "invite": inv["token"]})
    assert bad.status_code == 403
    rita = authed_client(app, "rita@example.com", "Rita", invite=inv["token"])
    assert [(o["id"], o["role"]) for o in rita.get("/api/me").json()["orgs"]] == [("org-default", "reviewer")]
    # Single use.
    again = new_client(app).post("/api/auth/signup", json={"email": "rita2@example.com", "name": "R", "password": PASSWORD,
                                                           "invite": inv["token"]})
    assert again.status_code == 410
    # Accepting while signed in; revoking.
    inv2 = owner.post("/api/orgs/org-default/invites", json={"role": "developer"}).json()
    sam = authed_client(app, "sam@example.com", "Sam")
    r = sam.post(f"/api/invites/{inv2['token']}/accept")
    assert r.status_code == 200 and r.json()["role"] == "developer"
    inv3 = owner.post("/api/orgs/org-default/invites", json={"role": "developer"}).json()
    assert owner.delete(f"/api/orgs/org-default/invites/{inv3['id']}").status_code == 204
    assert sam.post(f"/api/invites/{inv3['token']}/accept").status_code == 410
    # Expired invites are refused.
    inv4 = owner.post("/api/orgs/org-default/invites", json={"role": "viewer"}).json()
    store = app.state.store
    with store.engine.begin() as conn:
        conn.execute(text("UPDATE invites SET expires_at=:t WHERE id=:id"), {"t": time.time() - 1, "id": inv4["id"]})
    assert new_client(app).get(f"/api/invites/{inv4['token']}").status_code == 404


# ---------------------------------------------------------------- CSRF / CORS
def test_csrf_rejects_cross_site_requests(app, owner):
    pid = make_project(owner)
    op = {"ops": [{"op": "add_node", "node": {"name": "X"}}]}
    evil = dict(owner.headers)
    evil["Origin"] = "https://evil.example"
    assert owner.post(f"/api/projects/{pid}/ops", json=op, headers={"Origin": "https://evil.example"}).status_code == 403
    # No Origin: Referer is used; neither -> refused when the session cookie is present.
    del owner.headers["Origin"]
    try:
        assert owner.post(f"/api/projects/{pid}/ops", json=op).status_code == 403
        assert owner.post(f"/api/projects/{pid}/ops", json=op,
                          headers={"Referer": "http://localhost:5173/#/p/x"}).status_code == 200
        assert owner.get(f"/api/projects/{pid}").status_code == 200  # safe methods are not checked
    finally:
        owner.headers["Origin"] = "http://localhost:5173"
    # Login CSRF: signup/login from another site are refused even without a cookie.
    c = new_client(app)
    r = c.post("/api/auth/login", json={"email": "owner@example.com", "password": PASSWORD},
               headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    # CORS: credentials allowed only for trusted origins.
    pre = c.options("/api/me", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert pre.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert pre.headers.get("access-control-allow-credentials") == "true"
    pre = c.options("/api/me", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"})
    assert pre.headers.get("access-control-allow-origin") is None


# ---------------------------------------------------------------- realtime
def test_ws_requires_session_and_membership(app, owner):
    pid = make_project(owner, template="blank")
    anon = new_client(app)
    with pytest.raises(WebSocketDisconnect):
        with anon.websocket_connect(f"/ws/{pid}/main") as ws:
            ws.receive_json()
    outsider = authed_client(app, "out@example.com", "Out")
    with pytest.raises(WebSocketDisconnect):
        with outsider.websocket_connect(f"/ws/{pid}/main", headers=ws_headers(outsider)) as ws:
            ws.receive_json()
    with owner.websocket_connect(f"/ws/{pid}/main", headers=ws_headers(owner)) as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["users"][0]["name"] == "Owner"


def test_collab_ticket_signing_and_verification(app, owner):
    pid = make_project(owner, template="blank")
    viewer = member_client(app, owner, "viewer", "v@example.com", "Vic")
    r = owner.get(f"/api/projects/{pid}/collab-ticket", params={"branch": "main", "color": "#30a46c"})
    assert r.status_code == 200
    t = r.json()
    claims = collab_sync.verify_ticket(t["ticket"], doc=f"{pid}:main")
    assert claims["name"] == "Owner" and claims["color"] == "#30a46c" and claims["ro"] is False
    assert claims["exp"] - time.time() <= collab_sync.TICKET_TTL + 1
    rv = viewer.get(f"/api/projects/{pid}/collab-ticket", params={"color": "javascript:alert(1)"}).json()
    vc = collab_sync.verify_ticket(rv["ticket"])
    assert vc["ro"] is True and rv["read_only"] is True and vc["color"] == "#888888"
    # Wrong document, tampering, expiry.
    with pytest.raises(ValueError):
        collab_sync.verify_ticket(t["ticket"], doc=f"{pid}:other")
    body, sig = t["ticket"].split(".")
    forged = collab_sync._b64url(collab_sync._unb64url(body).replace(b'"ro":false', b'"ro":true')) + "." + sig
    with pytest.raises(ValueError, match="signature"):
        collab_sync.verify_ticket(forged)
    with pytest.raises(ValueError, match="expired"):
        collab_sync.verify_ticket(t["ticket"], now=time.time() + collab_sync.TICKET_TTL + 5)
    old = collab_sync.sign_ticket({"doc": "d"}, ttl=1, now=time.time() - 100)
    with pytest.raises(ValueError, match="expired"):
        collab_sync.verify_ticket(old)
    # Requires read access; missing branch is a 404.
    outsider = authed_client(app, "out@example.com", "Out")
    assert outsider.get(f"/api/projects/{pid}/collab-ticket").status_code == 404
    assert owner.get(f"/api/projects/{pid}/collab-ticket", params={"branch": "nope"}).status_code == 404


def test_internal_state_endpoint(app, owner):
    pid = make_project(owner)
    c = new_client(app)
    assert c.get(f"/api/internal/state/{pid}:main").status_code == 401
    r = c.get(f"/api/internal/state/{pid}:main", headers={"x-internal-token": collab_sync.INTERNAL_TOKEN})
    assert r.status_code == 200 and r.json()["state"]["nodes"]


# ---------------------------------------------------------------- org AI keys (BYOK)
def test_secrets_box_round_trip(monkeypatch):
    monkeypatch.setenv("ARCHFLOW_SECRET_KEY", TEST_SECRET)
    ct = secrets_box.encrypt("sk-test-value-1234", "org-a:gemini")
    assert ct.startswith("v1:") and "sk-test" not in ct
    assert secrets_box.encrypt("sk-test-value-1234", "org-a:gemini") != ct  # random nonce
    assert secrets_box.decrypt(ct, "org-a:gemini") == "sk-test-value-1234"
    with pytest.raises(secrets_box.SecretDecryptError):
        secrets_box.decrypt(ct, "org-b:gemini")  # bound to its row
    monkeypatch.setenv("ARCHFLOW_SECRET_KEY", TEST_SECRET + "-rotated")
    with pytest.raises(secrets_box.SecretDecryptError):
        secrets_box.decrypt(ct, "org-a:gemini")
    monkeypatch.delenv("ARCHFLOW_SECRET_KEY")
    with pytest.raises(secrets_box.SecretsUnavailable):
        secrets_box.encrypt("x", "y")


def test_ai_settings_endpoints(app, owner, monkeypatch):
    dev = member_client(app, owner, "developer", "dev@example.com")
    monkeypatch.delenv("ARCHFLOW_SECRET_KEY", raising=False)  # independent of a developer's local .env
    r = owner.get("/api/orgs/org-default/ai-settings")  # readable without encryption; keys cannot be stored
    assert r.status_code == 200 and r.json()["enabled"] is False and len(r.json()["catalog"]) >= 10
    r = owner.put("/api/orgs/org-default/ai-settings", json={"gemini": "gm-test-key-abcd"})
    assert r.status_code == 503 and "ARCHFLOW_SECRET_KEY" in r.json()["detail"]
    monkeypatch.setenv("ARCHFLOW_SECRET_KEY", TEST_SECRET)
    assert dev.get("/api/orgs/org-default/ai-settings").status_code == 403
    assert dev.put("/api/orgs/org-default/ai-settings", json={"gemini": "x" * 20}).status_code == 403
    r = owner.put("/api/orgs/org-default/ai-settings", json={"gemini": "gm-test-key-abcd", "openai": "sk-test-key-wxyz"})
    assert r.status_code == 200
    p = r.json()["providers"]
    assert p["gemini"] == {"configured": True, "last4": "abcd", "updated_at": p["gemini"]["updated_at"]}
    assert p["openai"]["last4"] == "wxyz" and p["anthropic"]["configured"] is False
    assert "gm-test-key" not in r.text
    rows = app.state.store.list_ai_keys("org-default")
    assert all("test-key" not in row["ciphertext"] for row in rows)
    assert secrets_box.org_ai_keys(app.state.store, "org-default") == {"gemini": "gm-test-key-abcd",
                                                                       "openai": "sk-test-key-wxyz"}
    # Omitted = unchanged, "" = cleared.
    p = owner.put("/api/orgs/org-default/ai-settings", json={"openai": ""}).json()["providers"]
    assert p["openai"]["configured"] is False and p["gemini"]["configured"] is True
    assert owner.get("/api/me").json()["features"]["ai_settings"] is True


def test_llm_uses_org_keys(app, owner, monkeypatch):
    """With an org key configured, calls for that org use it (own router); others fall back to the env key."""
    monkeypatch.setenv("ARCHFLOW_SECRET_KEY", TEST_SECRET)
    monkeypatch.setenv("ARCHFLOW_AI", "auto")
    for k in ("GEMINI_API_KEYS", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "env-gemini-key")
    llm._reset()
    built = []

    class FakeRouter:
        def __init__(self, model_list, **kw):
            self.keys = [m["litellm_params"].get("api_key") for m in model_list]
            built.append(self)

        def completion(self, model, fallbacks, **kw):
            used.append(self.keys)
            msg = {"content": '{"ok": true}'}
            return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")])

    import litellm
    monkeypatch.setattr(litellm, "Router", FakeRouter)
    used: list = []
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}

    assert llm.complete_json("s", [{"role": "user", "content": "hi"}], schema, org_id="org-default") == {"ok": True}
    assert set(used[-1]) == {"env-gemini-key"}  # no org key yet
    owner.put("/api/orgs/org-default/ai-settings", json={"gemini": "org-gemini-key-1"})
    llm.complete_json("s", [{"role": "user", "content": "hi"}], schema, org_id="org-default")
    assert set(used[-1]) == {"org-gemini-key-1"}
    other = app.state.store.create_org("Other")["id"]
    llm.complete_json("s", [{"role": "user", "content": "hi"}], schema, org_id=other)
    assert set(used[-1]) == {"env-gemini-key"}
    # Changing the key rebuilds that org's router.
    owner.put("/api/orgs/org-default/ai-settings", json={"gemini": "org-gemini-key-2"})
    llm.complete_json("s", [{"role": "user", "content": "hi"}], schema, org_id="org-default")
    assert set(used[-1]) == {"org-gemini-key-2"}
    # An org with its own key is "available" even when the server has none.
    monkeypatch.delenv("GEMINI_API_KEY")
    assert llm.available("org-default") and not llm.available(other)
    llm._reset()


# ---------------------------------------------------------------- migration from display-name memberships
def test_sqlite_migrates_display_name_memberships(tmp_path):
    path = tmp_path / "old.db"
    s = Store(path)
    s.close()
    raw = sqlite3.connect(path)
    raw.executescript("""
        DROP TABLE alembic_version;  -- databases from before the ORM have no Alembic stamp
        DROP TABLE memberships;
        CREATE TABLE memberships (org_id TEXT NOT NULL, user_name TEXT NOT NULL, role TEXT NOT NULL,
                                  created_at TEXT NOT NULL, PRIMARY KEY (org_id, user_name));
        INSERT INTO memberships VALUES ('org-default', 'nk', 'member', '2025-01-01');
        INSERT INTO api_tokens (id, token_hash, kind, org_id, user_name, scopes, created_at)
            VALUES ('tok-old', 'h', 'pat', 'org-default', 'nk', '["archflow:read"]', '2025-01-01');
    """)
    raw.commit()
    raw.close()
    store = Store(path)
    cols = {c["name"] for c in inspect(store.engine).get_columns("memberships")}
    assert "user_id" in cols and "user_name" not in cols
    assert store.list_members(DEFAULT_ORG_ID) == []
    assert store.get_token_by_id("tok-old")["revoked"] is True  # ownerless legacy token no longer works
    store.close()
    Store(path).close()  # idempotent
