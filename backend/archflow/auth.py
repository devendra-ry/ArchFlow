"""OAuth 2.1 authorization server and API tokens for the remote MCP endpoint.

MCP clients (Claude.ai and Claude Desktop connectors, Claude Code, Codex, Cursor, …) discover this server
through the standard metadata documents, register themselves (dynamic client registration), send the user
to a consent page, and exchange the code (with PKCE) for an access token. Headless clients can instead use a
**personal access token** created in the UI.

Every token is bound to one user and one organization and carries scopes:

* ``archflow:read``  — read architectures, requirements, versions, analysis and coding briefs
* ``archflow:write`` — edit the model, commit versions, run AI commands, report task progress

The consent page requires a signed-in ArchFlow session, lists only the user's orgs and caps the scopes by
their role (viewers and reviewers get read only). Every use of a token re-checks that its user is still a
member of the org (removed members lose access immediately); MCP tools also cap scopes by the current role.

Secrets are never stored: authorization codes and tokens are kept as SHA-256 hashes.
"""

from __future__ import annotations

import hashlib
import html
import os
import secrets
import time
from typing import Any

from pydantic import AnyUrl

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

SCOPE_READ = "archflow:read"
SCOPE_WRITE = "archflow:write"
ALL_SCOPES = [SCOPE_READ, SCOPE_WRITE]

PUBLIC_URL = os.environ.get("ARCHFLOW_PUBLIC_URL", "http://localhost:8000").rstrip("/")
MCP_URL = f"{PUBLIC_URL}/mcp"
ACCESS_TTL = int(os.environ.get("ARCHFLOW_ACCESS_TOKEN_TTL", 3600))
REFRESH_TTL = int(os.environ.get("ARCHFLOW_REFRESH_TOKEN_TTL", 30 * 24 * 3600))
CODE_TTL = 300
PENDING_TTL = 900

TOKEN_PREFIX = {"access": "afa_", "refresh": "afr_", "pat": "afp_"}


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_secret(kind: str) -> str:
    return TOKEN_PREFIX[kind] + secrets.token_urlsafe(32)


def normalize_scopes(scopes: list[str] | None) -> list[str]:
    """Known scopes only; write implies read."""
    out = [s for s in (scopes or []) if s in ALL_SCOPES] or [SCOPE_READ]
    if SCOPE_WRITE in out and SCOPE_READ not in out:
        out.insert(0, SCOPE_READ)
    return out


class ArchFlowAuthCode(AuthorizationCode):
    org_id: str
    user_name: str
    user_id: str


class ArchFlowRefreshToken(RefreshToken):
    org_id: str
    user_name: str
    user_id: str


def create_pat(store, org_id: str, user: dict[str, Any], name: str, scopes: list[str],
               expires_in_days: int | None = None) -> tuple[str, dict[str, Any]]:
    """Create a personal access token for ``user``. Returns (secret, record); the secret is shown once."""
    secret = new_secret("pat")
    expires = time.time() + expires_in_days * 86400 if expires_in_days else None
    record = store.create_token(hash_secret(secret), "pat", org_id, user["name"], normalize_scopes(scopes),
                                name=name, expires_at=expires, resource=MCP_URL, user_id=user["id"])
    return secret, record


def token_member(store, rec: dict[str, Any] | None) -> str | None:
    """The token owner's CURRENT role in the token's org, or None (unknown owner or no longer a member)."""
    if not rec or not rec.get("user_id"):
        return None
    return store.get_role(rec["org_id"], rec["user_id"])


