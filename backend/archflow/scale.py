"""Optional shared Redis so several API instances can run behind a load balancer.

Set ``ARCHFLOW_REDIS_URL`` (e.g. ``redis://redis:6379/0``) and every instance shares:

* **Rate limits** — the per-org LLM limit (``llm.org_limiter``) becomes a one-minute fixed window per org
  (``SET NX EX`` + ``INCR``, atomic; a refused call gives its slot back) and the sign-in limiter
  (``users.login_limiter``) counts failures per e-mail + IP in a window that starts at the first failure. Keys
  are hashed, so no e-mail address is stored in Redis.
* **Real-time fan-out** — ``RoomBus``: ``Hub.broadcast`` delivers to this instance's sockets and publishes the
  message on ``<prefix>:room:<project>:<branch>``. Every instance runs one subscriber (``PSUBSCRIBE
  <prefix>:room:*``) that delivers messages from *other* instances to its local sockets; messages carry the
  publishing instance id, so the originating instance never delivers twice. Presence lists are kept in a Redis
  hash per room, filtered by a heartbeat key per instance so a crashed instance's users disappear within
  ``HEARTBEAT_TTL`` seconds. The subscriber logs and retries (exponential backoff, capped) when Redis goes away.
* **Jobs** — ``job_store()``: status and progress of long jobs (repository analysis, document and meeting
  imports) live in a Redis hash per job with a TTL, so any instance can answer a status poll, and a cancel
  flag set on any instance is seen by the instance running the job. The work itself still runs where it was
  submitted (no distributed queue).
* **Locks** — ``lock(name)`` (the first-account bootstrap at sign-up).

Without ``ARCHFLOW_REDIS_URL`` nothing here touches the network: limiters, the hub and jobs stay in-process
exactly as before. When Redis is configured but fails, each piece degrades to its in-process behaviour for that
call (logged) instead of failing the request.

What stays per instance (by design): the LLM circuit breakers and router cooldowns, the AI budget cache
(``usage.Budgets``, at most ``CACHE_SECONDS`` stale), knowledge/code-graph caches (derived data). OAuth state
(pending consent, codes, tokens) is already in the database and the MCP endpoint is stateless HTTP.

Environment: ``ARCHFLOW_REDIS_URL``, ``ARCHFLOW_REDIS_PREFIX`` (``archflow``), ``ARCHFLOW_INSTANCE_ID`` (random).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from contextlib import contextmanager
from typing import Any, Awaitable, Callable, Iterator, Protocol

log = logging.getLogger("archflow.scale")

JOB_TTL = 24 * 3600
MAX_LOCAL_JOBS = 500
HEARTBEAT_TTL = 30
HEARTBEAT_EVERY = 10
PRESENCE_TTL = 24 * 3600


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def redis_url() -> str:
    return os.environ.get("ARCHFLOW_REDIS_URL", "").strip()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------- connection
class Backend:
    """One Redis: a sync client (limiters, jobs; called from worker threads) and async clients (pub/sub), which
    are created per event loop. Tests pass fakeredis clients / factories."""

    def __init__(self, url: str = "", *, prefix: str | None = None, sync_client: Any = None,
                 async_factory: Callable[[], Any] | None = None):
        self.url = url
        self.prefix = prefix or os.environ.get("ARCHFLOW_REDIS_PREFIX", "").strip() or "archflow"
        self._sync = sync_client
        self._async_factory = async_factory
        self._lock = threading.Lock()

    def key(self, *parts: str) -> str:
        return ":".join((self.prefix, *parts))

    @property
    def sync(self) -> Any:
        if self._sync is None:
            with self._lock:
                if self._sync is None:
                    import redis

                    self._sync = redis.Redis.from_url(self.url, decode_responses=True, socket_timeout=5,
                                                      socket_connect_timeout=5, health_check_interval=30)
        return self._sync

    def new_async(self) -> Any:
        if self._async_factory is not None:
            return self._async_factory()
        import redis.asyncio as aioredis

        return aioredis.Redis.from_url(self.url, decode_responses=True, socket_connect_timeout=5,
                                       health_check_interval=30)

    def ping(self) -> dict[str, Any]:
        t0 = time.monotonic()
        try:
            self.sync.ping()
            return {"configured": True, "ok": True, "latency_ms": round((time.monotonic() - t0) * 1000, 1)}
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return {"configured": True, "ok": False, "error": type(exc).__name__}


_backend: Backend | None = None
_backend_from_env = True
_backend_lock = threading.Lock()


def backend() -> Backend | None:
    """The shared Redis, or None (in-process mode). Built from ``ARCHFLOW_REDIS_URL`` on first use."""
    global _backend
    if _backend is None and _backend_from_env:
        with _backend_lock:
            if _backend is None and redis_url():
                _backend = Backend(redis_url())
    return _backend


def enabled() -> bool:
    return backend() is not None


def configure(be: Backend | None, *, from_env: bool = False) -> None:
    """Use ``be`` (None = in-process). ``from_env=True`` goes back to reading ARCHFLOW_REDIS_URL (tests)."""
    global _backend, _backend_from_env, _jobs
    _backend, _backend_from_env, _jobs = be, from_env, None


# --------------------------------------------------------------------------- rate limits
class RedisWindowLimiter:
    """``llm.RateLimiter`` shared by all instances: at most ``rpm`` calls per org per calendar minute.

    Falls back to ``fallback`` (the in-process token bucket) for a call when Redis fails."""

    def __init__(self, be: Backend, fallback: Any, rpm_env: str = "ARCHFLOW_ORG_RPM", default_rpm: int = 60,
                 name: str = "org"):
        self.be, self.fallback, self.rpm_env, self.default_rpm, self.name = be, fallback, rpm_env, default_rpm, name

    def try_acquire(self, key: str) -> float:
        rpm = max(1, _env_int(self.rpm_env, self.default_rpm))
        now = time.time()
        window = int(now // 60)
        k = self.be.key("rl", self.name, _hash(key), str(window))
        try:
            pipe = self.be.sync.pipeline()
            pipe.set(k, 0, ex=120, nx=True)
            pipe.incr(k)
            count = pipe.execute()[1]
            if count <= rpm:
                return 0.0
            self.be.sync.decr(k)  # a refused call does not use up the next retry's slot
            return max(0.05, (window + 1) * 60 - now)
        except Exception as exc:  # noqa: BLE001 - degrade to this instance's own bucket
            log.warning("redis rate limit unavailable (%s); using the in-process limiter", type(exc).__name__)
            return self.fallback.try_acquire(key)

    def reset(self) -> None:
        self.fallback.reset()
        _delete_matching(self.be, self.be.key("rl", self.name, "*"))


class RedisLoginLimiter:
    """``users.LoginLimiter`` shared by all instances: ``max_failures`` failed sign-ins per key within ``window``
    seconds of the first failure."""

    def __init__(self, be: Backend, fallback: Any, max_failures: int = 10, window: float = 15 * 60):
        self.be, self.fallback, self.max_failures, self.window = be, fallback, max_failures, int(window)

    def _k(self, key: str) -> str:
        return self.be.key("login", _hash(key))

    def check(self, key: str) -> float:
        try:
            k = self._k(key)
            n = self.be.sync.get(k)
            if n is not None and int(n) >= self.max_failures:
                return float(max(1, self.be.sync.ttl(k)))
            return 0.0
        except Exception as exc:  # noqa: BLE001
            log.warning("redis login limiter unavailable (%s); using the in-process limiter", type(exc).__name__)
            return self.fallback.check(key)

    def failure(self, key: str) -> None:
        try:
            pipe = self.be.sync.pipeline()
            pipe.set(self._k(key), 0, ex=self.window, nx=True)
            pipe.incr(self._k(key))
            pipe.execute()
        except Exception:  # noqa: BLE001
            self.fallback.failure(key)

    def success(self, key: str) -> None:
        self.fallback.success(key)
        try:
            self.be.sync.delete(self._k(key))
        except Exception:  # noqa: BLE001
            pass

    def reset(self) -> None:
        self.fallback.reset()
        _delete_matching(self.be, self.be.key("login", "*"))


def _delete_matching(be: Backend, pattern: str) -> None:
    try:
        keys = list(be.sync.scan_iter(match=pattern, count=500))
        if keys:
            be.sync.delete(*keys)
    except Exception:  # noqa: BLE001
        pass


def install() -> bool:
    """Swap the in-process limiters for shared ones when Redis is configured. Returns whether it did."""
    from . import llm, users

    be = backend()
    if be is None:
        return False
    if not isinstance(llm.org_limiter, RedisWindowLimiter):
        llm.org_limiter = RedisWindowLimiter(be, llm.org_limiter)
    if not isinstance(users.login_limiter, RedisLoginLimiter):
        users.login_limiter = RedisLoginLimiter(be, users.login_limiter)
    return True


# --------------------------------------------------------------------------- jobs
class JobStore(Protocol):
    def create(self, job_id: str, fields: dict[str, Any]) -> None: ...

    def update(self, job_id: str, **fields: Any) -> None: ...

    def get(self, job_id: str) -> dict[str, Any] | None: ...

    def cancel(self, job_id: str) -> dict[str, Any] | None: ...

    def cancelled(self, job_id: str) -> bool: ...


class LocalJobStore:
    """In-process registry (the behaviour without Redis): finished jobs are kept ``JOB_TTL`` seconds, at most
    ``MAX_LOCAL_JOBS`` in all."""

    def __init__(self) -> None:
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create(self, job_id: str, fields: dict[str, Any]) -> None:
        with self._lock:
            self._gc()
            self._jobs[job_id] = {"id": job_id, **fields}

    def update(self, job_id: str, **fields: Any) -> None:
        with self._lock:
            if job_id in self._jobs:
                self._jobs[job_id].update(fields)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if not job.get("finished_at"):
                job["cancel_requested"] = True
            return dict(job)

    def cancelled(self, job_id: str) -> bool:
        with self._lock:
            return bool((self._jobs.get(job_id) or {}).get("cancel_requested"))

    def _gc(self) -> None:
        cutoff = time.time() - JOB_TTL
        for jid in [j for j, v in self._jobs.items() if v.get("finished_at") and v["finished_at"] < cutoff]:
            del self._jobs[jid]
        while len(self._jobs) > MAX_LOCAL_JOBS:
            self._jobs.pop(next(iter(self._jobs)))


class RedisJobStore:
    """Job fields in a Redis hash ``<prefix>:job:<id>`` (JSON values) expiring ``JOB_TTL`` seconds after the last
    write. Jobs created here are mirrored in-process, so this instance still answers when Redis fails."""

    def __init__(self, be: Backend, ttl: int = JOB_TTL):
        self.be, self.ttl, self.local = be, ttl, LocalJobStore()

    def _k(self, job_id: str) -> str:
        return self.be.key("job", job_id)

    def _write(self, job_id: str, fields: dict[str, Any]) -> None:
        try:
            pipe = self.be.sync.pipeline()
            pipe.hset(self._k(job_id), mapping={k: json.dumps(v, default=str) for k, v in fields.items()})
            pipe.expire(self._k(job_id), self.ttl)
            pipe.execute()
        except Exception as exc:  # noqa: BLE001
            log.warning("redis job store write failed for %s (%s)", job_id, type(exc).__name__)

    def create(self, job_id: str, fields: dict[str, Any]) -> None:
        self.local.create(job_id, fields)
        self._write(job_id, {"id": job_id, **fields})

    def update(self, job_id: str, **fields: Any) -> None:
        self.local.update(job_id, **fields)
        try:
            if not self.be.sync.exists(self._k(job_id)):  # expired or unknown: do not resurrect a partial hash
                return
        except Exception:  # noqa: BLE001
            return
        self._write(job_id, fields)

    def get(self, job_id: str) -> dict[str, Any] | None:
        try:
            raw = self.be.sync.hgetall(self._k(job_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("redis job store read failed for %s (%s)", job_id, type(exc).__name__)
            return self.local.get(job_id)
        if not raw:
            return self.local.get(job_id)
        return {k: json.loads(v) for k, v in raw.items()}

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        job = self.get(job_id)
        if job is None:
            return None
        if not job.get("finished_at"):
            self.local.cancel(job_id)
            self._write(job_id, {"cancel_requested": True})
            job["cancel_requested"] = True
        return job

    def cancelled(self, job_id: str) -> bool:
        if self.local.cancelled(job_id):
            return True
        try:
            return self.be.sync.hget(self._k(job_id), "cancel_requested") == "true"
        except Exception:  # noqa: BLE001
            return False


_jobs: JobStore | None = None
_jobs_lock = threading.Lock()


def job_store() -> JobStore:
    global _jobs
    if _jobs is None:
        with _jobs_lock:
            if _jobs is None:
                be = backend()
                _jobs = RedisJobStore(be) if be else LocalJobStore()
    return _jobs


def new_job_id(kind: str = "job") -> str:
    return f"{kind}-{secrets.token_hex(6)}"


def cancel_check(job_id: str, local: Callable[[], bool], store: JobStore | None = None,
                 every: float = 0.5) -> Callable[[], bool]:
    """A ``cancelled()`` callable for a running job: true once ``local()`` is (connection closed here) or the
    job's cancel flag is set on any instance. The shared flag is read at most every ``every`` seconds."""
    store = store or job_store()
    state = {"at": 0.0, "hit": False}

    def cancelled() -> bool:
        if local() or state["hit"]:
            return True
        now = time.monotonic()
        if now - state["at"] >= every:
            state["at"] = now
            state["hit"] = store.cancelled(job_id)
        return state["hit"]

    return cancelled


