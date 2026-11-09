"""Two-step sign-in (TOTP, recovery codes, require-MFA) and OIDC single sign-on against a fake identity provider
(a locally generated RSA key served as JWKS; every HTTP request stays in-process)."""

import base64
import hashlib
import os
import secrets
import time
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import httpx  # noqa: E402
import jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from archflow import mfa, sso  # noqa: E402
from archflow.auth import hash_secret as auth_hash  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.store import DEFAULT_ORG_ID, Store  # noqa: E402

from authhelp import PASSWORD, authed_client, member_client, new_client  # noqa: E402

TEST_SECRET = "test-only-secret-key-not-for-production"
ISSUER = "https://idp.example.com"
CLIENT_ID = "archflow-test"
CLIENT_SECRET = "idp-client-secret-0123"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ARCHFLOW_SECRET_KEY", TEST_SECRET)
    monkeypatch.setenv("ARCHFLOW_ALLOW_PRIVATE_SSO_URLS", "true")  # no DNS lookups for idp.example.com
    mfa.limiter.reset()
    sso.clear_cache()
    yield
    mfa.limiter.reset()
    sso.clear_cache()
    sso._transport = None


@pytest.fixture()
def app():
    return create_app(Store(":memory:"))


@pytest.fixture()
def owner(app):
    return authed_client(app)


def make_project(client, org_id=DEFAULT_ORG_ID):
    r = client.post("/api/projects", json={"name": "Shop", "template": "blank", "org_id": org_id})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ---------------------------------------------------------------- TOTP