class ArchFlowOAuthProvider:
    """Implements mcp.server.auth.provider.OAuthAuthorizationServerProvider on top of the ArchFlow store."""

    def __init__(self, store):
        self.store = store

    # ---- clients (RFC 7591 dynamic registration) ----
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self.store.get_oauth_client(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self.store.save_oauth_client(client_info.client_id, client_info.model_dump(mode="json"))

    # ---- authorization: hand off to the consent page ----
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        if params.resource and params.resource.rstrip("/") != MCP_URL:
            raise AuthorizeError(error="invalid_request", error_description=f"unknown resource {params.resource}")
        request_id = secrets.token_urlsafe(18)
        self.store.save_auth_code(hash_secret(f"pending:{request_id}"), {
            "client_id": client.client_id,
            "client_name": client.client_name or client.client_id,
            "scopes": normalize_scopes(params.scopes or ALL_SCOPES),
            "state": params.state,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "resource": params.resource,
        }, time.time() + PENDING_TTL)
        return f"{PUBLIC_URL}/oauth/consent?request={request_id}"

    def pending_request(self, request_id: str) -> dict[str, Any] | None:
        return self.store.get_auth_code(hash_secret(f"pending:{request_id}"))

    def complete_consent(self, request_id: str, approve: bool, org_id: str, user: dict[str, Any] | None,
                         scopes: list[str]) -> str:
        """Finish the consent step; returns the client redirect URL (with a code, or access_denied)."""
        req = self.store.pop_auth_code(hash_secret(f"pending:{request_id}"))
        if not req:
            raise ValueError("This authorization request has expired. Start the connection again from your client.")
        if not approve:
            return construct_redirect_uri(req["redirect_uri"], error="access_denied",
                                          error_description="The user denied access", state=req["state"])
        if not user:
            raise ValueError("Sign in to approve access.")
        granted = [s for s in normalize_scopes(scopes) if s in req["scopes"]] or [SCOPE_READ]
        code = secrets.token_urlsafe(32)
        self.store.save_auth_code(hash_secret(code), {
            **{k: req[k] for k in ("client_id", "code_challenge", "redirect_uri", "redirect_uri_provided_explicitly")},
            "scopes": granted, "resource": req["resource"] or MCP_URL, "org_id": org_id,
            "user_name": user["name"], "user_id": user["id"],
        }, time.time() + CODE_TTL)
        return construct_redirect_uri(req["redirect_uri"], code=code, state=req["state"])

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        data = self.store.get_auth_code(hash_secret(authorization_code))
        if not data or data.get("client_id") != client.client_id or "user_id" not in data:  # pending consent ≠ code
            return None
        return ArchFlowAuthCode(code=authorization_code, expires_at=time.time() + CODE_TTL, subject=data["user_id"],
                                redirect_uri=AnyUrl(data["redirect_uri"]), **{k: v for k, v in data.items() if k != "redirect_uri"})

    async def exchange_authorization_code(self, client: OAuthClientInformationFull, authorization_code) -> OAuthToken:
        if not self.store.pop_auth_code(hash_secret(authorization_code.code)):  # single use
            raise TokenError(error="invalid_grant", error_description="authorization code already used or expired")
        return self._issue(client.client_id, authorization_code.org_id, authorization_code.user_name,
                           authorization_code.user_id, authorization_code.scopes, authorization_code.resource)

    # ---- refresh tokens (rotated on every use) ----
    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str):
        rec = self.store.get_token(hash_secret(refresh_token))
        if not rec or rec["kind"] != "refresh" or rec["client_id"] != client.client_id:
            return None
        if token_member(self.store, rec) is None:  # removed from the org: no new access tokens
            return None
        return ArchFlowRefreshToken(token=refresh_token, client_id=client.client_id, scopes=rec["scopes"],
                                    expires_at=int(rec["expires_at"]) if rec["expires_at"] else None,
                                    resource=rec["resource"], subject=rec["user_id"],
                                    org_id=rec["org_id"], user_name=rec["user_name"], user_id=rec["user_id"])

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token, scopes: list[str]) -> OAuthToken:
        self.store.revoke_token(hash_secret(refresh_token.token))
        granted = [s for s in (scopes or refresh_token.scopes) if s in refresh_token.scopes] or refresh_token.scopes
        return self._issue(client.client_id, refresh_token.org_id, refresh_token.user_name, refresh_token.user_id,
                           granted, refresh_token.resource)

    def _issue(self, client_id: str, org_id: str, user_name: str, user_id: str, scopes: list[str],
               resource: str | None) -> OAuthToken:
        access, refresh = new_secret("access"), new_secret("refresh")
        now = time.time()
        common = dict(org_id=org_id, user_name=user_name, user_id=user_id, scopes=scopes, client_id=client_id,
                      resource=resource or MCP_URL)
        self.store.create_token(hash_secret(access), "access", expires_at=now + ACCESS_TTL, **common)
        self.store.create_token(hash_secret(refresh), "refresh", expires_at=now + REFRESH_TTL, **common)
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, scope=" ".join(scopes), refresh_token=refresh)

    # ---- resource server: validate bearer tokens (OAuth access tokens and personal access tokens) ----
    async def load_access_token(self, token: str) -> AccessToken | None:
        h = hash_secret(token)
        rec = self.store.get_token(h)
        if not rec or rec["kind"] not in ("access", "pat"):
            return None
        if token_member(self.store, rec) is None:  # owner unknown or no longer a member of the org
            return None
        self.store.touch_token(h)
        return AccessToken(token=token, client_id=rec["client_id"] or f"pat:{rec['id']}", scopes=rec["scopes"],
                           expires_at=int(rec["expires_at"]) if rec["expires_at"] else None,
                           resource=rec["resource"] or MCP_URL, subject=rec["user_id"],
                           claims={"org_id": rec["org_id"], "token_id": rec["id"], "kind": rec["kind"],
                                   "user_id": rec["user_id"], "user_name": rec["user_name"]})

    async def revoke_token(self, token) -> None:
        self.store.revoke_token(hash_secret(token.token))