# --------------------------------------------------------------------------- locks
@contextmanager
def lock(name: str, local: Any, timeout: float = 10.0) -> Iterator[None]:
    """``local`` (a threading lock) plus, with Redis, a lock shared by all instances (``SET NX PX`` with a token;
    released only by its holder). Best effort: if Redis fails or the wait exceeds ``timeout``, only the local lock
    is held."""
    with local:
        be = backend()
        key, token, held = "", secrets.token_hex(8), False
        if be is not None:
            key = be.key("lock", name)
            deadline = time.monotonic() + timeout
            try:
                while not (held := bool(be.sync.set(key, token, nx=True, px=int(timeout * 1000)))):
                    if time.monotonic() > deadline:
                        log.warning("redis lock %s: still held elsewhere after %.0f s, continuing", name, timeout)
                        break
                    time.sleep(0.05)
            except Exception as exc:  # noqa: BLE001
                log.warning("redis lock %s unavailable (%s)", name, type(exc).__name__)
                held = False
        try:
            yield
        finally:
            if held:
                try:
                    with be.sync.pipeline() as pipe:
                        pipe.watch(key)
                        if pipe.get(key) == token:
                            pipe.multi()
                            pipe.delete(key)
                            pipe.execute()
                        else:
                            pipe.unwatch()
                except Exception:  # noqa: BLE001 - it expires on its own
                    pass


