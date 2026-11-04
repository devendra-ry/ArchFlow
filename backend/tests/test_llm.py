"""Tests for the LLM gateway. No network: the LiteLLM router is replaced with a scripted fake."""

import json
from types import SimpleNamespace

import pytest

from archflow import ai, llm

KEY_ENVS = ["GEMINI_API_KEY", "GEMINI_API_KEYS", "GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "ARCHFLOW_MODEL_REASONING", "ARCHFLOW_MODEL_FAST", "ARCHFLOW_FALLBACK_OPENAI",
            "ARCHFLOW_FALLBACK_ANTHROPIC", "LANGFUSE_PUBLIC_KEY", "ARCHFLOW_ORG_RPM", "ARCHFLOW_LLM_MAX_ATTEMPTS"]


# --------------------------------------------------------------------------- fakes
class RateLimitError(Exception):
    status_code = 429

    def __init__(self, retry_after=None):
        super().__init__("429 Too Many Requests")
        self.response = SimpleNamespace(headers={"retry-after": str(retry_after)} if retry_after is not None else {})


class InternalServerError(Exception):
    status_code = 500


class ContentPolicyViolationError(Exception):
    status_code = 400


class BadRequestError(Exception):
    status_code = 400


def response(content="", tool_calls=None, finish="stop", model="fake"):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason=finish)], model=model,
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def tool_call(name, arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


class FakeRouter:
    """Each script entry is an exception (raised) or a response (returned); keyed by router model_name."""

    def __init__(self, scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = []

    def completion(self, model, messages, **kwargs):
        self.calls.append({"model": model, "messages": messages, **kwargs})
        item = self.scripts[model].pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in KEY_ENVS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ARCHFLOW_AI", "auto")
    sleeps = []
    monkeypatch.setattr(llm.time, "sleep", lambda s: sleeps.append(s))
    llm._reset()
    yield sleeps
    llm._reset()


@pytest.fixture
def all_keys(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test")
    monkeypatch.setenv("OPENAI_API_KEY", "o-test")


def install(monkeypatch, scripts):
    router = FakeRouter(scripts)
    monkeypatch.setattr(llm, "_get_router", lambda: router)
    return router


# --------------------------------------------------------------------------- availability / config
def test_available_false_without_keys():
    assert llm.available() is False
    st = llm.status()
    assert st["available"] is False and st["groups"] == {"reasoning": [], "fast": []}
    with pytest.raises(llm.LLMUnavailable):
        llm.complete_tools("sys", [{"role": "user", "content": "hi"}], [])


def test_available_respects_offline_and_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-test")
    assert llm.available() is True
    assert llm.status()["groups"]["reasoning"] == ["anthropic/claude-opus-5"]
    monkeypatch.setenv("ARCHFLOW_AI", "offline")
    assert llm.available() is False


def test_group_chain_and_multiple_gemini_keys(monkeypatch, all_keys):
    monkeypatch.setenv("GEMINI_API_KEYS", "k1, k2,g-test")
    tiers = llm._tiers("reasoning")
    assert [t.model for t in tiers] == [llm.DEFAULT_MODELS["reasoning"]["primary"], "anthropic/claude-opus-5",
                                        llm.DEFAULT_MODELS["reasoning"]["openai"]]
    assert tiers[0].keys == ("k1", "k2", "g-test")
    model_list, fallbacks = llm._router_config()
    gemini = [d for d in model_list if d["model_name"] == "reasoning"]
    assert len(gemini) == 3  # one deployment per key, load balanced by the router
    assert {"reasoning": ["reasoning#2", "reasoning#3"]} in fallbacks
    assert "k1" not in json.dumps(llm.status())  # keys never exposed


# --------------------------------------------------------------------------- resilience
def test_backoff_retries_then_succeeds(monkeypatch, all_keys, clean_env):
    router = install(monkeypatch, {"reasoning": [RateLimitError(), RateLimitError(), response("done")]})
    text, calls = llm.complete_tools("sys", [{"role": "user", "content": "hi"}], [])
    assert text == "done" and calls == []
    assert [c["model"] for c in router.calls] == ["reasoning"] * 3
    assert all(c["fallbacks"] == [] for c in router.calls)
    sleeps = clean_env
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= 0.5 and 0 <= sleeps[1] <= 1.0  # full jitter, base 0.5


def test_retry_after_is_honoured(monkeypatch, all_keys, clean_env):
    install(monkeypatch, {"reasoning": [RateLimitError(retry_after=3), response("ok")]})
    llm.complete_tools("sys", [{"role": "user", "content": "hi"}], [])
    assert clean_env == [3.0]


def test_long_retry_after_redispatches_immediately(monkeypatch, all_keys, clean_env):
    router = install(monkeypatch, {"reasoning": [RateLimitError(retry_after=600)], "reasoning#2": [response("claude")]})
    text, _ = llm.complete_tools("sys", [{"role": "user", "content": "hi"}], [])
    assert text == "claude" and clean_env == []
    assert [c["model"] for c in router.calls] == ["reasoning", "reasoning#2"]


def test_fallback_to_next_model_after_exhausting_retries(monkeypatch, all_keys):
    monkeypatch.setenv("ARCHFLOW_LLM_MAX_ATTEMPTS", "3")
    router = install(monkeypatch, {
        "reasoning": [RateLimitError()] * 3,
        "reasoning#2": [InternalServerError("boom")] * 3,
        "reasoning#3": [response("from openai")],
    })
    text, _ = llm.complete_tools("sys", [{"role": "user", "content": "hi"}], [])
    assert text == "from openai"
    assert [c["model"] for c in router.calls] == ["reasoning"] * 3 + ["reasoning#2"] * 3 + ["reasoning#3"]


def test_all_exhausted_raises_unavailable(monkeypatch, all_keys):
    monkeypatch.setenv("ARCHFLOW_LLM_MAX_ATTEMPTS", "2")
    install(monkeypatch, {"reasoning": [RateLimitError()] * 2, "reasoning#2": [RateLimitError()] * 2,
                          "reasoning#3": [RateLimitError()] * 2})
    with pytest.raises(llm.LLMUnavailable, match="All LLM providers failed"):
        llm.complete_tools("sys", [{"role": "user", "content": "hi"}], [])


def test_circuit_breaker_opens(monkeypatch, all_keys):
    monkeypatch.setenv("ARCHFLOW_LLM_BREAKER_FAILS", "3")
    monkeypatch.setenv("ARCHFLOW_LLM_MAX_ATTEMPTS", "5")
    primary = llm.DEFAULT_MODELS["reasoning"]["primary"]
    router = install(monkeypatch, {"reasoning": [InternalServerError()] * 10,
                                   "reasoning#2": [response("a"), response("b")]})
    assert llm.complete_tools("sys", [{"role": "user", "content": "x"}], [])[0] == "a"
    # breaker opened after 3 consecutive failures (not all 5 attempts were used)
    assert [c["model"] for c in router.calls].count("reasoning") == 3
    assert llm.status()["circuits"][primary] == "open"
    # next call skips the open primary entirely
    router.calls.clear()
    assert llm.complete_tools("sys", [{"role": "user", "content": "x"}], [])[0] == "b"
    assert [c["model"] for c in router.calls] == ["reasoning#2"]


def test_circuit_breaker_half_opens_after_cooldown(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])
    b = llm.CircuitBreaker(threshold=2, cooldown=30)
    b.failure(); b.failure()
    assert not b.allow()
    clock[0] += 31
    assert b.allow()          # half-open trial
    b.failure()               # trial fails -> open again
    assert not b.allow()
    clock[0] += 31
    assert b.allow(); b.success()
    assert b.state() == "closed"


def test_auth_error_moves_to_next_provider(monkeypatch, all_keys):
    class AuthenticationError(Exception):
        status_code = 401
    router = install(monkeypatch, {"reasoning": [AuthenticationError()], "reasoning#2": [response("ok")]})
    assert llm.complete_tools("s", [{"role": "user", "content": "x"}], [])[0] == "ok"
    assert len(router.calls) == 2


def test_content_policy_raises_refused(monkeypatch, all_keys):
    install(monkeypatch, {"reasoning": [ContentPolicyViolationError()]})
    with pytest.raises(llm.LLMRefused):
        llm.complete_tools("s", [{"role": "user", "content": "x"}], [])
    install(monkeypatch, {"reasoning": [response("", finish="content_filter")]})
    with pytest.raises(llm.LLMRefused):
        llm.complete_tools("s", [{"role": "user", "content": "x"}], [])


def test_real_litellm_exception_classification():
    import httpx
    import litellm
    req = httpx.Request("POST", "https://example.invalid")
    exc = litellm.RateLimitError("slow down", llm_provider="gemini", model="gemini/x",
                                 response=httpx.Response(429, headers={"retry-after": "7"}, request=req))
    assert llm._classify(exc) == "rate_limit"
    assert llm._retry_after(exc) == 7.0
    assert llm._classify(litellm.Timeout("t", model="m", llm_provider="p")) == "transient"
    assert llm._classify(litellm.ContentPolicyViolationError("no", model="m", llm_provider="p")) == "refused"
    assert llm._classify(litellm.AuthenticationError("bad key", llm_provider="p", model="m")) == "auth"


# --------------------------------------------------------------------------- org rate limiter
def test_org_rate_limiter(monkeypatch, all_keys, clean_env):
    monkeypatch.setenv("ARCHFLOW_ORG_RPM", "2")
    clock = [0.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])
    install(monkeypatch, {"reasoning": [response("1"), response("2"), response("3")]})
    msgs = [{"role": "user", "content": "x"}]
    llm.complete_tools("s", msgs, [], org_id="acme")
    llm.complete_tools("s", msgs, [], org_id="acme")
    # bucket empty; refill needs 30s > ARCHFLOW_ORG_RATE_WAIT (2s) -> refuse without a long wait
    with pytest.raises(llm.LLMUnavailable, match="rate limit for org"):
        llm.complete_tools("s", msgs, [], org_id="acme")
    # another org is unaffected
    assert llm.complete_tools("s", msgs, [], org_id="other")[0] == "3"


def test_org_rate_limiter_waits_briefly(monkeypatch, clean_env):
    monkeypatch.setenv("ARCHFLOW_ORG_RPM", "60")  # 1 token / second
    clock = [0.0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])

    def fake_sleep(s):
        clean_env.append(s)
        clock[0] += s
    monkeypatch.setattr(llm.time, "sleep", fake_sleep)
    for _ in range(60):
        llm._acquire_org("acme")
    llm._acquire_org("acme")  # waits ~1s for a refill, then proceeds
    assert clean_env and 0 < clean_env[0] <= 1.0