# --------------------------------------------------------------------------- consent and sign-in pages
_STYLE = """<style>
:root { --bg:#f7f7f8; --panel:#fff; --text:#1c1c21; --muted:#6b6b76; --border:#e3e3e8; --primary:#4f46e5; --warn:#b45309; }
@media (prefers-color-scheme: dark) { :root { --bg:#111114; --panel:#18181c; --text:#ececf1; --muted:#9a9aa6; --border:#2e2e36; --primary:#6d67f0; --warn:#f5a524; } }
* { box-sizing:border-box } body { margin:0; font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); }
main { max-width:460px; margin:48px auto; padding:0 16px; }
.card { background:var(--panel); border:1px solid var(--border); border-radius:14px; padding:22px; }
h1 { font-size:20px; margin:0 0 4px } p { margin:6px 0 } .muted { color:var(--muted); font-size:13.5px }
label { display:block; margin-top:14px; font-size:13px; color:var(--muted) }
input[type=text], input[type=email], input[type=password], select { width:100%; margin-top:4px; padding:8px 10px; border-radius:8px; border:1px solid var(--border); background:var(--panel); color:var(--text); font:inherit }
.radio { display:flex; gap:8px; align-items:flex-start; margin-top:8px; color:var(--text); font-size:14px }
.actions { display:flex; gap:10px; justify-content:flex-end; margin-top:20px }
button { font:inherit; padding:8px 16px; border-radius:8px; border:1px solid var(--border); background:var(--panel); color:var(--text); cursor:pointer }
button.primary { background:var(--primary); border-color:var(--primary); color:#fff }
.err { color:var(--warn); } .brand { font-weight:700; display:flex; gap:8px; align-items:center; margin-bottom:14px }
.logo { display:inline-grid; place-items:center; width:26px; height:26px; border-radius:7px; background:var(--primary); color:#fff; font-size:12px }
a { color:var(--primary) }
</style>"""


def _page(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>'
            f'{_STYLE}</head><body><main><div class="brand"><span class="logo">▲</span> ArchFlow</div>{body}</main></body></html>')


