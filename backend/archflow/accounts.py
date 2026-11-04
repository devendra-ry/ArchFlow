"""Account, organization, membership, invite and org AI-settings endpoints.

Registered by ``main.create_app`` via :func:`register_routes`. See users.py (passwords, sessions, rate limiting)
and authz.py (roles and dependencies).

Bootstrap: while the users table is empty, the first signup becomes owner of every existing org (claiming
legacy data, including ``org-default``). Later signups join the org of their invite, or get a personal org
("<name>'s workspace") as owner (unless ARCHFLOW_OPEN_SIGNUP=false, which then requires an invite).
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import ai_providers, llm, scale, secrets_box, users
from .auth import hash_secret
from .authz import RANK, ROLES, Access, Authz

ROLE_PATTERN = "^(" + "|".join(ROLES) + ")$"
INVITE_TTL = 7 * 24 * 3600
_signup_lock = threading.Lock()  # serializes the "first user" bootstrap check (across instances with Redis)


# --------------------------------------------------------------------------- request bodies
class SignupIn(BaseModel):
    email: str = Field(max_length=320)
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(max_length=users.MAX_PASSWORD)
    invite: str | None = Field(None, max_length=200)


class LoginIn(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=users.MAX_PASSWORD)


class OrgIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    owner: str = ""  # ignored (backwards compatibility): the creator is the owner


class OrgPatch(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class MemberPatch(BaseModel):
    role: str = Field(pattern=ROLE_PATTERN)


class InviteIn(BaseModel):
    email: str | None = Field(None, max_length=320)
    role: str = Field("developer", pattern=ROLE_PATTERN)


class AIConfigIn(BaseModel):
    """Model choices per group ([] = server defaults) and provider parameters (endpoints, API versions)."""
    reasoning: list[dict[str, Any]] = Field(default_factory=list, max_length=4)
    fast: list[dict[str, Any]] = Field(default_factory=list, max_length=4)
    params: dict[str, dict[str, Any]] = Field(default_factory=dict)


class AITestIn(BaseModel):
    group: str = Field("reasoning", pattern="^(reasoning|fast)$")


# --------------------------------------------------------------------------- helpers
def client_ip(request: Request) -> str:
    # Behind a reverse proxy this is the proxy's address unless uvicorn runs with --proxy-headers.
    return request.client.host if request.client else "-"


def set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(users.SESSION_COOKIE, token, **users.cookie_kwargs())


def me_payload(store, user: dict[str, Any]) -> dict[str, Any]:
    return {"user": users.public_user(user), "orgs": store.list_user_orgs(user["id"]),
            "features": {"ai_settings": secrets_box.enabled()}}


def _invite_link(token: str) -> str:
    return f"{users.frontend_url()}/#/invite/{token}"


def join_with_invite(store, invite: dict[str, Any], user: dict[str, Any]) -> None:
    """Consume an invite for ``user`` (single use). Raises HTTPException if it cannot be used."""
    if invite.get("email") and users.normalize_email(invite["email"]) != user["email"]:
        raise HTTPException(403, "This invite is for a different email address.")
    if not store.accept_invite(invite["id"], user["id"]):
        raise HTTPException(410, "This invite has already been used or has expired.")
    current = store.get_role(invite["org_id"], user["id"])
    if current is None or RANK[invite["role"]] > RANK.get(current, -1):
        store.add_member(invite["org_id"], user["id"], invite["role"])


def ai_settings_payload(store, org_id: str) -> dict[str, Any]:
    """Everything the AI settings screen needs. Key values are never returned (only the last 4 characters)."""
    enabled = secrets_box.enabled()
    rows = {r["provider"]: r for r in store.list_ai_keys(org_id)} if enabled else {}
    status = llm.status(org_id)
    return {"enabled": enabled, "providers": {
        p: {"configured": p in rows, "last4": rows[p]["last4"] if p in rows else None,
            "updated_at": rows[p]["updated_at"] if p in rows else None} for p in secrets_box.PROVIDERS},
        "catalog": ai_providers.catalog(), "config": store.get_ai_config(org_id) or {"reasoning": [], "fast": [], "params": {}},
        "defaults": {g: llm.default_chain(g) for g in ai_providers.GROUPS}, "effective": status["groups"],
        "available": status["available"]}


def require_secrets() -> None:
    if not secrets_box.enabled():
        raise HTTPException(503, "Organization AI keys are disabled: set ARCHFLOW_SECRET_KEY (a long random string, "
                                 "e.g. `openssl rand -base64 32`) in the API's environment and restart it. "
                                 "Until then the server's own GEMINI_API_KEY / ANTHROPIC_API_KEY / OPENAI_API_KEY "
                                 "are used.")


# --------------------------------------------------------------------------- routes
def register_routes(app: FastAPI, store: Any, authz: Authz) -> None:
    current_user = authz.current_user

    # ---- sign up / in / out ----
    @app.post("/api/auth/signup", status_code=201)
    def signup(body: SignupIn, request: Request, response: Response):
        email = users.normalize_email(body.email)
        name = body.name.strip()
        if not users.valid_email(email):
            raise HTTPException(422, "Enter a valid email address.")
        if not name:
            raise HTTPException(422, "Enter your name.")
        if err := users.check_password_rules(body.password):
            raise HTTPException(422, err)
        invite = None
        if body.invite:
            invite = store.get_invite(hash_secret(body.invite))
            if not invite:
                raise HTTPException(410, "This invite is invalid, already used or expired.")
            if invite.get("email") and users.normalize_email(invite["email"]) != email:
                raise HTTPException(403, "This invite is for a different email address.")
        password_hash = users.hash_password(body.password)
        with scale.lock("signup", _signup_lock):
            if store.get_user_by_email(email):
                raise HTTPException(409, "An account with this email already exists. Sign in instead.")
            first = store.count_users() == 0
            if not first and not invite and not users.open_signup():
                raise HTTPException(403, "Sign-up is by invitation only. Ask an admin for an invite link.")
            user = store.create_user(email, name, password_hash)
        if first:  # bootstrap: claim every existing org, including legacy data in org-default
            for org in store.list_orgs():
                store.add_member(org["id"], user["id"], "owner")
        if invite:
            join_with_invite(store, invite, user)
        if not first and not invite:
            org = store.create_org(f"{name}'s workspace")
            store.add_member(org["id"], user["id"], "owner")
        store.touch_login(user["id"])
        set_session_cookie(response, users.create_session(store, user["id"], request.headers.get("user-agent", "")))
        return me_payload(store, store.get_user(user["id"]))

    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request, response: Response):
        email = users.normalize_email(body.email)
        key = users.limiter_key(email, client_ip(request))
        wait = users.login_limiter.check(key)
        if wait:
            raise HTTPException(429, "Too many sign-in attempts. Try again later.",
                                headers={"Retry-After": str(int(wait) + 1)})
        rec = store.get_user_by_email(email, with_hash=True)
        if not users.verify_password(body.password, rec["password_hash"] if rec else None):
            users.login_limiter.failure(key)
            raise HTTPException(401, "Invalid email or password.")
        users.login_limiter.success(key)
        store.touch_login(rec["id"])
        set_session_cookie(response, users.create_session(store, rec["id"], request.headers.get("user-agent", "")))
        return me_payload(store, store.get_user(rec["id"]))

    @app.post("/api/auth/logout", status_code=204)
    def logout(request: Request, response: Response):
        token = request.cookies.get(users.SESSION_COOKIE)
        if token:
            store.revoke_session(users.session_hash(token))
        response.delete_cookie(users.SESSION_COOKIE, path="/", secure=users.cookie_secure(), httponly=True,
                               samesite="lax")

    @app.get("/api/me")
    def me(request: Request, response: Response, user: dict = Depends(current_user)):
        # Re-issue the cookie so the browser's copy follows the sliding server-side expiry.
        set_session_cookie(response, request.cookies[users.SESSION_COOKIE])
        return me_payload(store, user)

    # ---- organizations ----
    @app.get("/api/orgs")
    def list_orgs(user: dict = Depends(current_user)):
        return store.list_user_orgs(user["id"])

    @app.post("/api/orgs", status_code=201)
    def create_org(body: OrgIn, user: dict = Depends(current_user)):
        org = store.create_org(body.name.strip())
        store.add_member(org["id"], user["id"], "owner")
        return {**org, "role": "owner"}

    @app.patch("/api/orgs/{org_id}")
    def update_org(body: OrgPatch, a: Access = Depends(authz.org("owner"))):
        return {**store.update_org(a.org_id, body.name.strip()), "role": a.role}

    # ---- members ----
    @app.get("/api/orgs/{org_id}/members")
    def list_members(a: Access = Depends(authz.org("read"))):
        return store.list_members(a.org_id)

    def target_member(a: Access, user_id: str) -> dict[str, Any]:
        m = store.get_member(a.org_id, user_id)
        if not m:
            raise HTTPException(404, "member not found")
        return m

    def ensure_not_last_owner(org_id: str, member: dict[str, Any]) -> None:
        if member["role"] == "owner" and store.count_role(org_id, "owner") <= 1:
            raise HTTPException(409, "An organization needs at least one owner. Make someone else owner first.")

    @app.patch("/api/orgs/{org_id}/members/{user_id}")
    def update_member(user_id: str, body: MemberPatch, a: Access = Depends(authz.org("admin"))):
        m = target_member(a, user_id)
        if (body.role == "owner" or m["role"] == "owner") and a.role != "owner":
            raise HTTPException(403, "Only an owner can grant or change the owner role.")
        if body.role != "owner":
            ensure_not_last_owner(a.org_id, m)
        return store.add_member(a.org_id, user_id, body.role)

    @app.delete("/api/orgs/{org_id}/members/{user_id}", status_code=204)
    def remove_member(org_id: str, user_id: str, request: Request):
        user = current_user(request)
        # Anyone may leave an org; removing someone else needs admin (and owner to remove an owner).
        a = authz.check_org(user, org_id, "read" if user_id == user["id"] else "admin")
        m = target_member(a, user_id)
        if m["role"] == "owner" and a.role != "owner":
            raise HTTPException(403, "Only an owner can remove an owner.")
        ensure_not_last_owner(org_id, m)
        store.remove_member(org_id, user_id)
        store.revoke_user_tokens(org_id, user_id)  # their MCP tokens and PATs for this org end now

    # ---- invites ----
    @app.post("/api/orgs/{org_id}/invites", status_code=201)
    def create_invite(body: InviteIn, a: Access = Depends(authz.org("admin"))):
        if body.role == "owner" and a.role != "owner":
            raise HTTPException(403, "Only an owner can invite another owner.")
        email = users.normalize_email(body.email) if body.email and body.email.strip() else None
        if email and not users.valid_email(email):
            raise HTTPException(422, "Enter a valid email address or leave it empty.")
        token = "afi_" + secrets.token_urlsafe(24)
        inv = store.create_invite(a.org_id, hash_secret(token), body.role, time.time() + INVITE_TTL, email=email,
                                  created_by=a.user["id"])
        return {**inv, "token": token, "link": _invite_link(token)}

    @app.get("/api/orgs/{org_id}/invites")
    def list_invites(a: Access = Depends(authz.org("admin"))):
        return store.list_invites(a.org_id)

    @app.delete("/api/orgs/{org_id}/invites/{invite_id}", status_code=204)
    def revoke_invite(invite_id: str, a: Access = Depends(authz.org("admin"))):
        if not store.revoke_invite(a.org_id, invite_id):
            raise HTTPException(404, "invite not found")

    @app.get("/api/invites/{token}")
    def invite_info(token: str):
        """Public, but only for whoever holds the (secret, single-use) invite token: what it is an invite to."""
        inv = store.get_invite(hash_secret(token))
        if not inv:
            raise HTTPException(404, "This invite is invalid, already used or expired.")
        org = store.get_org(inv["org_id"]) or {}
        return {"org_name": org.get("name"), "role": inv["role"], "email": inv.get("email"),
                "expires_at": inv["expires_at"]}

    @app.post("/api/invites/{token}/accept")
    def accept_invite(token: str, user: dict = Depends(current_user)):
        inv = store.get_invite(hash_secret(token))
        if not inv:
            raise HTTPException(410, "This invite is invalid, already used or expired.")
        join_with_invite(store, inv, user)
        org = store.get_org(inv["org_id"])
        return {**org, "role": store.get_role(inv["org_id"], user["id"])}

    # ---- org AI provider keys (bring your own key) ----
    @app.get("/api/orgs/{org_id}/ai-settings")
    def get_ai_settings(a: Access = Depends(authz.org("admin"))):
        return ai_settings_payload(store, a.org_id)

    @app.put("/api/orgs/{org_id}/ai-settings")
    def put_ai_settings(body: dict[str, str | None], a: Access = Depends(authz.org("admin"))):
        """Per provider: a key sets it, "" or null clears it, an omitted provider is left unchanged."""
        require_secrets()
        unknown = [p for p in body if p not in secrets_box.PROVIDERS]
        if unknown:
            raise HTTPException(422, f"Unknown provider: {', '.join(unknown)}")
        for provider, raw in body.items():
            value = (raw or "").strip()
            if len(value) > 500:
                raise HTTPException(422, f"The {provider} key is too long.")
            if not value:
                store.delete_ai_key(a.org_id, provider)
                continue
            if len(value) < 8:
                raise HTTPException(422, f"The {provider} key looks too short.")
            ciphertext = secrets_box.encrypt(value, secrets_box.aad_for(a.org_id, provider))
            store.set_ai_key(a.org_id, provider, ciphertext, value[-4:], a.user["id"])
        llm.invalidate_org(a.org_id)
        return ai_settings_payload(store, a.org_id)

    @app.put("/api/orgs/{org_id}/ai-config")
    def put_ai_config(body: AIConfigIn, a: Access = Depends(authz.org("admin"))):
        """Which models the org uses for each group (in fallback order); empty = the server's defaults."""
        try:
            config = ai_providers.normalize_config(body.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        store.set_ai_config(a.org_id, config, a.user["id"])
        llm.invalidate_org(a.org_id)
        return ai_settings_payload(store, a.org_id)

    @app.post("/api/orgs/{org_id}/ai-test")
    async def test_ai(body: AITestIn, a: Access = Depends(authz.org("admin"))):
        """Make one tiny real call with the org's current settings and report the result."""
        return await asyncio.to_thread(llm.test_connection, a.org_id, body.group)