# --------------------------------------------------------------------------- structured output
SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}, "n": {"type": "integer"}},
          "required": ["name", "n"]}


def test_complete_json_strips_code_fences(monkeypatch, all_keys):
    router = install(monkeypatch, {"reasoning": [response('```json\n{"name": "api", "n": 2}\n```')]})
    out = llm.complete_json("sys", [{"role": "user", "content": "x"}], SCHEMA)
    assert out == {"name": "api", "n": 2}
    rf = router.calls[0]["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["schema"] == SCHEMA
    assert router.calls[0]["messages"][0] == {"role": "system", "content": "sys"}


def test_complete_json_retries_once_on_invalid(monkeypatch, all_keys):
    router = install(monkeypatch, {"reasoning": [response("not json at all"), response('{"name": "x", "n": 1}')]})
    assert llm.complete_json("sys", [{"role": "user", "content": "x"}], SCHEMA) == {"name": "x", "n": 1}
    retry_msgs = router.calls[1]["messages"]
    assert retry_msgs[-2]["role"] == "assistant" and "invalid" in retry_msgs[-1]["content"]


def test_complete_json_schema_violation_then_gives_up(monkeypatch, all_keys):
    router = install(monkeypatch, {"reasoning": [response('{"name": "x"}'), response('{"name": "x", "n": "two"}')]})
    with pytest.raises(llm.LLMUnavailable):
        llm.complete_json("sys", [{"role": "user", "content": "x"}], SCHEMA)
    assert len(router.calls) == 2
    assert "'n' is a required property" in router.calls[1]["messages"][-1]["content"]


def test_complete_json_falls_back_to_prompt_schema_on_bad_request(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    router = install(monkeypatch, {"reasoning": [BadRequestError("schema unsupported"), response('{"name":"a","n":1}')]})
    assert llm.complete_json("sys", [{"role": "user", "content": "x"}], SCHEMA) == {"name": "a", "n": 1}
    assert "response_format" not in router.calls[1]
    assert "JSON Schema" in router.calls[1]["messages"][0]["content"]


# --------------------------------------------------------------------------- tools
def test_complete_tools_parses_arguments(monkeypatch, all_keys):
    router = install(monkeypatch, {"reasoning": [response("Added a cache.", tool_calls=[
        tool_call("add_component", '{"name": "Redis", "type": "cache"}'),
        tool_call("connect_components", {"source": "API", "target": "Redis"}),
        tool_call("broken", "{not json"),
    ])]})
    text, calls = llm.complete_tools("sys", [{"role": "user", "content": "x"}], ai.TOOLS)
    assert text == "Added a cache."
    assert calls == [{"name": "add_component", "arguments": {"name": "Redis", "type": "cache"}},
                     {"name": "connect_components", "arguments": {"source": "API", "target": "Redis"}}]
    assert router.calls[0]["tools"] == ai.TOOLS and router.calls[0]["tool_choice"] == "auto"


def test_ai_tools_are_openai_format():
    names = [t["function"]["name"] for t in ai.TOOLS]
    assert "add_component" in names and "ask_clarifying_questions" in names
    assert all(t["type"] == "function" and t["function"]["parameters"]["type"] == "object" for t in ai.TOOLS)


def test_ai_llm_command_maps_tool_calls(monkeypatch, all_keys):
    from archflow.models import ProjectState
    install(monkeypatch, {"reasoning": [response("ok", tool_calls=[
        tool_call("add_component", '{"name": "Redis", "type": "cache", "x": 1, "y": 2}'),
        tool_call("ask_clarifying_questions", '{"questions": ["Which region?"]}'),
    ])]})
    res = ai.llm_command(ProjectState(), "add redis", [])
    assert res.ops == [{"op": "add_node", "node": {"name": "Redis", "type": "cache", "position": {"x": 1, "y": 2}}}]
    assert res.questions == ["Which region?"] and res.engine == "llm"


def test_ai_maps_unavailable(monkeypatch):
    from archflow.models import ProjectState
    with pytest.raises(ai.AIUnavailable):
        ai.claude_ask("ctx", "q")
    monkeypatch.setattr(ai, "MODE", "auto")
    res = ai.run_command(ProjectState(), "add Redis", [])  # no keys -> offline parser
    assert res.engine == "offline" and res.ops


# --------------------------------------------------------------------------- streaming
def chunk(text=None, finish=None):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=text), finish_reason=finish)])


