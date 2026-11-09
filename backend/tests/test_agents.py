"""The agent layer: versioned YAML prompts, Pydantic AI on the gateway, run traces (storage + admin API) and the
LLM suggestions endpoint. No network: the gateway's calls are faked."""

import os

os.environ.setdefault("ARCHFLOW_DB", ":memory:")
os.environ["ARCHFLOW_AI"] = "offline"

import pytest  # noqa: E402
from jinja2 import UndefinedError
from authhelp import new_client, signup  # noqa: E402

from archflow import agents, llm, suggestions  # noqa: E402
from archflow.agents import prompts, tracing  # noqa: E402
from archflow.main import create_app  # noqa: E402
from archflow.store import Store  # noqa: E402
from archflow.templates import build_template  # noqa: E402

from test_mcp_remote import BASE  # noqa: E402


# ---------------------------------------------------------------- prompts
def test_every_prompt_version_is_locked_and_loads():
    assert prompts.lock_problems() == []
    assert prompts.main(["check"]) == 0
    for entry in prompts.catalog():
        for v in entry["versions"]:
            p = prompts.get(entry["id"], v["version"])
            assert p.ref == f"{entry['id']}@{v['version']}" and p.system.strip() and p.group in ("reasoning", "fast")


def test_prompt_version_selection(monkeypatch):
    assert prompts.active_version("compare.evaluate") == "v1"
    with pytest.raises(prompts.PromptError):
        prompts.get("compare.evaluate", "v9")
    with pytest.raises(prompts.PromptError):
        prompts.get("no.such.prompt")
    monkeypatch.setenv("ARCHFLOW_PROMPTS", "compare.evaluate=v9, design.turn=v1")
    assert prompts.overrides() == {"compare.evaluate": "v9", "design.turn": "v1"}
    with pytest.raises(prompts.PromptError):  # a pin to a version that does not exist fails loudly
        prompts.get("compare.evaluate")
    monkeypatch.delenv("ARCHFLOW_PROMPTS")
    prompts.set_run_overrides({"design.turn": "v1"})
    try:
        assert prompts.active_version("design.turn") == "v1"
    finally:
        prompts.set_run_overrides({})


def test_changing_a_locked_prompt_is_detected(monkeypatch):
    lock = prompts.read_lock()
    monkeypatch.setattr(prompts, "read_lock", lambda: {**lock, "lld.design@v1": "0" * 16})
    assert any("lld.design@v1 changed" in p for p in prompts.lock_problems())


def test_templates_are_strict():
    p = prompts.get("memory.summarize")
    assert "<new_turns>\nhello\n</new_turns>" in p.render_user(summary="(none)", turns="hello")
    with pytest.raises(UndefinedError, match="turns"):
        p.render_user(summary="(none)")  # a missing variable is an error, not an empty string


# ---------------------------------------------------------------- runtime + tracing
def test_run_json_goes_through_the_gateway_with_attribution(monkeypatch):
    seen = {}

    def fake_complete_json(system, messages, schema, **kw):
        seen.update(system=system, messages=messages, schema=schema, **kw)
        llm._record(None, kw["group"], "gemini-test", 1000, 200, 12.0)  # what the real gateway does per call
        return {"facts": [{"text": "Deployment target is AWS.", "category": "decision"}]}

    usage = []
    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    monkeypatch.setattr(llm, "_usage_recorder", usage.append)
    from archflow.conversations import _FACTS_SCHEMA

    with tracing.listen(lambda t: None) as traces:
        out = agents.run_json("memory.extract", _FACTS_SCHEMA, variables={
            "existing": "(empty)", "user_text": "We deploy on AWS", "assistant_text": "Noted."})
    assert out["facts"][0]["text"] == "Deployment target is AWS."
    assert seen["group"] == "fast" and seen["max_tokens"] == 1000 and "long-term memory" in seen["system"]
    assert "User: We deploy on AWS" in seen["messages"][0]["content"]
    assert seen["schema"]["properties"]["facts"]["type"] == "array"
    assert usage[0]["prompt"] == "memory.extract@v1"
    (tr,) = traces
    assert tr["prompt"] == "memory.extract@v1" and tr["status"] == "ok" and tr["models"] == ["gemini-test"]
    assert tr["prompt_tokens"] == 1000 and tr["completion_tokens"] == 200 and tr["cost_usd"] > 0
    (span,) = tr["spans"]
    assert span["kind"] == "model" and span["cost_usd"] == tr["cost_usd"] and span["output"]["facts"]


def test_errors_keep_the_gateway_types_and_are_traced(monkeypatch):
    def down(*a, **k):
        raise llm.LLMUnavailable("all tiers down")

    monkeypatch.setattr(llm, "complete_tools", down)
    with tracing.listen(lambda t: None) as traces, pytest.raises(llm.LLMUnavailable):
        agents.run_text("memory.summarize", variables={"summary": "(none)", "turns": "x"})
    assert traces[0]["status"] == "error" and "all tiers down" in traces[0]["error"]


def test_stream_text_and_metadata_mode(monkeypatch):
    monkeypatch.setattr(llm, "stream_text", lambda system, messages, **kw: iter(["Hel", "lo"]))
    monkeypatch.setenv("ARCHFLOW_TRACES", "metadata")
    with tracing.listen(lambda t: None) as traces:
        chunks = list(agents.stream_text("assistant.ask", user="hi", history=[{"role": "user", "content": "q"},
                                                                            {"role": "assistant", "content": "a"}],
                                         variables={"project_context": "", "memory": "", "summary": ""}))
    assert chunks == ["Hel", "lo"]
    assert traces[0]["input"] is None and traces[0]["output"] is None  # no content kept in metadata mode


