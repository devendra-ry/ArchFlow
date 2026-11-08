"""Single sign-on with OpenID Connect, per organization.

Admins set the identity provider's issuer URL, a client ID and secret (the secret is encrypted with ``secrets_box``
and never returned, only its last 4 characters), the email domains that sign in this way, the role new people get,
and optionally "require SSO". The redirect (callback) URL to register at the provider is shown in the settings:
``<web app>/api/auth/sso/callback`` (or ``ARCHFLOW_SSO_CALLBACK_URL``).

Flow (authorization code with PKCE):

1. ``POST /api/auth/sso/discover`` with a work email finds the org whose SSO domains include the email's domain.
2. ``GET /api/auth/sso/{org_id}/start`` reads the provider's ``/.well-known/openid-configuration``, stores a
   random ``state`` with a ``nonce`` and the PKCE verifier (10 minutes, single use; only its hash is the key), sets a
   short-lived HttpOnly cookie that binds the attempt to this browser, and redirects to the provider.
3. ``GET /api/auth/sso/callback`` checks state and the browser binding, exchanges the code (with the PKCE verifier
   and the client secret) at the token endpoint, and validates the ID token: signature against the provider's JWKS
   (asymmetric algorithms only), ``iss``, ``aud`` (and ``azp`` when there are several audiences), ``exp``/``iat``
   (60 s leeway) and the ``nonce``. The email must be verified (``email_verified`` not false) and in an allowed
   domain.
4. Just-in-time provisioning: an unknown email gets an account (no password: it signs in through SSO only) and
   joins the org with the default role; a known account that is not a member joins the same way. Then, if the user
   has MFA on, the web app asks for the code; otherwise the session starts. The session remembers that it signed in
   through this org's SSO, which "require SSO" checks (authz.py; owners are exempt as a break-glass path).

Provider URLs must be public https addresses unless ``ARCHFLOW_ALLOW_PRIVATE_SSO_URLS=true`` (then also http, for
an identity provider on your own network). Requests never follow redirects. SAML is not supported.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import threading
import time
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from . import jira, mfa, secrets_box, users
from .auth import hash_secret
from .authz import ROLES, Access

log = logging.getLogger("archflow.sso")

STATE_TTL = 600
CACHE_TTL = 3600
LEEWAY = 60
SSO_COOKIE = "archflow_sso"
COOKIE_PATH = "/api/auth/sso"
NO_PASSWORD = "sso-only"  # not a valid password hash: users.verify_password always fails for it
ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA")
DEFAULT_ROLES = tuple(r for r in ROLES if r != "owner")
DOMAIN_RE = re.compile(r"^(?=.{3,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

# Tests replace this to keep every request in-process.
_transport: httpx.BaseTransport | None = None
_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()


class SSOError(RuntimeError):
    """Sign-in through the identity provider failed (the message is safe to show)."""


def aad(org_id: str) -> str:
    return f"org-sso:{org_id}"


def callback_url() -> str:
    return os.environ.get("ARCHFLOW_SSO_CALLBACK_URL", "").strip() or f"{users.frontend_url()}/api/auth/sso/callback"


def allow_private() -> bool:
    return os.environ.get("ARCHFLOW_ALLOW_PRIVATE_SSO_URLS", "").strip().lower() in ("1", "true", "yes")


def check_url(url: str, what: str = "issuer URL") -> str:
    """A provider URL: https (http only with ARCHFLOW_ALLOW_PRIVATE_SSO_URLS), no credentials, public host.
    Returns it without a trailing slash. Raises ValueError."""
    url = (url or "").strip()
    if not url or len(url) > 500:
        raise ValueError(f"Enter the {what}.")
    parts = urlsplit(url)
    private = allow_private()
    if parts.scheme != "https" and not (private and parts.scheme == "http"):
        raise ValueError(f"The {what} must start with https://.")
    if parts.username or parts.password or "@" in parts.netloc:
        raise ValueError(f"Do not put credentials in the {what}.")
    if parts.fragment or not parts.hostname:
        raise ValueError(f"The {what} is not valid.")
    if not private:
        try:
            jira.check_site_url(f"https://{parts.netloc}")  # public addresses only (SSRF guard)
        except ValueError as exc:
            raise ValueError(f"The {what} must be a public https address (set ARCHFLOW_ALLOW_PRIVATE_SSO_URLS=true "
                             "for an identity provider on your own network).") from exc
    return url.rstrip("/")


def clean_domains(raw: list[str]) -> list[str]:
    out: list[str] = []
    for d in raw:
        d = (d or "").strip().lower().lstrip("@")
        if not d:
            continue
        if not DOMAIN_RE.match(d):
            raise ValueError(f"'{d}' is not an email domain (example: acme.com).")
        if d not in out:
            out.append(d)
    return out


def email_domain(email: str) -> str:
    return users.normalize_email(email).rpartition("@")[2]


# --------------------------------------------------------------------------- HTTP and caches
def _client() -> httpx.Client:
    return httpx.Client(transport=_transport, follow_redirects=False, timeout=10.0,
                        headers={"Accept": "application/json", "User-Agent": "ArchFlow-SSO"})


def _get_json(url: str, what: str) -> dict[str, Any]:
    try:
        with _client() as c:
            r = c.get(url)
    except httpx.HTTPError as exc:
        raise SSOError(f"The identity provider's {what} did not answer ({type(exc).__name__}).") from exc
    if r.status_code != 200:
        raise SSOError(f"The identity provider's {what} answered HTTP {r.status_code}.")
    try:
        data = r.json()
    except ValueError as exc:
        raise SSOError(f"The identity provider's {what} is not JSON.") from exc
    if not isinstance(data, dict):
        raise SSOError(f"The identity provider's {what} is not valid.")
    return data


def _cached(key: str, load, refresh: bool = False) -> dict[str, Any]:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and not refresh and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = load()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


def discovery(issuer: str, refresh: bool = False) -> dict[str, Any]:
    """The provider's OpenID configuration (cached for an hour), checked against the configured issuer."""
    def load() -> dict[str, Any]:
        doc = _get_json(f"{issuer.rstrip('/')}/.well-known/openid-configuration", "discovery document")
        if str(doc.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
            raise SSOError("The identity provider's discovery document is for a different issuer.")
        for field in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            try:
                check_url(str(doc.get(field) or ""), field.replace("_", " "))
            except ValueError as exc:
                raise SSOError(f"The identity provider's {field.replace('_', ' ')} is missing or not allowed.") from exc
        return doc
    return _cached(f"disc:{issuer}", load, refresh)


def _signing_key(jwks_uri: str, kid: str | None, alg: str) -> Any:
    for refresh in (False, True):  # unknown kid: the provider may have rotated keys, so fetch once more
        keys = _cached(f"jwks:{jwks_uri}", lambda: _get_json(jwks_uri, "key set"), refresh).get("keys") or []
        for k in keys:
            if (not isinstance(k, dict) or k.get("use", "sig") != "sig" or (kid and k.get("kid") != kid)
                    or k.get("alg", alg) != alg):
                continue
            try:
                return jwt.PyJWK(k, algorithm=alg).key
            except jwt.PyJWTError:
                continue
    raise SSOError("The ID token is signed with a key the identity provider does not publish.")


# --------------------------------------------------------------------------- ID token
def validate_id_token(id_token: str, *, doc: dict[str, Any], client_id: str, nonce: str) -> dict[str, Any]:
    """Verify signature (JWKS), iss, aud/azp, exp/iat and nonce. Returns the claims. Raises SSOError."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise SSOError("The identity provider sent an unreadable ID token.") from exc
    alg = header.get("alg")
    if alg not in ALGORITHMS:
        raise SSOError("The ID token uses an unsupported signing algorithm.")
    key = _signing_key(doc["jwks_uri"], header.get("kid"), alg)
    try:
        claims = jwt.decode(id_token, key=key, algorithms=[alg], audience=client_id, issuer=doc["issuer"],
                            leeway=LEEWAY, options={"require": ["exp", "iat", "iss", "aud", "sub"]})
    except jwt.ExpiredSignatureError as exc:
        raise SSOError("The sign-in token has expired. Try again.") from exc
    except jwt.InvalidAudienceError as exc:
        raise SSOError("The ID token was issued for a different app (audience).") from exc
    except jwt.InvalidIssuerError as exc:
        raise SSOError("The ID token was issued by a different identity provider (issuer).") from exc
    except jwt.InvalidSignatureError as exc:
        raise SSOError("The ID token signature is not valid.") from exc
    except jwt.PyJWTError as exc:
        raise SSOError(f"The ID token is not valid ({type(exc).__name__}).") from exc
    aud = claims.get("aud")
    if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != client_id:
        raise SSOError("The ID token was issued for a different app (authorized party).")
    if not nonce or not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        raise SSOError("The sign-in response does not match this sign-in attempt (nonce).")
    return claims


# --------------------------------------------------------------------------- the flow
def find_org(store, email: str) -> dict[str, Any] | None:
    domain = email_domain(email)
    if not domain:
        return None
    for s in store.list_sso_settings():
        if domain in (s["domains"] or []):
            return s
    return None


def _settings(store, org_id: str) -> dict[str, Any]:
    s = store.get_auth_settings(org_id)
    if not s or not s["sso_enabled"] or not s["issuer"] or not s["client_id"]:
        raise SSOError("Single sign-on is not set up for this organization.")
    return s


def _pkce(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def begin(store, org_id: str, login_hint: str = "") -> tuple[str, str]:
    """Start a sign-in: returns (the provider's authorization URL, the browser-binding cookie value)."""
    s = _settings(store, org_id)
    doc = discovery(s["issuer"])
    state, nonce, verifier, binding = (secrets.token_urlsafe(32) for _ in range(4))
    store.save_auth_code(hash_secret(f"sso:{state}"), {"org_id": org_id, "nonce": nonce, "verifier": verifier,
                                                        "binding": hash_secret(binding)}, time.time() + STATE_TTL)
    params = {"response_type": "code", "client_id": s["client_id"], "redirect_uri": callback_url(),
              "scope": "openid email profile", "state": state, "nonce": nonce,
              "code_challenge": _pkce(verifier), "code_challenge_method": "S256"}
    if login_hint and users.valid_email(users.normalize_email(login_hint)):
        params["login_hint"] = users.normalize_email(login_hint)
    endpoint = doc["authorization_endpoint"]
    return f"{endpoint}{'&' if '?' in endpoint else '?'}{urlencode(params)}", binding


def _exchange(doc: dict[str, Any], client_id: str, client_secret: str, code: str, verifier: str) -> dict[str, Any]:
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": callback_url(), "code_verifier": verifier}
    headers: dict[str, str] = {}
    methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if "client_secret_basic" in methods or "client_secret_post" not in methods:
        raw = f"{quote(client_id, safe='')}:{quote(client_secret, safe='')}"  # RFC 6749 2.3.1: form-encode first
        headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
    else:
        data.update(client_id=client_id, client_secret=client_secret)
    try:
        with _client() as c:
            r = c.post(doc["token_endpoint"], data=data, headers=headers)
    except httpx.HTTPError as exc:
        raise SSOError(f"The identity provider's token endpoint did not answer ({type(exc).__name__}).") from exc
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or not isinstance(body, dict):
        err = body.get("error") if isinstance(body, dict) else None
        raise SSOError(f"The identity provider refused the sign-in code ({err or f'HTTP {r.status_code}'}).")
    if not isinstance(body.get("id_token"), str):
        raise SSOError("The identity provider did not return an ID token.")
    return body


def provision(store, org_id: str, settings: dict[str, Any], claims: dict[str, Any]) -> dict[str, Any]:
    """Just-in-time: the account for the token's email (created if new) and membership with the default role."""
    email = users.normalize_email(str(claims.get("email") or ""))
    if not users.valid_email(email):
        raise SSOError("The identity provider did not share a valid email address. Ask it to include the email scope.")
    if claims.get("email_verified") is False:
        raise SSOError("Your email address is not verified at the identity provider.")
    if email_domain(email) not in (settings["domains"] or []):
        raise SSOError(f"{email} is not in an email domain this organization allows for single sign-on.")
    user = store.get_user_by_email(email)
    if not user:
        name = str(claims.get("name") or claims.get("preferred_username") or email.split("@")[0]).strip()[:80]
        try:
            user = store.create_user(email, name or email.split("@")[0], NO_PASSWORD)
        except Exception:  # noqa: BLE001 - lost a race with another sign-in for the same email
            user = store.get_user_by_email(email)
            if not user:
                raise
    if store.get_role(org_id, user["id"]) is None:
        store.add_member(org_id, user["id"], settings["default_role"] if settings["default_role"] in DEFAULT_ROLES
                         else "viewer")
    return user


def finish(store, state: str, code: str, binding: str | None) -> tuple[dict[str, Any], str]:
    """Complete a sign-in from the callback: returns (user, org_id). Raises SSOError."""
    req = store.pop_auth_code(hash_secret(f"sso:{state}")) if state and len(state) < 200 else None
    if not req or "verifier" not in req:
        raise SSOError("This sign-in attempt has expired or was already used. Try again.")
    if not binding or not hmac.compare_digest(req["binding"], hash_secret(binding)):
        raise SSOError("This sign-in was started in another browser or tab. Start again from the sign-in page.")
    if not code:
        raise SSOError("The identity provider did not return a sign-in code.")
    org_id = req["org_id"]
    s = _settings(store, org_id)
    try:
        client_secret = secrets_box.decrypt(s["ciphertext"], aad(org_id))
    except (secrets_box.SecretDecryptError, secrets_box.SecretsUnavailable) as exc:
        raise SSOError("The SSO client secret cannot be read. An admin needs to enter it again.") from exc
    doc = discovery(s["issuer"])
    tokens = _exchange(doc, s["client_id"], client_secret, code, req["verifier"])
    claims = validate_id_token(tokens["id_token"], doc=doc, client_id=s["client_id"], nonce=req["nonce"])
    return provision(store, org_id, s, claims), org_id


# --------------------------------------------------------------------------- settings payload
def settings_payload(store, org_id: str) -> dict[str, Any]:
    s = store.get_auth_settings(org_id) or {}
    return {"sso": {"enabled": bool(s.get("sso_enabled")), "issuer": s.get("issuer") or "",
                    "client_id": s.get("client_id") or "", "secret_set": bool(s.get("ciphertext")),
                    "secret_last4": s.get("last4") or None, "domains": s.get("domains") or [],
                    "default_role": s.get("default_role") or "developer", "require_sso": bool(s.get("require_sso"))},
            "require_mfa": bool(s.get("require_mfa")), "callback_url": callback_url(),
            "secrets_enabled": secrets_box.enabled(), "updated_at": s.get("updated_at")}


# --------------------------------------------------------------------------- routes
class DiscoverIn(BaseModel):
    email: str = Field(max_length=320)


class SSOIn(BaseModel):
    enabled: bool = True
    issuer: str = Field("", max_length=500)
    client_id: str = Field("", max_length=300)
    client_secret: str | None = Field(None, max_length=2000)  # None or "" keeps the stored secret
    domains: list[str] = Field(default_factory=list, max_length=50)
    default_role: str = Field("developer", pattern="^(" + "|".join(DEFAULT_ROLES) + ")$")
    require_sso: bool = False


class MfaPolicyIn(BaseModel):
    require_mfa: bool


def _login_redirect(**params: str) -> RedirectResponse:
    return RedirectResponse(f"{users.frontend_url()}/#/login?{urlencode(params)}", status_code=303)


def register_routes(app: FastAPI, store: Any, authz: Any) -> None:
    from . import accounts  # accounts imports mfa; keep imports one-way at module load

    org_admin = authz.org("admin")

    # ---- admin settings ----
    @app.get("/api/orgs/{org_id}/auth-settings")
    def get_auth_settings(a: Access = Depends(org_admin)):
        return settings_payload(store, a.org_id)

    @app.put("/api/orgs/{org_id}/auth-settings/sso")
    def put_sso(body: SSOIn, a: Access = Depends(org_admin)):
        current = store.get_auth_settings(a.org_id) or {}
        try:
            issuer = check_url(body.issuer) if body.issuer.strip() or body.enabled else ""
            domains = clean_domains(body.domains)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        client_id = body.client_id.strip()
        secret = (body.client_secret or "").strip()
        if body.default_role == "admin" and a.role != "owner" and current.get("default_role") != "admin":
            raise HTTPException(403, "Only an owner can make admin the default role for new people.")
        if body.enabled:
            if not client_id:
                raise HTTPException(422, "Enter the client ID from your identity provider.")
            if not domains:
                raise HTTPException(422, "Enter at least one email domain, for example acme.com.")
            if not secret and not current.get("ciphertext"):
                raise HTTPException(422, "Enter the client secret from your identity provider.")
            for other in store.list_sso_settings():
                taken = set(other["domains"] or []) & set(domains)
                if other["org_id"] != a.org_id and taken:
                    raise HTTPException(409, f"{sorted(taken)[0]} already signs in to another organization.")
        if body.enabled and body.require_sso and a.role != "owner" and a.user.get("auth_method") != f"sso:{a.org_id}":
            raise HTTPException(409, "Sign in with SSO once before you require it, so you know it works. "
                                     "(Owners can always sign in with a password.)")
        fields: dict[str, Any] = dict(sso_enabled=body.enabled, issuer=issuer, client_id=client_id, domains=domains,
                                      default_role=body.default_role, require_sso=body.enabled and body.require_sso)
        if secret:
            if not secrets_box.enabled():
                raise HTTPException(503, "Storing the client secret needs ARCHFLOW_SECRET_KEY (a long random string) "
                                         "in the API's environment. Set it and restart the API.")
            fields.update(ciphertext=secrets_box.encrypt(secret, aad(a.org_id)), last4=secret[-4:])
        store.set_auth_settings(a.org_id, a.user["id"], **fields)
        clear_cache()
        return settings_payload(store, a.org_id)

    @app.delete("/api/orgs/{org_id}/auth-settings/sso", status_code=204)
    def delete_sso(a: Access = Depends(org_admin)):
        store.set_auth_settings(a.org_id, a.user["id"], sso_enabled=False, issuer="", client_id="", ciphertext="",
                                last4="", domains=[], require_sso=False)

    @app.post("/api/orgs/{org_id}/auth-settings/sso/test")
    def test_sso(a: Access = Depends(org_admin)):
        """Read the provider's discovery document and keys with the saved issuer."""
        s = store.get_auth_settings(a.org_id) or {}
        if not s.get("issuer"):
            raise HTTPException(409, "Save the issuer URL first.")
        try:
            doc = discovery(s["issuer"], refresh=True)
            keys = _cached(f"jwks:{doc['jwks_uri']}", lambda: _get_json(doc["jwks_uri"], "key set"), True)
        except SSOError as exc:
            return {"ok": False, "detail": str(exc)}
        return {"ok": True, "detail": f"Found {doc['issuer']} with {len(keys.get('keys') or [])} signing key(s)."}

    @app.put("/api/orgs/{org_id}/auth-settings/mfa")
    def put_mfa_policy(body: MfaPolicyIn, a: Access = Depends(org_admin)):
        if body.require_mfa and not store.mfa_enabled(a.user["id"]):
            raise HTTPException(409, "Turn on two-step sign-in for your own account first.")
        store.set_auth_settings(a.org_id, a.user["id"], require_mfa=body.require_mfa)
        return settings_payload(store, a.org_id)

    # ---- sign in ----
    @app.post("/api/auth/sso/discover")
    def sso_discover(body: DiscoverIn):
        email = users.normalize_email(body.email)
        if not users.valid_email(email):
            raise HTTPException(422, "Enter your work email address.")
        s = find_org(store, email)
        if not s:
            raise HTTPException(404, "Single sign-on is not set up for this email domain. Sign in with your password.")
        org = store.get_org(s["org_id"]) or {}
        return {"org_id": s["org_id"], "org_name": org.get("name", ""),
                "start_url": f"/api/auth/sso/{s['org_id']}/start?{urlencode({'login_hint': email})}"}

    @app.get("/api/auth/sso/{org_id}/start")
    def sso_start(org_id: str, login_hint: str = ""):
        try:
            url, binding = begin(store, org_id, login_hint[:320])
        except SSOError as exc:
            return _login_redirect(sso_error=str(exc))
        resp = RedirectResponse(url, status_code=302)
        resp.set_cookie(SSO_COOKIE, binding, max_age=STATE_TTL, path=COOKIE_PATH, httponly=True, samesite="lax",
                        secure=users.cookie_secure())
        return resp

    @app.get("/api/auth/sso/callback")
    def sso_callback(request: Request, state: str = "", code: str = "", error: str = "",
                     error_description: str = ""):
        binding = request.cookies.get(SSO_COOKIE)
        try:
            if error:
                if state:
                    store.pop_auth_code(hash_secret(f"sso:{state[:200]}"))
                raise SSOError(f"The identity provider did not sign you in: {(error_description or error)[:200]}")
            user, org_id = finish(store, state, code, binding)
        except SSOError as exc:
            log.info("SSO sign-in failed: %s", exc)
            resp = _login_redirect(sso_error=str(exc))
        else:
            method = f"sso:{org_id}"
            if store.mfa_enabled(user["id"]):
                resp = _login_redirect(mfa=mfa.start_challenge(store, user["id"], method))
            else:
                resp = RedirectResponse(f"{users.frontend_url()}/#/", status_code=303)
                accounts.start_session(store, user["id"], request, resp, method)
        resp.delete_cookie(SSO_COOKIE, path=COOKIE_PATH, secure=users.cookie_secure(), httponly=True, samesite="lax")
        return resp
