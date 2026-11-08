"""Redis scale-out (scale.py), simulated with fakeredis: two "instances" = two clients on one fake server."""

import asyncio
import json
import threading
import time

import fakeredis
import pytest
from authhelp import login, new_client, signup, ws_headers

from archflow import llm, scale, users
from archflow.codegraph.jobs import JobRunner
from archflow.main import Hub, create_app
from archflow.store import Store


def backend(server, **kw) -> scale.Backend:
    return scale.Backend("redis://fake", sync_client=fakeredis.FakeRedis(server=server, decode_responses=True),
                         async_factory=lambda: fakeredis.FakeAsyncRedis(server=server, decode_responses=True), **kw)


class Broken:
    """A Redis client whose every call fails, like one whose server went away."""

    def __getattr__(self, name):
        def fail(*a, **k):
            raise ConnectionError("redis down")
        return fail


@pytest.fixture()
def server():
    return fakeredis.FakeServer()


@pytest.fixture(autouse=True)
def _restore():
    org, login_ = llm.org_limiter, users.login_limiter
    yield
    llm.org_limiter, users.login_limiter = org, login_
    scale.configure(None, from_env=True)


# ---------------------------------------------------------------- no URL: in-process, exactly as before
def test_without_redis_url_everything_stays_in_process(monkeypatch):
    monkeypatch.delenv("ARCHFLOW_REDIS_URL", raising=False)
    scale.configure(None, from_env=True)
    org, login_ = llm.org_limiter, users.login_limiter
    assert not scale.enabled() and scale.install() is False
    assert llm.org_limiter is org and users.login_limiter is login_
    assert isinstance(llm.org_limiter, llm.InMemoryTokenBucket)
    assert isinstance(scale.job_store(), scale.LocalJobStore)
    bus = scale.RoomBus()
    assert not bus.distributed
    assert scale.status(bus) == {"configured": False, "mode": "in-process"}
    with scale.lock("x", threading.Lock()):
        pass

    async def run():
        await bus.publish("p:main", {"type": "x"})  # no-op
        return await bus.presence("p:main", [{"name": "A"}])
    assert asyncio.run(run()) == [{"name": "A"}]


def test_url_from_env_builds_the_backend(monkeypatch):
    monkeypatch.setenv("ARCHFLOW_REDIS_URL", "redis://example:6379/0")
    scale.configure(None, from_env=True)
    assert scale.enabled() and scale.backend().url == "redis://example:6379/0"
    assert isinstance(scale.job_store(), scale.RedisJobStore)


# ---------------------------------------------------------------- rate limits
def test_org_rate_limit_is_shared_between_instances(server, monkeypatch):
    monkeypatch.setenv("ARCHFLOW_ORG_RPM", "3")
    a = scale.RedisWindowLimiter(backend(server), llm.InMemoryTokenBucket())
    b = scale.RedisWindowLimiter(backend(server), llm.InMemoryTokenBucket())
    assert a.try_acquire("org-1") == 0 and b.try_acquire("org-1") == 0 and a.try_acquire("org-1") == 0
    wait = b.try_acquire("org-1")
    assert 0 < wait <= 60
    assert a.try_acquire("org-1") > 0  # refusals give their slot back, but the window stays full
    assert a.try_acquire("org-2") == 0  # other orgs are separate
    a.reset()
    assert b.try_acquire("org-1") == 0


def test_org_rate_limit_falls_back_to_the_local_bucket_when_redis_fails(monkeypatch):
    monkeypatch.setenv("ARCHFLOW_ORG_RPM", "1")
    lim = scale.RedisWindowLimiter(scale.Backend("redis://x", sync_client=Broken()), llm.InMemoryTokenBucket())
    assert lim.try_acquire("org") == 0 and lim.try_acquire("org") > 0


def test_login_limiter_is_shared_and_hashes_keys(server):
    a = scale.RedisLoginLimiter(backend(server), users.InMemoryLoginLimiter(), max_failures=3, window=60)
    b = scale.RedisLoginLimiter(backend(server), users.InMemoryLoginLimiter(), max_failures=3, window=60)
    key = users.limiter_key("Someone@Example.com", "10.0.0.1")
    a.failure(key)
    b.failure(key)
    assert a.check(key) == 0
    a.failure(key)
    assert 0 < b.check(key) <= 60
    assert not any("example" in k.lower() for k in fakeredis.FakeRedis(server=server, decode_responses=True).keys())
    b.success(key)
    assert a.check(key) == 0