def test_totp_matches_rfc6238_vectors():
    secret = base64.b32encode(b"12345678901234567890").decode()
    # RFC 6238 appendix B (SHA-1), last 6 of the 8 digits
    assert mfa.totp(secret, 59 // 30) == "287082"
    assert mfa.totp(secret, 1111111109 // 30) == "081804"
    assert mfa.totp(secret, 2000000000 // 30) == "279037"
    step = mfa.match_step(secret, "287082", at=59)
    assert step == 1
    assert mfa.match_step(secret, "287082", at=59 + 30) == 1  # one step of clock drift
    assert mfa.match_step(secret, "287082", at=59 + 90) is None
    assert mfa.match_step(secret, "28708", at=59) is None and mfa.match_step(secret, "abcdef", at=59) is None
    assert mfa.otpauth_uri("JBSWY3DPEHPK3PXP", "a@b.co").startswith("otpauth://totp/ArchFlow%3Aa%40b.co?secret=JBSWY3DPEHPK3PXP")


def code_for(secret, offset=0):
    return mfa.totp(secret, mfa.current_step() + offset)


def enroll(client):
    r = client.post("/api/auth/mfa/setup")
    assert r.status_code == 200, r.text
    secret = r.json()["secret"]
    r = client.post("/api/auth/mfa/enable", json={"code": code_for(secret)})
    assert r.status_code == 200, r.text
    return secret, r.json()["recovery_codes"]


# ---------------------------------------------------------------- MFA enroll, sign in, recovery, disable
def test_mfa_enroll_login_recovery_and_disable(app, owner):
    assert owner.get("/api/auth/mfa").json()["enabled"] is False
    r = owner.post("/api/auth/mfa/setup")
    assert r.status_code == 200
    secret, uri = r.json()["secret"], r.json()["otpauth_uri"]
    assert uri.startswith("otpauth://totp/ArchFlow%3Aowner%40example.com?") and f"secret={secret}" in uri
    stored = app.state.authz.store.get_mfa(owner.get("/api/me").json()["user"]["id"])
    assert secret not in stored["ciphertext"] and stored["enabled"] is False  # encrypted at rest
    wrong = "000000" if code_for(secret) != "000000" else "111111"
    assert owner.post("/api/auth/mfa/enable", json={"code": wrong}).status_code == 400
    r = owner.post("/api/auth/mfa/enable", json={"code": code_for(secret)})
    assert r.status_code == 200
    codes = r.json()["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10 and r.json()["recovery_codes_left"] == 10
    stored = app.state.authz.store.get_mfa(stored["user_id"])
    assert not any(c in str(stored) for c in codes)  # only hashes are stored
    assert owner.get("/api/me").json()["security"]["mfa_enabled"] is True
    assert owner.post("/api/auth/mfa/setup").status_code == 409
    mfa.limiter.reset()

    # Password alone no longer signs in: a challenge token, then the code.
    c = new_client(app)
    r = c.post("/api/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["mfa_required"] is True and "user" not in r.json()
    token = r.json()["mfa_token"]
    assert c.get("/api/me").status_code == 401
    assert c.post("/api/auth/mfa/verify", json={"mfa_token": token, "code": wrong}).status_code == 401
    # The code used to enable is spent (same time step): replay refused.
    assert c.post("/api/auth/mfa/verify", json={"mfa_token": token, "code": code_for(secret)}).status_code == 401
    assert c.post("/api/auth/mfa/verify", json={"mfa_token": "afm_bogus", "code": code_for(secret, 1)}).status_code == 401
    r = c.post("/api/auth/mfa/verify", json={"mfa_token": token, "code": code_for(secret, 1)})
    assert r.status_code == 200 and r.json()["user"]["email"] == "owner@example.com"
    assert c.get("/api/me").status_code == 200
    # The challenge is single use.
    assert new_client(app).post("/api/auth/mfa/verify", json={"mfa_token": token, "code": codes[0]}).status_code == 401
    mfa.limiter.reset()

    # A recovery code works once.
    c2 = new_client(app)
    token = c2.post("/api/auth/login", json={"email": "owner@example.com", "password": PASSWORD}).json()["mfa_token"]
    assert c2.post("/api/auth/mfa/verify", json={"mfa_token": token, "code": codes[0].upper()}).status_code == 200
    c3 = new_client(app)
    token = c3.post("/api/auth/login", json={"email": "owner@example.com", "password": PASSWORD}).json()["mfa_token"]
    assert c3.post("/api/auth/mfa/verify", json={"mfa_token": token, "code": codes[0]}).status_code == 401
    assert owner.get("/api/auth/mfa").json()["recovery_codes_left"] == 9
    mfa.limiter.reset()

    # New recovery codes need a current code (not a recovery code); old ones stop working.
    assert owner.post("/api/auth/mfa/recovery-codes", json={"code": codes[1]}).status_code == 400
    new_codes = owner.post("/api/auth/mfa/recovery-codes", json={"code": code_for(secret, -1)})
    assert new_codes.status_code == 400  # older than the last used step: refused
    mfa.limiter.reset()

    # Turning it off needs a valid code.
    assert owner.post("/api/auth/mfa/disable", json={"code": wrong}).status_code == 400
    assert owner.post("/api/auth/mfa/disable", json={"code": codes[2]}).status_code == 200
    assert owner.get("/api/auth/mfa").json()["enabled"] is False
    r = new_client(app).post("/api/auth/login", json={"email": "owner@example.com", "password": PASSWORD})
    assert r.status_code == 200 and "user" in r.json()


def test_mfa_code_attempts_are_rate_limited(app, owner):
    secret, _ = enroll(owner)
    c = new_client(app)
    token = c.post("/api/auth/login", json={"email": "owner@example.com", "password": PASSWORD}).json()["mfa_token"]
    wrong = "000000" if code_for(secret, 1) != "000000" else "111111"
    for _ in range(5):
        assert c.post("/api/auth/mfa/verify", json={"mfa_token": token, "code": wrong}).status_code == 401
    r = c.post("/api/auth/mfa/verify", json={"mfa_token": token, "code": code_for(secret, 1)})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0  # even the right code waits
    mfa.limiter.reset()
    assert c.post("/api/auth/mfa/verify", json={"mfa_token": token, "code": code_for(secret, 1)}).status_code == 200


def test_mfa_needs_the_secret_key(app, owner, monkeypatch):
    monkeypatch.delenv("ARCHFLOW_SECRET_KEY")
    assert owner.post("/api/auth/mfa/setup").status_code == 503
    assert owner.post("/api/auth/mfa/enable", json={"code": "123456"}).status_code == 409


def test_oauth_consent_login_asks_for_the_code(app, owner):
    secret, _ = enroll(owner)
    c = new_client(app)
    r = c.post("/oauth/login", data={"email": "owner@example.com", "password": PASSWORD, "next": ""})
    assert r.status_code == 200 and 'name="mfa_token"' in r.text and "archflow_session" not in r.headers.get("set-cookie", "")
    token = r.text.split('name="mfa_token" value="')[1].split('"')[0]
    r = c.post("/oauth/login", data={"mfa_token": token, "code": "999999" if code_for(secret, 1) != "999999" else "888888"})
    assert r.status_code == 401
    r = c.post("/oauth/login", data={"mfa_token": token, "code": code_for(secret, 1)})
    assert r.status_code == 200 and "archflow_session" in r.headers.get("set-cookie", "")


# ---------------------------------------------------------------- require MFA for an org
def test_org_can_require_mfa(app, owner):
    pid = make_project(owner)
    dev = member_client(app, owner, "developer", "dev@example.com", "Dev")
    assert dev.put("/api/orgs/org-default/auth-settings/mfa", json={"require_mfa": True}).status_code == 403
    # An admin must have MFA on before requiring it (no locking yourself out).
    assert owner.put("/api/orgs/org-default/auth-settings/mfa", json={"require_mfa": True}).status_code == 409
    enroll(owner)
    r = owner.put("/api/orgs/org-default/auth-settings/mfa", json={"require_mfa": True})
    assert r.status_code == 200 and r.json()["require_mfa"] is True

    me = dev.get("/api/me").json()
    assert me["security"]["mfa_required"] is True and me["security"]["mfa_enabled"] is False
    r = dev.get(f"/api/projects/{pid}")
    assert r.status_code == 403 and "two-step" in r.json()["detail"]
    assert dev.get("/api/orgs/org-default/members").status_code == 403
    assert dev.get("/api/projects").json() == []  # projects of blocked orgs are not listed
    assert owner.get(f"/api/projects/{pid}").status_code == 200

    secret, _ = enroll(dev)  # the MFA routes stay open so that the member can enroll
    assert dev.get(f"/api/projects/{pid}").status_code == 200
    assert len(dev.get("/api/projects").json()) == 1
    r = dev.post("/api/auth/mfa/disable", json={"code": code_for(secret, 1)})
    assert r.status_code == 409 and "requires" in r.json()["detail"]


# ---------------------------------------------------------------- OIDC single sign-on
class FakeIdP:
    """A minimal OpenID provider: discovery, JWKS, and a token endpoint that checks PKCE and client auth."""

    def __init__(self, issuer=ISSUER):
        self.issuer = issuer
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "key-1"
        self.codes: dict[str, dict] = {}
        self.token_requests: list[dict] = []

    def jwk(self, key=None, kid=None):
        d = jwt.algorithms.RSAAlgorithm.to_jwk((key or self.key).public_key(), as_dict=True)
        return {**d, "kid": kid or self.kid, "use": "sig", "alg": "RS256"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{self.issuer}/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": self.issuer, "authorization_endpoint": f"{self.issuer}/authorize",
                "token_endpoint": f"{self.issuer}/token", "jwks_uri": f"{self.issuer}/jwks",
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"]})
        if url == f"{self.issuer}/jwks":
            return httpx.Response(200, json={"keys": [self.jwk()]})
        if url == f"{self.issuer}/token" and request.method == "POST":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append({"form": form, "auth": request.headers.get("authorization")})
            expected_auth = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if request.headers.get("authorization") != expected_auth:
                return httpx.Response(401, json={"error": "invalid_client"})
            grant = self.codes.pop(form.get("code", ""), None)
            if not grant:
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).decode().rstrip("=")
            if challenge != grant["code_challenge"] or form.get("redirect_uri") != grant["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer", "id_token": grant["id_token"]})
        return httpx.Response(404)

    def authorize(self, auth_url: str, who: str, sign_key=None, kid=None, **overrides) -> tuple[str, str]:
        """What the provider does after the user signs in: returns (state, code)."""
        q = {k: v[0] for k, v in parse_qs(urlsplit(auth_url).query).items()}
        assert auth_url.startswith(f"{self.issuer}/authorize?")
        assert q["response_type"] == "code" and q["client_id"] == CLIENT_ID and q["code_challenge_method"] == "S256"
        assert "openid" in q["scope"].split()
        now = int(time.time())
        claims = {"iss": self.issuer, "aud": CLIENT_ID, "sub": f"sub-{who}", "email": who, "email_verified": True,
                  "name": who.split("@")[0].title(), "iat": now, "exp": now + 300, "nonce": q["nonce"]}
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        id_token = jwt.encode(claims, sign_key or self.key, algorithm="RS256", headers={"kid": kid or self.kid})
        code = secrets.token_urlsafe(16)
        self.codes[code] = {"code_challenge": q["code_challenge"], "redirect_uri": q["redirect_uri"], "id_token": id_token}
        return q["state"], code


@pytest.fixture()
def idp():
    fake = FakeIdP()
    sso._transport = httpx.MockTransport(fake.handler)
    return fake


def configure_sso(owner, org_id=DEFAULT_ORG_ID, **extra):
    body = {"enabled": True, "issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
            "domains": ["acme.com"], "default_role": "reviewer", **extra}
    r = owner.put(f"/api/orgs/{org_id}/auth-settings/sso", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def sso_sign_in(app, idp, who, client=None, **claims):
    """Run the browser side of the flow. Returns (client, the final redirect response)."""
    c = client or new_client(app)
    r = c.post("/api/auth/sso/discover", json={"email": who})
    assert r.status_code == 200, r.text
    r = c.get(r.json()["start_url"])
    assert r.status_code == 302, r.text
    state, code = idp.authorize(r.headers["location"], who, **claims)
    return c, c.get("/api/auth/sso/callback", params={"state": state, "code": code})


def sso_error(resp) -> str:
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("http://localhost:5173/#/login?"), loc
    return parse_qs(loc.split("?", 1)[1]).get("sso_error", [""])[0]


def test_sso_settings_are_admin_only_and_the_secret_is_write_only(app, owner, idp, monkeypatch):
    dev = member_client(app, owner, "developer", "dev@example.com")
    assert dev.get("/api/orgs/org-default/auth-settings").status_code == 403
    assert dev.put("/api/orgs/org-default/auth-settings/sso", json={"issuer": ISSUER}).status_code == 403
    s = configure_sso(owner)
    assert s["sso"]["secret_set"] and s["sso"]["secret_last4"] == "0123" and CLIENT_SECRET not in str(s)
    assert s["callback_url"] == "http://localhost:5173/api/auth/sso/callback"
    got = owner.get("/api/orgs/org-default/auth-settings").json()
    assert got["sso"]["domains"] == ["acme.com"] and CLIENT_SECRET not in str(got)
    row = app.state.authz.store.get_auth_settings(DEFAULT_ORG_ID)
    assert CLIENT_SECRET not in row["ciphertext"]
    # Keeping the stored secret: omit it.
    r = owner.put("/api/orgs/org-default/auth-settings/sso", json={
        "issuer": ISSUER, "client_id": CLIENT_ID, "domains": ["acme.com", "@Acme.io"], "default_role": "viewer"})
    assert r.status_code == 200 and r.json()["sso"]["domains"] == ["acme.com", "acme.io"]
    assert app.state.authz.store.get_auth_settings(DEFAULT_ORG_ID)["ciphertext"] == row["ciphertext"]
    assert owner.post("/api/orgs/org-default/auth-settings/sso/test").json()["ok"] is True
    # Validation
    bad = {"issuer": ISSUER, "client_id": CLIENT_ID, "domains": ["acme.com"]}
    assert owner.put("/api/orgs/org-default/auth-settings/sso", json={**bad, "domains": ["not a domain"]}).status_code == 422
    assert owner.put("/api/orgs/org-default/auth-settings/sso", json={**bad, "client_id": ""}).status_code == 422
    assert owner.put("/api/orgs/org-default/auth-settings/sso", json={**bad, "default_role": "owner"}).status_code == 422
    monkeypatch.delenv("ARCHFLOW_ALLOW_PRIVATE_SSO_URLS")
    for issuer in ("http://idp.example.com", "https://localhost", "https://user:pw@idp.example.com"):
        assert owner.put("/api/orgs/org-default/auth-settings/sso", json={**bad, "issuer": issuer}).status_code == 422
    monkeypatch.setenv("ARCHFLOW_ALLOW_PRIVATE_SSO_URLS", "true")
    # A domain belongs to one org only.
    other = owner.post("/api/orgs", json={"name": "Other"}).json()["id"]
    r = owner.put(f"/api/orgs/{other}/auth-settings/sso", json={**bad, "client_secret": CLIENT_SECRET})
    assert r.status_code == 409
    # Turning it off
    assert owner.delete("/api/orgs/org-default/auth-settings/sso").status_code == 204
    assert new_client(app).post("/api/auth/sso/discover", json={"email": "a@acme.com"}).status_code == 404


def test_sso_sign_in_provisions_the_user_just_in_time(app, owner, idp):
    configure_sso(owner)
    assert new_client(app).post("/api/auth/sso/discover", json={"email": "x@other.com"}).status_code == 404
    c = new_client(app)
    r = c.post("/api/auth/sso/discover", json={"email": "Jane@Acme.com"})
    assert r.json()["org_id"] == DEFAULT_ORG_ID
    start = c.get(r.json()["start_url"])
    assert start.status_code == 302 and "archflow_sso=" in start.headers["set-cookie"]
    q = parse_qs(urlsplit(start.headers["location"]).query)
    assert q["login_hint"] == ["jane@acme.com"] and q["redirect_uri"] == ["http://localhost:5173/api/auth/sso/callback"]
    state, code = idp.authorize(start.headers["location"], "jane@acme.com")
    r = c.get("/api/auth/sso/callback", params={"state": state, "code": code})
    assert r.status_code == 303 and r.headers["location"] == "http://localhost:5173/#/"
    me = c.get("/api/me").json()
    assert me["user"]["email"] == "jane@acme.com" and me["user"]["name"] == "Jane"
    assert me["orgs"][0]["id"] == DEFAULT_ORG_ID and me["orgs"][0]["role"] == "reviewer"  # the default role
    assert me["security"]["auth_method"] == f"sso:{DEFAULT_ORG_ID}"
    sent = idp.token_requests[-1]["form"]
    assert sent["grant_type"] == "authorization_code" and len(sent["code_verifier"]) >= 43
    # The provisioned account has no password.
    assert new_client(app).post("/api/auth/login", json={"email": "jane@acme.com", "password": "sso-only"}).status_code == 401
    # The state is single use.
    assert "expired" in sso_error(c.get("/api/auth/sso/callback", params={"state": state, "code": code}))
    # A second sign-in reuses the account and keeps the role an admin gave since.
    uid = me["user"]["id"]
    owner.patch(f"/api/orgs/org-default/members/{uid}", json={"role": "developer"})
    c2, r = sso_sign_in(app, idp, "jane@acme.com")
    assert r.headers["location"] == "http://localhost:5173/#/"
    me2 = c2.get("/api/me").json()
    assert me2["user"]["id"] == uid and me2["orgs"][0]["role"] == "developer"


def test_sso_takes_over_a_pre_registered_password_account(app, owner, idp):
    """Sign-up does not verify emails: someone may register jane@acme.com first. When Acme's IdP proves who owns the
    address, the squatter's password, sessions and tokens must stop working before the account joins the org."""
    configure_sso(owner)
    squatter = new_client(app)
    r = squatter.post("/api/auth/signup", json={"email": "jane@acme.com", "name": "Jane", "password": PASSWORD})
    assert r.status_code == 201, r.text
    own_org = r.json()["orgs"][0]["id"]
    pat = squatter.post(f"/api/orgs/{own_org}/tokens", json={"name": "cli", "access": "read"})
    assert pat.status_code == 201, pat.text
    store = app.state.authz.store
    assert store.get_token(auth_hash(pat.json()["token"])) is not None

    jane, r = sso_sign_in(app, idp, "jane@acme.com")
    assert r.headers["location"] == "http://localhost:5173/#/"
    me = jane.get("/api/me").json()
    assert DEFAULT_ORG_ID in {o["id"] for o in me["orgs"]}
    assert squatter.get("/api/me").status_code == 401  # the old session ended
    assert new_client(app).post("/api/auth/login",
                                json={"email": "jane@acme.com", "password": PASSWORD}).status_code == 401
    assert store.get_token(auth_hash(pat.json()["token"])) is None  # tokens revoked

    # Later SSO sign-ins (already a member) leave the new session alone.
    _, r = sso_sign_in(app, idp, "jane@acme.com")
    assert jane.get("/api/me").status_code == 200


@pytest.mark.parametrize("claims, message", [
    ({"nonce": "not-the-nonce"}, "nonce"),
    ({"exp": int(time.time()) - 600, "iat": int(time.time()) - 900}, "expired"),
    ({"aud": "some-other-app"}, "audience"),
    ({"aud": [CLIENT_ID, "other"], "azp": "other"}, "authorized party"),
    ({"iss": "https://evil.example.com"}, "issuer"),
    ({"email_verified": False}, "not verified"),
    ({"email": "jane@evil.com"}, "domain"),
    ({"exp": None}, "not valid"),
])
def test_sso_rejects_bad_id_tokens(app, owner, idp, claims, message):
    configure_sso(owner)
    c, r = sso_sign_in(app, idp, "jane@acme.com", **claims)
    assert message in sso_error(r)
    assert c.get("/api/me").status_code == 401
    assert app.state.authz.store.get_user_by_email("jane@acme.com") is None  # nothing provisioned


def test_sso_rejects_forged_signatures_and_unbound_callbacks(app, owner, idp):
    configure_sso(owner)
    attacker = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    c, r = sso_sign_in(app, idp, "jane@acme.com", sign_key=attacker)  # right kid, wrong key
    assert "signature" in sso_error(r)
    c, r = sso_sign_in(app, idp, "jane@acme.com", sign_key=attacker, kid="unknown")
    assert "key" in sso_error(r)
    # An HMAC token signed with the public key (algorithm confusion) is refused outright.
    c = new_client(app)
    start = c.get(c.post("/api/auth/sso/discover", json={"email": "jane@acme.com"}).json()["start_url"])
    state, code = idp.authorize(start.headers["location"], "jane@acme.com")
    idp.codes[code]["id_token"] = jwt.encode({"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "email": "jane@acme.com",
                                              "iat": int(time.time()), "exp": int(time.time()) + 300}, "k" * 32,
                                             algorithm="HS256")
    assert "algorithm" in sso_error(c.get("/api/auth/sso/callback", params={"state": state, "code": code}))
    # A callback without the browser binding cookie (e.g. a link sent to a victim) is refused.
    c = new_client(app)
    start = c.get(c.post("/api/auth/sso/discover", json={"email": "jane@acme.com"}).json()["start_url"])
    state, code = idp.authorize(start.headers["location"], "jane@acme.com")
    victim = new_client(app)
    assert "another browser" in sso_error(victim.get("/api/auth/sso/callback", params={"state": state, "code": code}))
    # Provider errors and unknown states
    assert "did not sign you in" in sso_error(c.get("/api/auth/sso/callback", params={"error": "access_denied"}))
    assert "expired" in sso_error(c.get("/api/auth/sso/callback", params={"state": "nope", "code": "x"}))
    assert app.state.authz.store.get_user_by_email("jane@acme.com") is None


def test_sso_with_mfa_asks_for_the_code(app, owner, idp):
    configure_sso(owner)
    c, _ = sso_sign_in(app, idp, "jane@acme.com")
    secret, _ = enroll(c)
    c2, r = sso_sign_in(app, idp, "jane@acme.com")
    loc = r.headers["location"]
    assert loc.startswith("http://localhost:5173/#/login?mfa=")
    assert c2.get("/api/me").status_code == 401
    token = parse_qs(loc.split("?", 1)[1])["mfa"][0]
    r = c2.post("/api/auth/mfa/verify", json={"mfa_token": token, "code": code_for(secret, 1)})
    assert r.status_code == 200 and r.json()["security"]["auth_method"] == f"sso:{DEFAULT_ORG_ID}"


def test_org_can_require_sso(app, owner, idp):
    pid = make_project(owner)
    admin = member_client(app, owner, "admin", "admin@acme.com")
    dev = member_client(app, owner, "developer", "dev@acme.com")
    configure_sso(owner)
    # An admin must have signed in through SSO before requiring it.
    body = {"issuer": ISSUER, "client_id": CLIENT_ID, "domains": ["acme.com"], "require_sso": True}
    assert admin.put("/api/orgs/org-default/auth-settings/sso", json=body).status_code == 409
    assert owner.put("/api/orgs/org-default/auth-settings/sso", json=body).status_code == 200  # owners may

    r = dev.get(f"/api/projects/{pid}")  # a password session
    assert r.status_code == 403 and "single sign-on" in r.json()["detail"]
    assert dev.get("/api/me").json()["security"]["sso_required_orgs"] == [DEFAULT_ORG_ID]
    assert owner.get(f"/api/projects/{pid}").status_code == 200  # owners are exempt (break-glass)
    sso_dev, _ = sso_sign_in(app, idp, "dev@acme.com")
    assert sso_dev.get(f"/api/projects/{pid}").status_code == 200
    assert sso_dev.get("/api/me").json()["security"]["sso_required_orgs"] == []
    # Signing in through SSO of another org does not count.
    other = owner.post("/api/orgs", json={"name": "Other"}).json()["id"]
    configure_sso(owner, other, domains=["other.io"])
    assert sso_dev.get(f"/api/orgs/{other}/members").status_code == 404  # not a member there
    # Leaving is still allowed.
    assert dev.delete(f"/api/orgs/org-default/members/{dev.get('/api/me').json()['user']['id']}").status_code == 204


def test_sign_in_rule_impact_lists_blocked_members_and_tokens(app, owner):
    dev = member_client(app, owner, "developer", "impact-dev@example.com")
    owner.post("/api/orgs/org-default/tokens", json={"name": "ci", "access": "read"})
    assert dev.get("/api/orgs/org-default/auth-settings/impact").status_code == 403
    i = owner.get("/api/orgs/org-default/auth-settings/impact", params={"require_mfa": "true"}).json()
    assert len(i["members_without_mfa"]) >= 2 and any(t["name"] == "ci" for t in i["tokens_blocked"])
    # the current rules block nothing, so nothing is revoked
    assert owner.get("/api/orgs/org-default/auth-settings/impact").json()["tokens_blocked"] == []
    assert owner.post("/api/orgs/org-default/auth-settings/revoke-blocked-tokens").json() == {"revoked": 0}