# --------------------------------------------------------------------------- real-time fan-out
Deliver = Callable[[str, dict[str, Any]], Awaitable[None]]


class RoomBus:
    """Cross-instance fan-out for ``main.Hub``. Without Redis every method is a no-op / returns the local view."""

    def __init__(self, be: Backend | None = None, *, instance_id: str | None = None):
        self.be = be if be is not None else backend()
        self.instance_id = instance_id or os.environ.get("ARCHFLOW_INSTANCE_ID") or secrets.token_hex(6)
        self.connected = False
        self._client: Any = None
        self._client_loop: Any = None
        self._task: asyncio.Task[None] | None = None
        self._beat_at = 0.0
        self.failures = 0

    @property
    def distributed(self) -> bool:
        return self.be is not None

    def _channel_prefix(self) -> str:
        return self.be.key("room", "") if self.be else ""

    def _aclient(self) -> Any:
        loop = asyncio.get_running_loop()
        if self._client is None or self._client_loop is not loop:
            self._client, self._client_loop = self.be.new_async(), loop
        return self._client

    # ---- publishing ------------------------------------------------------------------------------------
    async def publish(self, room: str, msg: dict[str, Any]) -> None:
        if not self.be:
            return
        try:
            await self._aclient().publish(self._channel_prefix() + room,
                                          json.dumps({"o": self.instance_id, "m": msg}, default=str))
        except Exception as exc:  # noqa: BLE001 - local sockets already got it
            log.warning("redis publish to %s failed (%s)", room, type(exc).__name__)

    # ---- presence ---------------------------------------------------------------------------------------
    def _presence_key(self, room: str) -> str:
        return self.be.key("presence", room)

    def _instance_key(self, instance: str) -> str:
        return self.be.key("instance", instance)

    async def _heartbeat(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._beat_at >= HEARTBEAT_EVERY:
            self._beat_at = now
            await self._aclient().set(self._instance_key(self.instance_id), "1", ex=HEARTBEAT_TTL)

    async def present(self, room: str, client_id: int, user: dict[str, Any]) -> None:
        if not self.be:
            return
        try:
            await self._heartbeat(force=True)
            c = self._aclient()
            await c.hset(self._presence_key(room), f"{self.instance_id}:{client_id}",
                         json.dumps({**user, "client_id": client_id}))
            await c.expire(self._presence_key(room), PRESENCE_TTL)
        except Exception as exc:  # noqa: BLE001
            log.warning("redis presence update failed (%s)", type(exc).__name__)

    async def absent(self, room: str, client_id: int) -> None:
        if not self.be:
            return
        try:
            await self._aclient().hdel(self._presence_key(room), f"{self.instance_id}:{client_id}")
        except Exception as exc:  # noqa: BLE001
            log.warning("redis presence update failed (%s)", type(exc).__name__)

    async def presence(self, room: str, local: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Everyone in the room on every live instance (``local`` when Redis is off or fails)."""
        if not self.be:
            return local
        try:
            c = self._aclient()
            entries = await c.hgetall(self._presence_key(room))
            instances = sorted({f.split(":", 1)[0] for f in entries})
            alive = dict(zip(instances, await c.mget([self._instance_key(i) for i in instances]))) if instances else {}
            dead = [f for f in entries if not alive.get(f.split(":", 1)[0])]
            if dead:
                await c.hdel(self._presence_key(room), *dead)
            return [json.loads(v) for f, v in sorted(entries.items()) if f not in dead]
        except Exception as exc:  # noqa: BLE001
            log.warning("redis presence read failed (%s)", type(exc).__name__)
            return local

    # ---- subscribing -------------------------------------------------------------------------------------
    async def handle(self, message: dict[str, Any], deliver: Deliver) -> bool:
        """Deliver one pub/sub message locally unless this instance published it. Returns whether it delivered."""
        if message.get("type") not in ("pmessage", "message"):
            return False
        try:
            data = json.loads(message["data"])
            channel = message["channel"]
            room = channel[len(self._channel_prefix()):]
            if data.get("o") == self.instance_id:
                return False
            msg = data["m"]
            if isinstance(msg, dict) and msg.get("type") == "presence":
                # Presence messages can arrive out of order: send the current list, not the publisher's snapshot.
                msg = {**msg, "users": await self.presence(room, msg.get("users") or [])}
            await deliver(room, msg)
            return True
        except Exception:  # noqa: BLE001 - one bad message never stops the subscriber
            log.exception("redis fan-out: could not deliver a message")
            return False

    async def listen(self, deliver: Deliver, *, max_backoff: float = 30.0) -> None:
        """Subscribe to every room channel and deliver forever; reconnects with backoff when Redis fails."""
        delay = 0.5
        while True:
            pubsub = None
            try:
                pubsub = self._aclient().pubsub()
                await pubsub.psubscribe(self._channel_prefix() + "*")
                await self._heartbeat(force=True)
                if self.failures:
                    log.info("redis fan-out reconnected (instance %s)", self.instance_id)
                self.connected, delay = True, 0.5
                while True:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    await self._heartbeat()
                    if msg is not None:
                        await self.handle(msg, deliver)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - log and retry
                self.connected = False
                self.failures += 1
                self._client = None  # reconnect with a fresh client
                log.warning("redis fan-out subscriber failed (%s: %s); retrying in %.1f s",
                            type(exc).__name__, exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, max_backoff)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.aclose()
                    except Exception:  # noqa: BLE001
                        pass

    def start(self, deliver: Deliver) -> None:
        if self.be and self._task is None:
            self._task = asyncio.get_running_loop().create_task(self.listen(deliver), name="archflow-room-bus")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.connected = False
        if self._client is not None and self.be:
            try:
                await self._client.delete(self._instance_key(self.instance_id))
                await self._client.aclose()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def status(self) -> dict[str, Any]:
        return {"instance": self.instance_id, "subscriber": "connected" if self.connected else
                ("starting" if self._task is not None and not self.failures else
                 ("reconnecting" if self._task is not None else "stopped"))}


# --------------------------------------------------------------------------- health + routes
def status(bus: RoomBus | None = None, *, ping: bool = False) -> dict[str, Any]:
    """Redis status for /api/health (no network call) and /api/ready (``ping=True``)."""
    be = backend()
    if be is None:
        return {"configured": False, "mode": "in-process"}
    out: dict[str, Any] = be.ping() if ping else {"configured": True}
    if bus is not None:
        out.update(bus.status())
    return out


def register_routes(app: Any, *, authz: Any) -> None:
    """GET /api/projects/{pid}/jobs/{job_id} (status) and POST …/cancel, answered by any instance."""
    from fastapi import Depends, HTTPException

    read, edit = authz.project("read"), authz.project("edit")

    def scoped(pid: str, job_id: str) -> dict[str, Any]:
        job = job_store().get(job_id)
        if not job or job.get("project_id") != pid:
            raise HTTPException(404, "job not found")
        return job

    def public(job: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if k != "local_path"}

    @app.get("/api/projects/{pid}/jobs/{job_id}")
    def job_status(pid: str, job_id: str, a=Depends(read)):
        return public(scoped(pid, job_id))

    @app.post("/api/projects/{pid}/jobs/{job_id}/cancel")
    def job_cancel(pid: str, job_id: str, a=Depends(edit)):
        scoped(pid, job_id)
        return public(job_store().cancel(job_id) or {})