def test_install_swaps_in_shared_limiters(server):
    scale.configure(backend(server))
    assert scale.install() is True
    assert isinstance(llm.org_limiter, scale.RedisWindowLimiter)
    assert isinstance(users.login_limiter, scale.RedisLoginLimiter)
    scale.install()  # idempotent
    assert isinstance(llm.org_limiter.fallback, llm.InMemoryTokenBucket)


# ---------------------------------------------------------------- jobs
def test_job_state_and_cancel_flag_are_visible_across_instances(server):
    a, b = scale.RedisJobStore(backend(server)), scale.RedisJobStore(backend(server))
    a.create("import-1", {"project_id": "p1", "status": "running", "progress": {"stage": "upload"},
                          "finished_at": None})
    a.update("import-1", progress={"stage": "read", "done": 2, "total": 5})
    job = b.get("import-1")
    assert job["status"] == "running" and job["progress"] == {"stage": "read", "done": 2, "total": 5}
    assert fakeredis.FakeRedis(server=server).ttl("archflow:job:import-1") > 0
    checker = scale.cancel_check("import-1", lambda: False, a, every=0)
    assert not checker() and not a.cancelled("import-1")
    assert b.cancel("import-1")["cancel_requested"] is True
    assert a.cancelled("import-1") and checker()
    assert b.get("nope") is None and b.cancel("nope") is None
    b.update("nope", status="done")  # never resurrects an unknown job
    assert b.get("nope") is None


def test_job_store_keeps_answering_from_its_own_mirror_when_redis_fails():
    store = scale.RedisJobStore(scale.Backend("redis://x", sync_client=Broken()))
    store.create("j", {"status": "running", "finished_at": None})
    store.update("j", status="done")
    assert store.get("j")["status"] == "done"


def test_codegraph_job_status_and_cancel_from_another_instance(server, tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHFLOW_ALLOW_LOCAL_REPOS", "1")
    started, release = threading.Event(), threading.Event()

    def worker(repo, workdir):
        started.set()
        release.wait(10)
        return {"nodes": [], "edges": []}

    class NoStore:
        def add_code_graph(self, *a, **k):  # pragma: no cover - cancelled before storing
            raise AssertionError("cancelled job must not be stored")

    a = JobRunner(NoStore(), worker=worker, jobs=scale.RedisJobStore(backend(server)))
    b = JobRunner(NoStore(), worker=worker, jobs=scale.RedisJobStore(backend(server)))
    job = a.submit("p1", "Owner", local_path=str(tmp_path))
    assert started.wait(10)
    seen = b.get(job["id"])
    assert seen["status"] == "running" and seen["phase"] == "analysing" and "local_path" not in seen
    assert b.cancel(job["id"])["cancel_requested"] is True
    release.set()
    for _ in range(200):
        if (b.get(job["id"]) or {}).get("status") == "cancelled":
            break
        time.sleep(0.02)
    assert b.get(job["id"])["status"] == "cancelled"
    a.shutdown()
    b.shutdown()


# ---------------------------------------------------------------- real-time fan-out
class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, data):
        self.sent.append(json.loads(data))


async def _until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


def test_broadcast_reaches_other_instances_once(server):
    async def run():
        hub_a = Hub(scale.RoomBus(backend(server), instance_id="a"))
        hub_b = Hub(scale.RoomBus(backend(server), instance_id="b"))
        for h in (hub_a, hub_b):
            h.bus.start(h.deliver)
        await _until(lambda: hub_a.bus.connected and hub_b.bus.connected)
        wa, wb, other = FakeWS(), FakeWS(), FakeWS()
        await hub_a.join("p1:main", wa, {"name": "Ann", "color": "#111"})
        await hub_b.join("p1:main", wb, {"name": "Bob", "color": "#222"})
        await hub_b.join("p2:main", other, {"name": "Cy", "color": "#333"})
        # presence spans both instances
        def last_presence(ws):
            return {u["name"] for u in [m for m in ws.sent if m["type"] == "presence"][-1]["users"]}
        await _until(lambda: last_presence(wa) == last_presence(wb) == {"Ann", "Bob"})

        await hub_a.broadcast("p1:main", {"type": "state", "rev": 7})
        await _until(lambda: any(m.get("rev") == 7 for m in wb.sent))
        await hub_a.broadcast("p1:main", {"type": "sentinel"})
        await _until(lambda: any(m["type"] == "sentinel" for m in wb.sent))
        assert [m for m in wa.sent if m.get("rev") == 7] == [{"type": "state", "rev": 7}]  # no double delivery
        assert len([m for m in wb.sent if m.get("rev") == 7]) == 1
        assert not any(m.get("rev") == 7 for m in other.sent)  # other rooms are untouched

        # cursor relays skip the sender but reach the other instance
        await hub_b.broadcast("p1:main", {"type": "cursor", "x": 1}, exclude=wb)
        await _until(lambda: any(m["type"] == "cursor" for m in wa.sent))
        assert not any(m["type"] == "cursor" for m in wb.sent)

        await hub_b.leave("p1:main", wb)
        await _until(lambda: any(m["type"] == "left" for m in wa.sent))
        await hub_a.bus.stop()
        await hub_b.bus.stop()
        # a stopped instance's heartbeat is gone, so its users drop out of presence
        assert await scale.RoomBus(backend(server), instance_id="c").presence("p1:main", []) == []

    asyncio.run(run())


