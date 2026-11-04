"""Accounts: password hashing, browser sessions, login rate limiting and the trusted browser origins.

* Passwords: stdlib ``hashlib.scrypt`` (N=2**14, r=8, p=1, 16-byte random salt, 32-byte key), compared in
  constant time. Stored as ``scrypt$N$r$p$<salt b64>$<hash b64>`` so parameters can be raised later.
* Sessions: an opaque random token in an HttpOnly, SameSite=Lax cookie (Secure when ARCHFLOW_PUBLIC_URL is
  https). Only its SHA-256 is stored. 14-day sliding expiry.
* Login rate limiting: per (email, client IP), in memory. ``LoginLimiter`` is the seam for a shared (Redis)
  implementation when the API runs as several processes.

Environment:
    ARCHFLOW_PUBLIC_URL        public URL of the API (its origin is always trusted; https => Secure cookies)
    ARCHFLOW_ALLOWED_ORIGINS   comma-separated browser origins allowed to call the API with the session cookie
                               (default http://localhost:5173, the Vite dev server)
    ARCHFLOW_OPEN_SIGNUP       "false" to require an invite for every signup except the very first
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import deque
from typing import Protocol
from urllib.parse import urlparse

from .auth import PUBLIC_URL, hash_secret

SESSION_COOKIE = "archflow_session"
SESSION_TTL = 14 * 24 * 3600
SESSION_REFRESH_AFTER = 3600       # extend the sliding expiry at most once an hour per session
MIN_PASSWORD = 10
MAX_PASSWORD = 1024                # scrypt input bound (avoid huge request bodies being hashed)

SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_DKLEN, SALT_BYTES = 2 ** 14, 8, 1, 32, 16

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,}$")

# A valid hash to verify against when the email is unknown, so timing does not reveal which accounts exist.
_DUMMY_HASH: str | None = None


# --------------------------------------------------------------------------- passwords
def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    global _DUMMY_HASH
    if not stored:
        _DUMMY_HASH = _DUMMY_HASH or hash_password("dummy-password-for-timing")
        stored, ok = _DUMMY_HASH, False
    else:
        ok = True
    try:
        algo, n, r, p, salt, expected = stored.split("$")
        if algo != "scrypt":
            return False
        exp = base64.b64decode(expected)
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                            dklen=len(exp), maxmem=2 ** 27)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, exp) and ok


def check_password_rules(password: str) -> str | None:
    """An error message, or None if the password is acceptable."""
    if len(password) < MIN_PASSWORD:
        return f"Password must be at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return "Password is too long."
    return None


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match(email)) and len(email) <= 320


def open_signup() -> bool:
    return os.environ.get("ARCHFLOW_OPEN_SIGNUP", "true").strip().lower() not in ("0", "false", "no", "off")


# --------------------------------------------------------------------------- sessions
def new_session_token() -> str:
    return "afs_" + secrets.token_urlsafe(32)


def session_hash(token: str) -> str:
    return hash_secret(token)


def cookie_secure() -> bool:
    return PUBLIC_URL.lower().startswith("https://")


def cookie_kwargs() -> dict:
    """Attributes for the session cookie (Set-Cookie). Host-only: no Domain attribute."""
    return {"httponly": True, "samesite": "lax", "secure": cookie_secure(), "path": "/", "max_age": SESSION_TTL}


def create_session(store, user_id: str, user_agent: str = "", auth_method: str = "password") -> str:
    """``auth_method``: "password" or "sso:<org_id>" (orgs that require SSO check it, see authz.py)."""
    token = new_session_token()
    store.create_session(session_hash(token), user_id, time.time() + SESSION_TTL, user_agent, auth_method)
    return token


def resolve_session(store, token: str | None) -> dict | None:
    """The signed-in user for a session cookie value (extending the sliding expiry), or None."""
    if not token or len(token) > 200:
        return None
    h = session_hash(token)
    sess = store.get_session(h)
    if not sess:
        return None
    user = store.get_user(sess["user_id"])
    if not user:
        return None
    now = time.time()
    if sess["expires_at"] - now < SESSION_TTL - SESSION_REFRESH_AFTER:
        store.extend_session(h, now + SESSION_TTL)
    return {**user, "auth_method": sess.get("auth_method") or "password"}


# --------------------------------------------------------------------------- login rate limiting
class LoginLimiter(Protocol):
    """Seam for a shared (e.g. Redis) limiter. ``check`` returns seconds until the key may try again (0 = ok)."""

    def check(self, key: str) -> float: ...

    def failure(self, key: str) -> None: ...

    def success(self, key: str) -> None: ...

    def reset(self) -> None: ...


class InMemoryLoginLimiter:
    """At most ``max_failures`` failed attempts per key in a sliding ``window`` (seconds). Single process only."""

    def __init__(self, max_failures: int = 10, window: float = 15 * 60):
        self.max_failures, self.window = max_failures, window
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def check(self, key: str) -> float:
        now = time.monotonic()
        with self._lock:
            q = self._prune(key, now)
            if len(q) >= self.max_failures:
                return max(1.0, self.window - (now - q[0]))
            return 0.0

    def failure(self, key: str) -> None:
        with self._lock:
            self._prune(key, time.monotonic()).append(time.monotonic())
            if len(self._hits) > 100_000:  # bound memory under a credential-stuffing flood
                self._hits.clear()

    def success(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


login_limiter: LoginLimiter = InMemoryLoginLimiter()


def limiter_key(email: str, ip: str | None) -> str:
    return f"{normalize_email(email)}|{ip or '-'}"


# --------------------------------------------------------------------------- trusted origins (CSRF / CORS)
def origin_of(url: str | None) -> str | None:
    if not url:
        return None
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return None
    return f"{p.scheme}://{p.netloc}".lower()


def allowed_origins() -> list[str]:
    raw = os.environ.get("ARCHFLOW_ALLOWED_ORIGINS", "http://localhost:5173")
    out: list[str] = []
    for o in [PUBLIC_URL, *raw.split(",")]:
        o = origin_of(o.strip())
        if o and o not in out:
            out.append(o)
    return out


def frontend_url() -> str:
    """Where the web app lives (first ARCHFLOW_ALLOWED_ORIGINS entry, else the API's public URL)."""
    raw = [o.strip() for o in os.environ.get("ARCHFLOW_ALLOWED_ORIGINS", "http://localhost:5173").split(",") if o.strip()]
    return (raw[0] if raw else PUBLIC_URL).rstrip("/")


def public_user(user: dict) -> dict:
    return {k: user.get(k) for k in ("id", "email", "name", "created_at", "last_login_at")}
