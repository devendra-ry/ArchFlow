"""Two-step sign-in (MFA) with time-based one-time codes (TOTP, RFC 6238) and recovery codes.

* Enroll: ``POST /api/auth/mfa/setup`` returns a new secret and its ``otpauth://`` URI (the web app draws the QR
  code); ``POST /api/auth/mfa/enable`` with a current code turns it on and returns 10 one-time recovery codes
  (shown once; only their SHA-256 hashes are stored). The TOTP secret is encrypted with ``secrets_box`` (AES-GCM
  bound to the user), so MFA needs ARCHFLOW_SECRET_KEY (or the generated key file), like the org AI keys.
* Sign in: when MFA is on, ``POST /api/auth/login`` (and the SSO callback) answer with a short-lived challenge
  token instead of a session; ``POST /api/auth/mfa/verify`` with the token and a code (or a recovery code)
  creates the session. A code's time step is recorded so it cannot be used twice.
* Turn off with a current code (``POST /api/auth/mfa/disable``), unless one of your orgs requires MFA.
* Code attempts are rate limited per user: 5 failures in 15 minutes, then wait.

TOTP: HMAC-SHA1, 30-second steps, 6 digits, accepting one step either side for clock drift.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from typing import Any
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import secrets_box, users
from .auth import hash_secret

ISSUER = "ArchFlow"
STEP = 30
DIGITS = 6
DRIFT = 1                  # accept codes from one step before or after now
RECOVERY_CODES = 10
CHALLENGE_TTL = 300        # seconds to enter the code after the password (or SSO)
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no 0/o, 1/l/i

# 5 wrong codes per user in 15 minutes, then wait (separate from the password limiter).
limiter: users.LoginLimiter = users.InMemoryLoginLimiter(max_failures=5, window=15 * 60)


# --------------------------------------------------------------------------- TOTP (RFC 6238)
def new_secret() -> str:
    """A random 160-bit secret, base32 without padding (what authenticator apps expect)."""
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def totp(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def current_step(at: float | None = None) -> int:
    return int((time.time() if at is None else at) // STEP)


def match_step(secret: str, code: str, at: float | None = None) -> int | None:
    """The time step ``code`` belongs to (now ± DRIFT), or None."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    now = current_step(at)
    for step in range(now - DRIFT, now + DRIFT + 1):
        if hmac.compare_digest(totp(secret, step), code):
            return step
    return None


def otpauth_uri(secret: str, account: str, issuer: str = ISSUER) -> str:
    label = quote(f"{issuer}:{account}")
    return f"otpauth://totp/{label}?" + urlencode({"secret": secret, "issuer": issuer, "algorithm": "SHA1",
                                                   "digits": DIGITS, "period": STEP})


# --------------------------------------------------------------------------- recovery codes
def new_recovery_codes(n: int = RECOVERY_CODES) -> list[str]:
    def one() -> str:
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        return f"{raw[:5]}-{raw[5:]}"
    return [one() for _ in range(n)]


def recovery_hash(code: str) -> str:
    return hash_secret("mfa-recovery:" + (code or "").strip().lower().replace("-", "").replace(" ", ""))


def looks_like_recovery(code: str) -> bool:
    c = (code or "").strip().lower().replace("-", "").replace(" ", "")
    return len(c) == 10 and all(ch in _RECOVERY_ALPHABET for ch in c)


# --------------------------------------------------------------------------- stored factor
def aad(user_id: str) -> str:
    return f"user-mfa:{user_id}"


def _secret_of(rec: dict[str, Any]) -> str:
    return secrets_box.decrypt(rec["ciphertext"], aad(rec["user_id"]))


def check_code(store, user_id: str, code: str, *, allow_recovery: bool = True, pending: bool = False) -> bool:
    """Verify a TOTP code (or, if allowed, a recovery code) for ``user_id``, rate limited. ``pending`` checks the
    factor being enrolled instead of the enabled one. Raises HTTPException 429 when there were too many failures."""
    key = f"mfa|{user_id}"
    wait = limiter.check(key)
    if wait:
        raise HTTPException(429, "Too many wrong codes. Try again later.", headers={"Retry-After": str(int(wait) + 1)})
    rec = store.get_mfa(user_id)
    ok = False
    if rec and bool(rec["enabled"]) != pending:
        try:
            step = match_step(_secret_of(rec), code)
        except secrets_box.SecretDecryptError:
            step = None
        if step is not None:
            ok = store.use_mfa_step(user_id, step)  # False = this code was already used
        elif allow_recovery and not pending and looks_like_recovery(code):
            ok = store.use_recovery_code(user_id, recovery_hash(code))
    if ok:
        limiter.success(key)
    else:
        limiter.failure(key)
    return ok


def required_by(store, user_id: str) -> list[dict[str, Any]]:
    """The user's orgs that require MFA."""
    out = []
    for o in store.list_user_orgs(user_id):
        s = store.get_auth_settings(o["id"])
        if s and s["require_mfa"]:
            out.append({"id": o["id"], "name": o["name"]})
    return out