def test_subscriber_logs_and_reconnects_after_failures(server, caplog):
    calls = {"n": 0}

    class FlakyPubSub:
        async def psubscribe(self, *a):
            raise ConnectionError("redis down")

        async def aclose(self):
            pass

    class Flaky:
        def pubsub(self):
            return FlakyPubSub()

    def factory():
        calls["n"] += 1
        return Flaky() if calls["n"] == 1 else fakeredis.FakeAsyncRedis(server=server, decode_responses=True)

    async def run():
        bus = scale.RoomBus(scale.Backend("redis://fake", sync_client=fakeredis.FakeRedis(server=server),
                                          async_factory=factory), instance_id="x")
        got = []

        async def deliver(room, msg):
            got.append((room, msg))

        bus.start(deliver)
        await _until(lambda: bus.connected, timeout=10)
        assert bus.failures == 1
        await bus.handle({"type": "pmessage", "channel": "archflow:room:p:main",
                          "data": json.dumps({"o": "y", "m": {"type": "t"}})}, deliver)
        await bus.handle({"type": "pmessage", "channel": "archflow:room:p:main", "data": "not json"}, deliver)
        await bus.stop()
        return got

    assert asyncio.run(run()) == [("p:main", {"type": "t"})]
    assert any("subscriber failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------- whole app: two API instances, one Redis
def test_two_app_instances_share_events_jobs_and_health(server, monkeypatch):
    scale.configure(backend(server))
    store = Store(":memory:")
    app_a, app_b = create_app(store), create_app(store)
    with new_client(app_a) as a, new_client(app_b) as b:
        signup(a)
        login(b, "owner@example.com")
        pid = a.post("/api/projects", json={"name": "Scale", "template": "blank"}).json()["id"]
        health = b.get("/api/health").json()["redis"]
        assert health["configured"] is True and health["instance"] == app_b.state.hub.bus.instance_id
        assert b.get("/api/ready").json()["redis"]["ok"] is True

        with b.websocket_connect(f"/ws/{pid}/main", headers=ws_headers(b)) as ws:
            assert ws.receive_json()["type"] == "hello"
            assert ws.receive_json()["type"] == "presence"
            for _ in range(100):
                if app_a.state.hub.bus.connected:
                    break
                time.sleep(0.02)
            r = a.post(f"/api/projects/{pid}/ops", json={"ops": [{"op": "add_requirement", "requirement": {
                "title": "Scale out"}}]})
            assert r.status_code == 200, r.text
            msg = ws.receive_json()
            while msg["type"] != "state":
                msg = ws.receive_json()
            assert any(rq["title"] == "Scale out" for rq in msg["state"]["requirements"].values())

        # an import streamed by instance A is visible (and cancellable) on instance B
        r = a.post(f"/api/projects/{pid}/requirements/import", params={"filename": "n.md"},
                   content=b"# Requirements\n- The system shall scale.\n", headers={"Accept": "text/event-stream"})
        job_id = r.headers["x-job-id"]
        assert f'"job_id": "{job_id}"' in r.text
        job = b.get(f"/api/projects/{pid}/jobs/{job_id}").json()
        assert job["status"] == "done" and job["kind"] == "document_import" and job["progress"]["stage"]
        assert b.get(f"/api/projects/other/jobs/{job_id}").status_code in (403, 404)
        assert b.post(f"/api/projects/{pid}/jobs/{job_id}/cancel").json()["status"] == "done"