# ---------------------------------------------------------------- traces API + suggestions endpoint
@pytest.fixture()
def client():
    store = Store(":memory:")
    with new_client(create_app(store), base_url=BASE) as c:
        signup(c)
        yield c
    tracing.set_sink(None)


def test_suggestions_endpoint_and_traces_api(client, monkeypatch):
    pid = client.post("/api/projects", json={"name": "Support", "template": "ai-customer-support"}).json()["id"]
    org_id = client.get(f"/api/projects/{pid}").json()["project"]["org_id"]
    url = f"/api/projects/{pid}/ai/suggestions"
    assert client.post(url, json={"mode": "edit"}).json() == {"suggestions": [], "engine": "none"}  # offline

    calls = []

    def fake_complete_json(system, messages, schema, **kw):
        calls.append(messages[0]["content"])
        return {"suggestions": [
            {"label": "Add a dead-letter queue for failed Order Service events", "prompt": "", "why": "lost events"},
            {"label": "Add a dead-letter queue for failed Order Service events", "prompt": "dup"},
            {"label": "  ", "prompt": "empty"},
            {"label": "Trace REQ-003 to a component", "prompt": "Link REQ-003 to the component that implements it"}]}

    monkeypatch.setattr(llm, "available", lambda *a, **k: True)
    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    body = {"mode": "edit", "last_question": "What happens if the LLM is down?", "last_answer": "Requests fail."}
    r = client.post(url, json=body).json()
    assert r["engine"] == "llm" and [s["label"] for s in r["suggestions"]] == [
        "Add a dead-letter queue for failed Order Service events", "Trace REQ-003 to a component"]
    assert r["suggestions"][0]["prompt"] == r["suggestions"][0]["label"]  # empty prompt falls back to the label
    assert "Mode: edit" in calls[0] and "Order Service" in calls[0] and "<last_exchange>" in calls[0]
    assert client.post(url, json=body).json().get("cached") is True and len(calls) == 1
    assert client.post(url, json={"mode": "nope"}).status_code == 422

    traces = client.get(f"/api/orgs/{org_id}/ai/traces").json()
    assert traces["content"] == "full"
    (t,) = traces["traces"]
    assert t["prompt"] == "assistant.suggestions@v2" and t["feature"] == "suggestions" and t["project_id"] == pid
    assert t["project_name"] == "Support" and "input" not in t
    full = client.get(f"/api/orgs/{org_id}/ai/traces/{t['id']}").json()
    assert "Mode: edit" in full["input"]["user"] and full["output"]["suggestions"] and full["spans"][0]["kind"] == "model"
    assert client.get(f"/api/orgs/{org_id}/ai/traces/tr_nope").status_code == 404
    assert client.get(f"/api/orgs/{org_id}/ai/traces", params={"feature": "decisions"}).json()["traces"] == []
    catalog = client.get(f"/api/orgs/{org_id}/ai/prompts").json()
    assert catalog["problems"] == [] and any(p["id"] == "assistant.suggestions" for p in catalog["prompts"])


def test_project_summary_marks_untraced_requirements():
    from archflow.analysis import full_analysis

    state = build_template("ai-customer-support")
    s = suggestions.project_summary(state)
    untraced = set(full_analysis(state)["traceability"]["untraced_requirements"])
    assert untraced and {r["id"] for r in s["requirements"] if not r["traced"]} == untraced
    assert s["components"] and all(" -> " in c for c in s["connections"]) and s["open_issues"]


def test_traces_record_who_asked_what_and_nest_parallel_agents(client, monkeypatch):
    """Observability: every run says who asked, what they asked and what the AI answered; agents fired in parallel
    are listed under their parent run."""
    pid = client.post("/api/projects", json={"name": "Shop", "template": "ai-customer-support"}).json()["id"]
    org_id = client.get(f"/api/projects/{pid}").json()["project"]["org_id"]
    me = client.get("/api/me").json()["user"]
    monkeypatch.setattr(llm, "available", lambda *a, **k: True)

    def fake_complete_json(system, messages, schema, **kw):
        llm._record(org_id, "reasoning", "gemini-test", 400, 100, 7.0)
        return {"summary": "ok", "findings": [{"severity": "high", "title": "Orders DB is a single point of failure",
                                              "detail": "d", "components": ["Orders DB"], "fix": "Add a replica"}]}

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    assert client.post(f"/api/projects/{pid}/ai/review").status_code == 200
    listing = client.get(f"/api/orgs/{org_id}/ai/traces").json()
    (panel,) = listing["traces"]  # the specialists are not listed at the top level
    assert panel["agent"] == "review.panel" and panel["user_id"] == me["id"] and panel["user_name"] == me["name"]
    assert panel["question"] == "Review this architecture" and panel["feature"] == "review"
    assert panel["cost_usd"] > 0 and panel["prompt_tokens"] == 4 * 400  # includes its four agents
    assert {"id": me["id"], "name": me["name"]} in listing["people"]
    detail = client.get(f"/api/orgs/{org_id}/ai/traces/{panel['id']}").json()
    assert len(detail["children"]) == 4
    child = detail["children"][0]
    assert child["user_name"] == me["name"] and child["question"].startswith("Review: ")
    assert "Orders DB is a single point of failure" in child["answer"]
    everything = client.get(f"/api/orgs/{org_id}/ai/traces", params={"all_runs": "true"}).json()["traces"]
    assert len(everything) == 5
    mine = client.get(f"/api/orgs/{org_id}/ai/traces", params={"user_id": "u-nobody"}).json()["traces"]
    assert mine == []
    usage = client.get(f"/api/orgs/{org_id}/ai-usage").json()
    assert usage["by_user"][0]["name"] == me["name"] and usage["by_user"][0]["calls"] == 4
    assert {r["key"] for r in usage["by_prompt"]} == {"review.specialist@v2"}