def consent_page(req: dict[str, Any], request_id: str, orgs: list[dict[str, Any]], user: dict[str, Any],
                 write_roles: set[str], error: str = "") -> str:
    """``orgs``: the user's memberships ({id, name, role}); ``write_roles``: roles that may grant edit access."""
    esc = html.escape
    can_write = SCOPE_WRITE in req["scopes"]
    if not orgs:
        return _page("Connect to ArchFlow", f"""<div class="card"><h1>Connect {esc(req["client_name"])}</h1>
<p class="err">You are not a member of any organization yet, so there is nothing to connect to.</p>
<p class="muted">Signed in as {esc(user["email"])}.</p></div>""")
    org_opts = "".join(
        f'<option value="{esc(o["id"])}">{esc(o["name"])} ({esc(o["role"])}'
        f'{"" if o["role"] in write_roles else ", read only"})</option>' for o in orgs)
    write_note = ('<p class="muted">Edit access needs the developer role or above in the chosen organization; '
                  'otherwise the connection is read only.</p>') if can_write else ""
    return _page("Connect to ArchFlow", f"""
<form class="card" method="post" action="/oauth/consent">
  <h1>Connect {esc(req["client_name"])}</h1>
  <p class="muted">This app wants to access your ArchFlow architectures over MCP. Choose the organization and what it may do.</p>
  <p class="muted">Signed in as <b>{esc(user["name"])}</b> ({esc(user["email"])}).</p>
  {f'<p class="err">{esc(error)}</p>' if error else ''}
  <input type="hidden" name="request" value="{esc(request_id)}">
  <label>Organization<select name="org_id" required>{org_opts}</select></label>
  <label>Access</label>
  <div class="radio"><input type="radio" name="access" value="read" id="r" {'' if can_write else 'checked'}><label for="r" style="margin:0;color:inherit">Read only: view architectures, requirements, versions and coding briefs</label></div>
  {f'<div class="radio"><input type="radio" name="access" value="write" id="w" checked><label for="w" style="margin:0;color:inherit">Read and edit: change the canvas, commit versions, report task progress</label></div>' if can_write else ''}
  {write_note}
  <div class="actions"><button type="submit" name="decision" value="deny">Deny</button>
  <button class="primary" type="submit" name="decision" value="approve">Allow access</button></div>
</form>
<p class="muted" style="text-align:center;margin-top:12px">You can revoke access at any time from ArchFlow.</p>""")


def login_page(next_url: str, app_url: str, error: str = "", email: str = "", mfa_token: str = "") -> str:
    """Minimal sign-in form served by the API (used by the OAuth consent flow; the web app has its own). With
    ``mfa_token`` it asks for the two-step sign-in code instead."""
    esc = html.escape
    if mfa_token:
        return _page("Sign in to ArchFlow", f"""
<form class="card" method="post" action="/oauth/login">
  <h1>Enter your code</h1>
  <p class="muted">Open your authenticator app and enter the 6-digit code for ArchFlow, or use a recovery code.</p>
  {f'<p class="err">{esc(error)}</p>' if error else ''}
  <input type="hidden" name="next" value="{esc(next_url)}">
  <input type="hidden" name="mfa_token" value="{esc(mfa_token)}">
  <label>Code<input type="text" name="code" required autocomplete="one-time-code" inputmode="text" autofocus></label>
  <div class="actions"><button class="primary" type="submit">Continue</button></div>
</form>""")
    return _page("Sign in to ArchFlow", f"""
<form class="card" method="post" action="/oauth/login">
  <h1>Sign in to continue</h1>
  <p class="muted">An app is asking to connect to your ArchFlow account.</p>
  {f'<p class="err">{esc(error)}</p>' if error else ''}
  <input type="hidden" name="next" value="{esc(next_url)}">
  <label>Email<input type="email" name="email" required autocomplete="email" value="{esc(email)}"></label>
  <label>Password<input type="password" name="password" required autocomplete="current-password"></label>
  <div class="actions"><button class="primary" type="submit">Sign in</button></div>
</form>
<p class="muted" style="text-align:center;margin-top:12px">No account yet? <a href="{esc(app_url)}/#/signup" target="_blank" rel="noopener">Create one in ArchFlow</a>, then come back to this page.</p>""")