def stream(*items):
    """A provider stream: strings become chunks, exceptions are raised when reached."""
    for item in items:
        if isinstance(item, BaseException):
            raise item
        yield chunk(item)


def test_stream_text_yields_chunks(monkeypatch, all_keys):
    router = install(monkeypatch, {"reasoning": [stream("Hel", "lo ", None, "world")]})
    # Text is forwarded word by word: redaction holds back a partial word until it is complete (redact.StreamRestorer)
    assert list(llm.stream_text("sys", [{"role": "user", "content": "hi"}])) == ["Hello ", "world"]
    assert router.calls[0]["stream"] is True


def test_stream_text_accepts_a_whole_response(monkeypatch, all_keys):
    install(monkeypatch, {"reasoning": [response("all at once")]})
    assert list(llm.stream_text("sys", [{"role": "user", "content": "hi"}])) == ["all at once"]


def test_stream_failure_before_text_retries_without_streaming(monkeypatch, all_keys):
    router = install(monkeypatch, {"reasoning": [stream(RateLimitError()), response("recovered")]})
    assert list(llm.stream_text("sys", [{"role": "user", "content": "hi"}])) == ["recovered"]
    assert router.calls[0]["stream"] is True and "stream" not in router.calls[1]


def test_stream_failure_after_text_raises(monkeypatch, all_keys):
    install(monkeypatch, {"reasoning": [stream("partial ", InternalServerError())]})
    out = []
    with pytest.raises(llm.LLMUnavailable, match="interrupted"):
        for piece in llm.stream_text("sys", [{"role": "user", "content": "hi"}]):
            out.append(piece)
    assert out == ["partial "]


def test_stream_refusal(monkeypatch, all_keys):
    install(monkeypatch, {"reasoning": [iter([chunk("I can"), chunk(None, finish="content_filter")])]})
    with pytest.raises(llm.LLMRefused):
        list(llm.stream_text("sys", [{"role": "user", "content": "hi"}]))


def test_refusal_reason_names_the_problem_without_the_key():
    from archflow.llm import refusal_reason
    billing = Exception('GeminiException BadRequestError - {"error": {"code": 403, "message": "There are issues with '
                        'your billing account, please resolve / pay your outstanding balance"}}')
    assert refusal_reason(billing) == "the provider account has a billing problem"
    assert refusal_reason(Exception("API key not valid. Please pass a valid API key.")) == "the API key was rejected"
    assert refusal_reason(Exception("PermissionDeniedError: forbidden")) == "the key has no permission for this model"
    assert refusal_reason(Exception("something else")) == "the key was refused"