# --------------------------------------------------------------------------- sign-in challenge
def start_challenge(store, user_id: str, auth_method: str = "password") -> str:
    """After the first factor: a short-lived, single-use token to present with the code."""
    token = "afm_" + secrets.token_urlsafe(24)
    store.save_auth_code(hash_secret(f"mfa:{token}"), {"user_id": user_id, "auth_method": auth_method},
                         time.time() + CHALLENGE_TTL)
    return token


def peek_challenge(store, token: str) -> dict[str, Any] | None:
    if not token or len(token) > 200:
        return None
    return store.get_auth_code(hash_secret(f"mfa:{token}"))


def finish_challenge(store, token: str) -> dict[str, Any] | None:
    return store.pop_auth_code(hash_secret(f"mfa:{token}"))


# --------------------------------------------------------------------------- routes
class CodeIn(BaseModel):
    code: str = Field(min_length=1, max_length=40)


class VerifyIn(BaseModel):
    mfa_token: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=40)


def status_payload(store, user_id: str) -> dict[str, Any]:
    rec = store.get_mfa(user_id)
    return {"enabled": bool(rec and rec["enabled"]), "pending": bool(rec and not rec["enabled"]),
            "recovery_codes_left": len(rec["recovery_hashes"]) if rec and rec["enabled"] else 0,
            "required_by": required_by(store, user_id), "available": secrets_box.enabled()}


def register_routes(app: FastAPI, store: Any, authz: Any) -> None:
    from . import accounts  # accounts imports this module

    current_user = authz.current_user

    @app.get("/api/auth/mfa")
    def mfa_status(user: dict = Depends(current_user)):
        return status_payload(store, user["id"])

    @app.post("/api/auth/mfa/setup")
    def mfa_setup(user: dict = Depends(current_user)):
        if not secrets_box.enabled():
            raise HTTPException(503, "Two-step sign-in needs ARCHFLOW_SECRET_KEY (a long random string) in the API's "
                                     "environment. Ask your administrator.")
        if store.mfa_enabled(user["id"]):
            raise HTTPException(409, "Two-step sign-in is already on.")
        secret = new_secret()
        store.start_mfa(user["id"], secrets_box.encrypt(secret, aad(user["id"])))
        return {"secret": secret, "otpauth_uri": otpauth_uri(secret, user["email"])}

    @app.post("/api/auth/mfa/enable")
    def mfa_enable(body: CodeIn, user: dict = Depends(current_user)):
        if store.mfa_enabled(user["id"]):
            raise HTTPException(409, "Two-step sign-in is already on.")
        if not store.get_mfa(user["id"]):
            raise HTTPException(409, "Start the setup first.")
        if not check_code(store, user["id"], body.code, allow_recovery=False, pending=True):
            raise HTTPException(400, "That code is not right. Check the time on your phone and try the newest code.")
        codes = new_recovery_codes()
        rec = store.get_mfa(user["id"])
        store.enable_mfa(user["id"], [recovery_hash(c) for c in codes], rec["last_step"])
        return {**status_payload(store, user["id"]), "recovery_codes": codes}

    @app.post("/api/auth/mfa/disable")
    def mfa_disable(body: CodeIn, user: dict = Depends(current_user)):
        if not store.mfa_enabled(user["id"]):
            raise HTTPException(409, "Two-step sign-in is not on.")
        orgs = required_by(store, user["id"])
        if orgs:
            raise HTTPException(409, f"{orgs[0]['name']} requires two-step sign-in, so you cannot turn it off.")
        if not check_code(store, user["id"], body.code):
            raise HTTPException(400, "That code is not right.")
        store.delete_mfa(user["id"])
        return status_payload(store, user["id"])

    @app.post("/api/auth/mfa/recovery-codes")
    def mfa_new_recovery_codes(body: CodeIn, user: dict = Depends(current_user)):
        if not store.mfa_enabled(user["id"]):
            raise HTTPException(409, "Two-step sign-in is not on.")
        if not check_code(store, user["id"], body.code, allow_recovery=False):
            raise HTTPException(400, "That code is not right.")
        codes = new_recovery_codes()
        store.set_recovery_codes(user["id"], [recovery_hash(c) for c in codes])
        return {**status_payload(store, user["id"]), "recovery_codes": codes}

    @app.post("/api/auth/mfa/verify")
    def mfa_verify(body: VerifyIn, request: Request, response: Response):
        """Second sign-in step: the challenge token from login (or SSO) and a code or recovery code."""
        challenge = peek_challenge(store, body.mfa_token)
        if not challenge:
            raise HTTPException(401, "This sign-in has expired. Sign in again.")
        if not check_code(store, challenge["user_id"], body.code):
            raise HTTPException(401, "That code is not right.")
        if not finish_challenge(store, body.mfa_token):  # single use (lost a race with another request)
            raise HTTPException(401, "This sign-in has expired. Sign in again.")
        return accounts.start_session(store, challenge["user_id"], request, response, challenge["auth_method"])
