"""Provider-agnostic LLM gateway (LiteLLM Router; Gemini primary, Claude and OpenAI as fallbacks).

Public API (synchronous; callers wrap these in ``asyncio.to_thread``):

    available() -> bool
    status() -> dict
    complete_json(system, messages, schema, *, group="reasoning", org_id=None, max_tokens=8000) -> dict
    complete_tools(system, messages, tools, *, group="reasoning", org_id=None, max_tokens=8000) -> (text, calls)
    stream_text(system, messages, *, group="reasoning", org_id=None, max_tokens=8000) -> Iterator[str]
    LLMUnavailable, LLMRefused

Resilience is layered:

1. **LiteLLM Router** (inside one provider tier): ``num_retries``, a minimum ``retry_after`` with the
   provider's ``Retry-After`` header honoured, per-deployment cooldowns (``allowed_fails`` /
   ``cooldown_time``) and load balancing across several Gemini API keys (``GEMINI_API_KEYS``).
   Router-level ``fallbacks`` are configured too (for anyone using the router directly).
2. **Our outer wrapper** (``_dispatch``) drives the provider tiers explicitly: exponential backoff with full
   jitter that honours ``Retry-After``, a per-model circuit breaker, and an explicit re-dispatch to the next
   tier once a tier is exhausted. We call each tier with ``fallbacks=[]`` so we always know which model
   failed (needed for the breaker) and so the known LiteLLM gap where some 429s skip router fallbacks
   cannot strand a request.
3. **Per-org token bucket** in front of everything so a single tenant cannot burn the shared quota.

Environment:
    ARCHFLOW_AI                     auto | claude | llm | offline ("offline" disables this module)
    ARCHFLOW_MODEL_REASONING        primary model for the "reasoning" group (default newest Gemini Pro)
    ARCHFLOW_MODEL_FAST             primary model for the "fast" group (default newest Gemini Flash)
    ARCHFLOW_FALLBACK_ANTHROPIC[_FAST]  Claude fallback per group
    ARCHFLOW_FALLBACK_OPENAI[_FAST]     OpenAI fallback per group
    GEMINI_API_KEYS (comma separated), GEMINI_API_KEY, GOOGLE_API_KEY, ANTHROPIC_API_KEY, OPENAI_API_KEY
    ARCHFLOW_ORG_RPM (60), ARCHFLOW_ORG_RATE_WAIT seconds (2)
    ARCHFLOW_LLM_MAX_ATTEMPTS (5), ARCHFLOW_LLM_BACKOFF_BASE (0.5), ARCHFLOW_LLM_BACKOFF_CAP (20),
    ARCHFLOW_LLM_DEADLINE seconds (120), ARCHFLOW_LLM_ROUTER_RETRIES (2), ARCHFLOW_LLM_TIMEOUT (120)
    ARCHFLOW_LLM_BREAKER_FAILS (5), ARCHFLOW_LLM_BREAKER_COOLDOWN seconds (30)
    ARCHFLOW_REDACT                 on (PII and secrets) | secrets | off: what ``redact`` removes from requests
    LANGFUSE_PUBLIC_KEY             enables LiteLLM's langfuse callback when set

Per-org keys (BYOK): ``set_org_key_resolver`` (wired in main.create_app to the encrypted org_ai_keys table) lets
a call made with ``org_id`` use that org's own provider keys through a per-org Router (rebuilt when the keys
change); providers the org has not configured fall back to the environment keys above.

Before a request leaves, ``_dispatch`` replaces personal data and secrets in its messages with placeholders
(``redact``) and puts the personal data back into the answer; secrets never come back, and secrets in an answer are
masked. Untrusted blocks and user/tool messages are scanned for prompt injection (``untrusted``). Counts and
signals go to ``redact.observe`` observers (the agent traces), never the values.

Logging goes to the ``archflow.llm`` logger. API keys and prompt contents are never logged.
"""

from __future__ import annotations

import email.utils
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from collections import Counter, OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol

from . import ai_providers, redact, untrusted

log = logging.getLogger("archflow.llm")

__all__ = ["LLMUnavailable", "LLMRefused", "LLMInvalidOutput", "available", "status",
           "complete_json", "complete_tools", "stream_text", "set_org_key_resolver", "invalidate_org",
           "LLMBudgetExceeded", "usage_scope", "set_usage_scope", "set_usage_hooks", "budget_block", "estimate_cost",
           "set_org_config_resolver", "test_connection", "observe_calls"]


# Thinking models (Gemini 3.x, Claude, GPT-5) spend output tokens on reasoning before answering; a small
# max_tokens can leave nothing for the answer. Never go below this floor.
MIN_OUTPUT_TOKENS = int(os.environ.get("ARCHFLOW_LLM_MIN_OUTPUT_TOKENS", 2048))


class LLMUnavailable(RuntimeError):
    """No provider configured/reachable, or all retries and fallbacks are exhausted."""


class LLMRefused(RuntimeError):
    """The provider refused the request on content-policy grounds."""


class LLMInvalidOutput(LLMUnavailable):
    """The model kept returning output that does not match the requested schema."""


# --------------------------------------------------------------------------- configuration
# Defaults verified against litellm's model_prices_and_context_window map (litellm 1.103.x).
DEFAULT_MODELS: dict[str, dict[str, str]] = {
    "reasoning": {
        "primary": "gemini/gemini-3.1-pro-preview",  # newest Gemini Pro in the map (no GA 3.x Pro yet)
        "anthropic": "anthropic/claude-opus-5",
        "openai": "openai/gpt-5.6",
    },
    "fast": {
        "primary": "gemini/gemini-3.8-flash",  # newest GA Gemini Flash in the map
        "anthropic": "anthropic/claude-haiku-4-5",
        "openai": "openai/gpt-5.4-mini",
    },
}
GROUPS = tuple(DEFAULT_MODELS)

_KEY_ENVS = {"gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "anthropic": ("ANTHROPIC_API_KEY",),
             "openai": ("OPENAI_API_KEY",)}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _mode() -> str:
    return os.environ.get("ARCHFLOW_AI", "auto").strip().lower()


def _provider(model: str) -> str:
    prefix = model.split("/", 1)[0] if "/" in model else ""
    if prefix in ("gemini", "anthropic", "openai"):
        return prefix
    if prefix:
        return prefix  # vertex_ai, bedrock, ollama, ... : credentials resolved by LiteLLM itself
    low = model.lower()
    if low.startswith("gemini"):
        return "gemini"
    if low.startswith("claude"):
        return "anthropic"
    if low.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    return "unknown"


def _keys(provider: str, org_keys: dict[str, str] | None = None) -> list[str | None]:
    """API keys for a provider. ``[None]`` means "let LiteLLM resolve credentials" (other providers).

    A key the org configured for this provider (``org_keys``) replaces the environment's keys."""
    if org_keys and org_keys.get(provider):
        return [org_keys[provider]]
    if provider == "gemini":
        keys = [k.strip() for k in os.environ.get("GEMINI_API_KEYS", "").split(",")]
        keys += [os.environ.get(e, "").strip() for e in _KEY_ENVS["gemini"]]
        seen: list[str | None] = []
        for k in keys:
            if k and k not in seen:
                seen.append(k)
        return seen
    if provider in _KEY_ENVS:
        key = next((os.environ[e].strip() for e in _KEY_ENVS[provider] if os.environ.get(e, "").strip()), "")
        return [key] if key else []
    if provider in ai_providers.PROVIDERS:  # openrouter, mistral, groq, azure, openai_compatible, ...
        key = (org_keys or {}).get(provider) or ai_providers.env_key(provider)
        if provider == "openai_compatible":
            return [key or ai_providers.NO_KEY]
        return [key] if key else []
    return [None]


def _group_models(group: str) -> list[str]:
    """Ordered, de-duplicated model chain for a group (before filtering on configured keys)."""
    if group not in DEFAULT_MODELS:
        raise ValueError(f"unknown model group {group!r}; expected one of {GROUPS}")
    d = DEFAULT_MODELS[group]
    suffix = "" if group == "reasoning" else f"_{group.upper()}"
    chain = [
        os.environ.get(f"ARCHFLOW_MODEL_{group.upper()}") or d["primary"],
        os.environ.get(f"ARCHFLOW_FALLBACK_ANTHROPIC{suffix}") or d["anthropic"],
        os.environ.get(f"ARCHFLOW_FALLBACK_OPENAI{suffix}") or d["openai"],
    ]
    out: list[str] = []
    for m in chain:
        if m and m not in out:
            out.append(m)
    return out


@dataclass(frozen=True)
class Tier:
    name: str            # router model_name, e.g. "reasoning" or "reasoning#2"
    model: str           # LiteLLM model id, e.g. "gemini/gemini-3.1-pro-preview"
    keys: tuple          # API keys (never logged)
    params: tuple = ()   # extra LiteLLM params, e.g. (("api_base", "https://..."), ("api_version", "..."))
    provider: str = ""   # ArchFlow provider id when chosen in an org's AI config


def _tiers(group: str, org_keys: dict[str, str] | None = None, org_cfg: dict[str, Any] | None = None) -> list[Tier]:
    """The group's provider chain: the org's own (AI settings) when it chose models, else the server defaults.
    Tiers without usable credentials are left out."""
    tiers: list[Tier] = []
    chain = (org_cfg or {}).get(group) or []
    if chain:
        for i, entry in enumerate(chain):
            provider = entry["provider"]
            if provider not in ai_providers.PROVIDERS:
                continue
            params = (org_cfg or {}).get("params", {}).get(provider, {})
            if any(not params.get(r) for r in ai_providers.PROVIDERS[provider].get("required_params", [])):
                continue
            keys = _keys(provider, org_keys)
            if keys:
                tiers.append(Tier(group if i == 0 else f"{group}#{i + 1}", ai_providers.litellm_model(provider, entry["model"]),
                                  tuple(keys), tuple(sorted(params.items())), provider))
        return tiers
    for i, model in enumerate(_group_models(group)):
        keys = _keys(_provider(model), org_keys)
        if keys:
            tiers.append(Tier(group if i == 0 else f"{group}#{i + 1}", model, tuple(keys)))
    return tiers


def primary_model(group: str = "reasoning") -> str:
    """The configured primary model id for a group (whether or not its key is set)."""
    return _group_models(group)[0]


# --------------------------------------------------------------------------- per-org keys (BYOK)
OrgKeyResolver = Callable[[str], "dict[str, str]"]
_org_key_resolver: OrgKeyResolver | None = None


def set_org_key_resolver(resolver: OrgKeyResolver | None) -> None:
    """Register ``resolver(org_id) -> {provider: api_key}`` (decrypted org keys; see secrets_box.py).

    When a call passes ``org_id`` and the org has a key for a provider, that key is used for the call instead of
    the environment's key(s) for the same provider. Providers without an org key fall back to the environment."""
    global _org_key_resolver
    _org_key_resolver = resolver


OrgConfigResolver = Callable[[str], "dict[str, Any]"]
_org_config_resolver: OrgConfigResolver | None = None


def set_org_config_resolver(resolver: OrgConfigResolver | None) -> None:
    """Register ``resolver(org_id) -> {"reasoning": [{provider, model}], "fast": [...], "params": {...}}`` (the
    org's model choices from AI settings, see ai_providers.normalize_config). Empty chains mean server defaults."""
    global _org_config_resolver
    _org_config_resolver = resolver


def _org_config(org_id: str | None) -> dict[str, Any]:
    if not org_id or _org_config_resolver is None:
        return {}
    try:
        return _org_config_resolver(org_id) or {}
    except Exception:  # noqa: BLE001 - a broken config store must not take AI down
        log.exception("could not load the AI config of org %s; using server defaults", org_id)
        return {}


def _org_keys(org_id: str | None) -> dict[str, str]:
    if not org_id or _org_key_resolver is None:
        return {}
    try:
        return {k: v for k, v in (_org_key_resolver(org_id) or {}).items() if v}
    except Exception:  # noqa: BLE001 - a broken key store must not take AI down; env keys still work
        log.exception("could not load AI keys for org %s; using environment keys", org_id)
        return {}


def available(org_id: str | None = None) -> bool:
    """True when a provider is configured (environment or the org's own keys) and the org's monthly AI budget is
    not used up, so callers fall back to their offline paths when it is."""
    if _mode() == "offline":
        return False
    org_keys, org_cfg = _org_keys(org_id), _org_config(org_id)
    if not any(_tiers(g, org_keys, org_cfg) for g in GROUPS):
        return False
    return budget_block(org_id) is None


# --------------------------------------------------------------------------- usage, cost and budgets
class LLMBudgetExceeded(LLMUnavailable):
    """The organization's monthly AI budget is used up."""


# The request's attribution, set by the API per request (main.py middleware): which project and feature an LLM
# call is made for. asyncio.to_thread copies context, so worker threads see it too.
_usage_scope: ContextVar[dict[str, str]] = ContextVar("archflow_llm_usage_scope", default={})


@contextmanager
def usage_scope(**fields: str) -> Iterator[None]:
    token = _usage_scope.set({**_usage_scope.get(), **{k: v for k, v in fields.items() if v}})
    try:
        yield
    finally:
        _usage_scope.reset(token)


def current_scope() -> dict[str, str]:
    """The attribution (project_id, feature, prompt) of calls made in the current context."""
    return dict(_usage_scope.get())


def set_usage_scope(**fields: str) -> None:
    """Set the attribution for the rest of the current context (one request)."""
    _usage_scope.set({**_usage_scope.get(), **{k: v for k, v in fields.items() if v}})


UsageRecorder = Callable[[dict[str, Any]], None]
BudgetCheck = Callable[[str], "str | None"]  # org_id -> reason it is blocked, or None
_usage_recorder: UsageRecorder | None = None
_budget_check: BudgetCheck | None = None

# USD per million (input, output) tokens, used when LiteLLM does not know a model's price. Keys are matched as
# substrings of the model name, longest first. Override or extend with ARCHFLOW_LLM_PRICES='{"model": [in, out]}'.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "gemini-3.8-flash": (0.30, 2.50), "gemini-3.1-pro": (2.00, 12.00), "flash": (0.30, 2.50), "gemini": (1.25, 10.00),
    "claude-opus": (15.00, 75.00), "claude-sonnet": (3.00, 15.00), "claude-haiku": (1.00, 5.00), "claude": (3.00, 15.00),
    "gpt-5-mini": (0.25, 2.00), "gpt-5": (1.25, 10.00), "gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00),
}


def set_usage_hooks(recorder: UsageRecorder | None, budget_check: BudgetCheck | None) -> None:
    """``recorder(event)`` stores one call's usage; ``budget_check(org_id)`` returns why the org may not call
    models right now (monthly budget used up), or None. Wired to the store in main.create_app."""
    global _usage_recorder, _budget_check
    _usage_recorder, _budget_check = recorder, budget_check


def budget_block(org_id: str | None) -> str | None:
    if not org_id or _budget_check is None:
        return None
    try:
        return _budget_check(org_id)
    except Exception:  # noqa: BLE001 - a broken budget store must not take AI down
        log.exception("budget check failed for org %s", org_id)
        return None


def _prices() -> dict[str, tuple[float, float]]:
    prices = dict(DEFAULT_PRICES)
    raw = os.environ.get("ARCHFLOW_LLM_PRICES", "")
    if raw:
        try:
            prices.update({k: (float(v[0]), float(v[1])) for k, v in json.loads(raw).items()})
        except (ValueError, TypeError, IndexError):
            log.warning("ARCHFLOW_LLM_PRICES is not valid JSON {model: [input, output]}; using defaults")
    return prices


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int, resp: Any = None) -> tuple[float, str]:
    """(USD, source): LiteLLM's price list when it knows the model, else ARCHFLOW_LLM_PRICES / the defaults."""
    if resp is not None:
        try:
            import litellm

            cost = float(litellm.completion_cost(completion_response=resp) or 0)
            if cost > 0:
                return cost, "litellm"
        except Exception:  # noqa: BLE001 - unknown model or response shape
            pass
    name = (model or "").lower()
    prices = _prices()
    key = next((k for k in sorted(prices, key=len, reverse=True) if k.lower() in name), None)
    if key is None:
        return 0.0, "unknown"
    pin, pout = prices[key]
    return (prompt_tokens * pin + completion_tokens * pout) / 1_000_000, "table"


# Observers of every model call made in the current context (agents.tracing uses them to attach the model,
# tokens and cost of each call to the trace of the agent run that made it).
CallObserver = Callable[[dict[str, Any]], None]
_call_observers: ContextVar[tuple[CallObserver, ...]] = ContextVar("archflow_llm_call_observers", default=())


@contextmanager
def observe_calls(observer: CallObserver) -> Iterator[None]:
    token = _call_observers.set((*_call_observers.get(), observer))
    try:
        yield
    finally:
        _call_observers.reset(token)


def _record(org_id: str | None, group: str, model: str, prompt_tokens: int, completion_tokens: int,
            latency_ms: float, resp: Any = None, estimated: bool = False) -> None:
    observers = _call_observers.get()
    if _usage_recorder is None and not observers:
        return
    cost, source = estimate_cost(model, prompt_tokens, completion_tokens, resp)
    scope = _usage_scope.get()
    event = {"org_id": org_id or "", "project_id": scope.get("project_id", ""),
             "feature": scope.get("feature", "other"), "prompt": scope.get("prompt", ""),
             "user_id": scope.get("user_id", ""), "group": group,
             "model": model, "prompt_tokens": int(prompt_tokens or 0), "completion_tokens": int(completion_tokens or 0),
             "cost_usd": round(cost, 6), "cost_source": "estimate" if estimated else source,
             "latency_ms": int(latency_ms)}
    for observe in observers:
        try:
            observe(event)
        except Exception:  # noqa: BLE001 - tracing must never fail a model call
            log.exception("LLM call observer failed")
    if _usage_recorder is None:
        return
    try:
        _usage_recorder(event)
    except Exception:  # noqa: BLE001 - never fail a model call because usage could not be stored
        log.exception("recording LLM usage failed")


def status(org_id: str | None = None) -> dict:
    org_keys, org_cfg = _org_keys(org_id), _org_config(org_id)
    return {
        "mode": _mode(),
        "groups": {g: [t.model for t in _tiers(g, org_keys, org_cfg)] for g in GROUPS},
        "available": available(org_id),
        "circuits": {m: b.state() for m, b in list(_breakers.items())},
    }


# --------------------------------------------------------------------------- router
_router_lock = threading.Lock()
_router: Any = None
_router_sig: str | None = None
_callbacks_set = False
# Routers for orgs with their own keys: org_id -> (config signature, Router). A key change produces a new
# signature, so the org's router is rebuilt on its next call (and the old one, holding the old key, dropped).
_org_routers: "OrderedDict[str, tuple[str, Any]]" = OrderedDict()
MAX_ORG_ROUTERS = 256


def _router_config(org_keys: dict[str, str] | None = None,
                   org_cfg: dict[str, Any] | None = None) -> tuple[list[dict], list[dict]]:
    model_list: list[dict] = []
    fallbacks: list[dict] = []
    timeout = _env_float("ARCHFLOW_LLM_TIMEOUT", 120)
    for g in GROUPS:
        tiers = _tiers(g, org_keys, org_cfg)
        for t in tiers:
            for i, key in enumerate(t.keys):
                params: dict[str, Any] = {"model": t.model, "timeout": timeout, **dict(t.params)}
                if key:
                    params["api_key"] = key
                model_list.append({"model_name": t.name, "litellm_params": params,
                                   "model_info": {"id": f"{t.name}:{t.model}:{i}"}})
        if len(tiers) > 1:
            fallbacks.append({tiers[0].name: [t.name for t in tiers[1:]]})
    return model_list, fallbacks


def _build_router(model_list: list[dict], fallbacks: list[dict]) -> Any:
    global _callbacks_set
    import litellm
    from litellm import Router

    if os.environ.get("LANGFUSE_PUBLIC_KEY") and not _callbacks_set:
        for attr in ("success_callback", "failure_callback"):
            cbs = getattr(litellm, attr)
            if "langfuse" not in cbs:
                cbs.append("langfuse")
        _callbacks_set = True
    return Router(
        model_list=model_list,
        num_retries=_env_int("ARCHFLOW_LLM_ROUTER_RETRIES", 2),
        retry_after=1,              # minimum wait; provider Retry-After headers take precedence
        allowed_fails=3,            # failures/minute before a deployment is cooled down
        cooldown_time=30,
        fallbacks=fallbacks,        # used by direct router callers; _dispatch drives tiers itself
        timeout=_env_float("ARCHFLOW_LLM_TIMEOUT", 120),
        routing_strategy="simple-shuffle",
    )


def _get_router(org_keys: dict[str, str] | None = None, org_id: str | None = None,
                org_cfg: dict[str, Any] | None = None):
    """The shared LiteLLM Router (rebuilt when env config changes), or an org's own router when it has its own
    keys or model choices."""
    global _router, _router_sig
    model_list, fallbacks = _router_config(org_keys, org_cfg)
    sig = hashlib.sha256(json.dumps([model_list, fallbacks], sort_keys=True).encode()).hexdigest()
    with _router_lock:
        if (org_keys or _has_chains(org_cfg)) and org_id:
            cached = _org_routers.get(org_id)
            if cached and cached[0] == sig:
                _org_routers.move_to_end(org_id)
                return cached[1]
            router = _build_router(model_list, fallbacks)
            _org_routers[org_id] = (sig, router)
            _org_routers.move_to_end(org_id)
            while len(_org_routers) > MAX_ORG_ROUTERS:
                _org_routers.popitem(last=False)
            log.info("LLM router configured for org %s (own keys for %s; models %s)", org_id, sorted(org_keys or {}),
                     {g: [t.model for t in _tiers(g, org_keys, org_cfg)] for g in GROUPS})
            return router
        if _router is not None and sig == _router_sig:
            return _router
        _router = _build_router(model_list, fallbacks)
        _router_sig = sig
        log.info("LLM router configured: %s", {g: [t.model for t in _tiers(g)] for g in GROUPS})
        return _router


def _has_chains(org_cfg: dict[str, Any] | None) -> bool:
    return bool(org_cfg) and any(org_cfg.get(g) for g in GROUPS)


def invalidate_org(org_id: str) -> None:
    """Forget an org's router (called when its keys change)."""
    with _router_lock:
        _org_routers.pop(org_id, None)


def _reset() -> None:
    """Drop cached router, breakers and rate-limit buckets (tests / config reloads)."""
    global _router, _router_sig
    with _router_lock:
        _router, _router_sig = None, None
        _org_routers.clear()
    _breakers.clear()
    org_limiter.reset()


# --------------------------------------------------------------------------- circuit breaker
class CircuitBreaker:
    """Opens after ``threshold`` consecutive failures; half-opens (one trial) after ``cooldown`` seconds."""

    def __init__(self, threshold: int, cooldown: float):
        self.threshold, self.cooldown = threshold, cooldown
        self.failures = 0
        self.opened_at: float | None = None
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            if self.opened_at is None:
                return True
            if time.monotonic() - self.opened_at >= self.cooldown:
                # half-open: let one trial through; another failure re-opens immediately
                self.opened_at = None
                self.failures = self.threshold - 1
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        with self._lock:
            self.failures += 1
            if self.failures >= self.threshold and self.opened_at is None:
                self.opened_at = time.monotonic()

    def state(self) -> str:
        with self._lock:
            if self.opened_at is None:
                return "closed"
            return "open" if time.monotonic() - self.opened_at < self.cooldown else "half_open"


_breakers: dict[str, CircuitBreaker] = {}
_breakers_lock = threading.Lock()


def _breaker(model: str) -> CircuitBreaker:
    with _breakers_lock:
        b = _breakers.get(model)
        if b is None:
            b = _breakers[model] = CircuitBreaker(_env_int("ARCHFLOW_LLM_BREAKER_FAILS", 5),
                                                  _env_float("ARCHFLOW_LLM_BREAKER_COOLDOWN", 30))
        return b


# --------------------------------------------------------------------------- per-org rate limit
class RateLimiter(Protocol):
    """Seam for a shared (Redis) implementation: return seconds to wait (0 = token taken), never block."""

    def try_acquire(self, key: str) -> float: ...

    def reset(self) -> None: ...


class InMemoryTokenBucket:
    """Token bucket per key; capacity = rpm, refills at rpm/60 tokens per second. Single-process only."""

    def __init__(self, rpm_env: str = "ARCHFLOW_ORG_RPM", default_rpm: int = 60):
        self.rpm_env, self.default_rpm = rpm_env, default_rpm
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last_refill)
        self._lock = threading.Lock()

    def try_acquire(self, key: str) -> float:
        rpm = max(1, _env_int(self.rpm_env, self.default_rpm))
        rate = rpm / 60.0
        now = time.monotonic()
        with self._lock:
            tokens, last = self._buckets.get(key, (float(rpm), now))
            tokens = min(float(rpm), tokens + (now - last) * rate)
            if tokens >= 1:
                self._buckets[key] = (tokens - 1, now)
                return 0.0
            self._buckets[key] = (tokens, now)
            return (1 - tokens) / rate

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


org_limiter: RateLimiter = InMemoryTokenBucket()


def _acquire_org(org_id: str | None) -> None:
    if not org_id:
        return
    max_wait = _env_float("ARCHFLOW_ORG_RATE_WAIT", 2.0)
    waited = 0.0
    while True:
        wait = org_limiter.try_acquire(org_id)
        if wait <= 0:
            return
        if waited + wait > max_wait:
            log.warning("org rate limit exceeded org=%s", org_id)
            raise LLMUnavailable("rate limit for org")
        time.sleep(wait)
        waited += wait


# --------------------------------------------------------------------------- error classification
def _names(exc: BaseException) -> set[str]:
    return {c.__name__ for c in type(exc).__mro__}


_LAST_REFUSAL: dict[str, str] = {}  # breaker key -> why the provider last refused the key (shown while the circuit is open)


def refusal_reason(exc: BaseException) -> str:
    """A short, key-free reason for an auth-type failure: billing, an invalid key, no permission or an unknown model."""
    text = str(exc).lower()
    if "billing" in text or "outstanding balance" in text or "quota" in text and "exceeded" in text:
        return "the provider account has a billing problem"
    if "api key not valid" in text or "invalid api key" in text or "incorrect api key" in text or "invalid x-api-key" in text:
        return "the API key was rejected"
    if "not found" in text and "model" in text:
        return "the model is not available for this key"
    if "permission" in text or "forbidden" in text:
        return "the key has no permission for this model"
    return "the key was refused"


def _classify(exc: BaseException) -> str:
    """'refused' | 'rate_limit' | 'transient' | 'auth' | 'bad_request' | 'unknown'."""
    names = _names(exc)
    code = getattr(exc, "status_code", None)
    if "ContentPolicyViolationError" in names:
        return "refused"
    if code == 429 or names & {"RateLimitError", "RouterRateLimitError", "RouterRateLimitErrorBasic"}:
        return "rate_limit"
    if names & {"Timeout", "APITimeoutError", "APIConnectionError", "ServiceUnavailableError",
                "BadGatewayError", "InternalServerError", "TimeoutError", "ConnectionError"}:
        return "transient"
    if isinstance(code, int):
        if code >= 500 or code == 408:
            return "transient"
        if code in (401, 403, 404):
            return "auth"
        if 400 <= code < 500:
            return "bad_request"
    if names & {"AuthenticationError", "PermissionDeniedError", "NotFoundError"}:
        return "auth"
    if names & {"BadRequestError", "UnsupportedParamsError", "UnprocessableEntityError"}:
        return "bad_request"
    return "unknown"


def _retry_after(exc: BaseException) -> float | None:
    """Seconds from a Retry-After header (or a router cooldown) carried on the exception, if any."""
    headers = None
    for attr in ("litellm_response_headers", "headers"):
        h = getattr(exc, attr, None)
        if h:
            headers = h
            break
    if headers is None:
        resp = getattr(exc, "response", None)
        headers = getattr(resp, "headers", None)
    value = None
    if headers is not None:
        try:
            value = headers.get("retry-after") or headers.get("Retry-After")
        except Exception:
            value = None
    if value is not None:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            parsed = email.utils.parsedate_tz(str(value))
            if parsed is not None:
                return max(0.0, email.utils.mktime_tz(parsed) - time.time())
    cooldown = getattr(exc, "cooldown_time", None)  # RouterRateLimitError: all deployments cooling down
    if isinstance(cooldown, (int, float)) and cooldown > 0:
        return float(cooldown)
    return None


def _backoff(attempt: int, base: float, cap: float) -> float:
    """Full jitter: uniform(0, min(cap, base * 2**attempt))."""
    return random.uniform(0, min(cap, base * (2 ** attempt)))


# --------------------------------------------------------------------------- dispatch
def _dispatch(group: str, org_id: str | None, **kwargs) -> Any:
    """Call the router tier by tier with backoff, breaker and explicit re-dispatch. Returns a ModelResponse."""
    if _mode() == "offline":
        raise LLMUnavailable("AI is disabled (ARCHFLOW_AI=offline)")
    org_keys, org_cfg = _org_keys(org_id), _org_config(org_id)
    tiers = _tiers(group, org_keys, org_cfg)
    if not tiers:
        raise LLMUnavailable("No LLM provider configured (set GEMINI_API_KEY, ANTHROPIC_API_KEY or OPENAI_API_KEY, "
                             "or add your organization's own key in AI settings)")
    blocked = budget_block(org_id)
    if blocked:
        raise LLMBudgetExceeded(blocked)
    _acquire_org(org_id)
    guard = redact.Redactor()
    if guard.mode != "off":
        kwargs = {**kwargs, "messages": _redact_messages(kwargs.get("messages") or [], guard)}
    redact.report({"redacted": dict(guard.counts), "injection": untrusted.scan(kwargs.get("messages") or [])})

    own = bool(org_keys) or _has_chains(org_cfg)
    router = _get_router(org_keys, org_id, org_cfg) if own else _get_router()
    max_attempts = max(1, _env_int("ARCHFLOW_LLM_MAX_ATTEMPTS", 5))
    base, cap = _env_float("ARCHFLOW_LLM_BACKOFF_BASE", 0.5), _env_float("ARCHFLOW_LLM_BACKOFF_CAP", 20.0)
    deadline = time.monotonic() + _env_float("ARCHFLOW_LLM_DEADLINE", 120.0)
    started = time.monotonic()
    total_calls = 0
    last_exc: BaseException | None = None
    errors: list[str] = []

    for tier_idx, tier in enumerate(tiers):
        # An org's own key gets its own breaker: one org's bad key must not trip the shared provider circuit.
        breaker_key = tier.model + (f"@{org_id}" if tier.provider or org_keys.get(_provider(tier.model)) else "")
        breaker = _breaker(breaker_key)
        if not breaker.allow():
            log.info("llm skip model=%s group=%s reason=circuit_open", tier.model, group)
            why = _LAST_REFUSAL.get(breaker_key)
            errors.append(f"{tier.model}: {why}" if why else f"{tier.model}: circuit open")
            continue
        for attempt in range(max_attempts):
            total_calls += 1
            t0 = time.monotonic()
            try:
                resp = router.completion(model=tier.name, fallbacks=[], **kwargs)
            except Exception as exc:  # noqa: BLE001 - classified below
                kind = _classify(exc)
                last_exc = exc
                latency = (time.monotonic() - t0) * 1000
                log.warning("llm error model=%s group=%s org=%s attempt=%d kind=%s type=%s latency_ms=%.0f",
                            tier.model, group, org_id, attempt + 1, kind, type(exc).__name__, latency)
                if kind == "refused":
                    raise LLMRefused("The model declined this request (content policy)") from exc
                if kind == "bad_request":
                    errors.append(f"{tier.model}: bad request ({type(exc).__name__})")
                    break  # request-specific; another provider may accept it. Not a health signal.
                breaker.failure()
                if kind in ("auth", "unknown"):
                    if kind == "unknown":
                        log.exception("unexpected LLM error from %s", tier.model)
                    if kind == "auth":
                        _LAST_REFUSAL[breaker_key] = refusal_reason(exc)
                        errors.append(f"{tier.model}: {_LAST_REFUSAL[breaker_key]}")
                    else:
                        errors.append(f"{tier.model}: {kind} ({type(exc).__name__})")
                    break
                # rate_limit / transient: back off on this tier, then re-dispatch to the next one
                errors.append(f"{tier.model}: {kind}")
                if attempt + 1 >= max_attempts or not breaker.allow():
                    break
                hinted = _retry_after(exc)
                delay = hinted if hinted is not None else _backoff(attempt, base, cap)
                if delay > cap or time.monotonic() + delay > deadline:
                    # Too long to wait here: hand over to the next provider now (or give up at the deadline)
                    log.info("llm redispatch model=%s wait=%.1fs exceeds budget", tier.model, delay)
                    break
                time.sleep(delay)
                continue
            breaker.success()
            _LAST_REFUSAL.pop(breaker_key, None)
            usage = getattr(resp, "usage", None)
            log.info("llm ok group=%s model=%s served_by=%s org=%s latency_ms=%.0f total_ms=%.0f calls=%d "
                     "fallback_depth=%d prompt_tokens=%s completion_tokens=%s",
                     group, tier.model, getattr(resp, "model", None), org_id, (time.monotonic() - t0) * 1000,
                     (time.monotonic() - started) * 1000, total_calls, tier_idx,
                     getattr(usage, "prompt_tokens", None), getattr(usage, "completion_tokens", None))
            if kwargs.get("stream"):
                try:  # stream_text records usage once the stream is read
                    setattr(resp, "_archflow_model", tier.model)
                except Exception:  # noqa: BLE001
                    pass
            else:
                _record(org_id, group, tier.model, getattr(usage, "prompt_tokens", 0) or 0,
                        getattr(usage, "completion_tokens", 0) or 0, (time.monotonic() - t0) * 1000, resp)
            return _restore_response(resp, guard, group)
        if time.monotonic() > deadline:
            break

    log.error("llm exhausted group=%s org=%s calls=%d errors=%s", group, org_id, total_calls, errors)
    raise LLMUnavailable("All LLM providers failed: " + "; ".join(errors[-6:])) from last_exc


# --------------------------------------------------------------------------- redaction
def _redact_messages(messages: list[dict], guard: redact.Redactor) -> list[dict]:
    """Copies of the messages with PII and secrets replaced: string and list-of-parts content, tool-call arguments."""
    out: list[dict] = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        m = dict(m)
        content = m.get("content")
        if isinstance(content, str):
            m["content"] = guard.redact(content)
        elif isinstance(content, list):
            m["content"] = [{**p, "text": guard.redact(p["text"])} if isinstance(p, dict) and isinstance(p.get("text"), str)
                            else guard.redact(p) if isinstance(p, str) else p for p in content]
        if m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = tc.get("function") if isinstance(tc, dict) else None
                if isinstance(fn, dict) and fn.get("arguments") is not None:
                    tc = {**tc, "function": {**fn, "arguments": guard.redact_value(fn["arguments"])}}
                calls.append(tc)
            m["tool_calls"] = calls
        out.append(m)
    return out


def _set(obj: Any, name: str, value: Any) -> None:
    if isinstance(obj, dict):
        obj[name] = value
    else:
        try:
            setattr(obj, name, value)
        except Exception:  # noqa: BLE001 - an immutable response object keeps its text
            log.warning("could not rewrite %s on %s", name, type(obj).__name__)


def _restore_response(resp: Any, guard: redact.Redactor, group: str) -> Any:
    """Put PII back into the answer (text and tool-call arguments) and mask secrets the model wrote."""
    if guard.mode == "off":
        return resp
    if _get(resp, "choices") is None:  # a stream: restored chunk by chunk as it is read
        return _RestoredStream(resp, redact.StreamRestorer(guard.mapping), group)
    masked: Counter[str] = Counter()
    for choice in _get(resp, "choices") or []:
        msg = _get(choice, "message")
        if msg is None:
            continue
        content = _get(msg, "content")
        if isinstance(content, str) and content:
            _set(msg, "content", redact.mask_secrets(redact.restore(content, guard.mapping), masked))
        for tc in _get(msg, "tool_calls") or []:
            fn = _get(tc, "function")
            args = _get(fn, "arguments") if fn is not None else None
            if args:
                _set(fn, "arguments", redact.mask_value(redact.restore_value(args, guard.mapping), masked))
    _report_masked(masked, group)
    return resp


def _report_masked(masked: Counter[str], group: str) -> None:
    if masked:
        log.warning("llm output contained secrets; masked group=%s kinds=%s", group, dict(masked))
        redact.report({"masked": dict(masked)})


class _RestoredStream:
    """A provider stream whose text chunks have PII restored and secrets masked (see ``redact.StreamRestorer``)."""

    def __init__(self, inner: Any, restorer: redact.StreamRestorer, group: str):
        self._inner, self._restorer, self._group = inner, restorer, group

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def __iter__(self) -> Iterator[Any]:
        for chunk in self._inner:
            choices = _get(chunk, "choices") or []
            delta = _get(choices[0], "delta") if choices else None
            text = _get(delta, "content") if delta is not None else None
            if isinstance(text, str) and text:
                _set(delta, "content", self._restorer.feed(text))
            yield chunk
        tail = self._restorer.flush()
        if tail:
            yield {"choices": [{"delta": {"content": tail}, "finish_reason": None}]}
        _report_masked(self._restorer.masked, self._group)


# --------------------------------------------------------------------------- helpers
def _msgs(system: str, messages: list[dict]) -> list[dict]:
    return ([{"role": "system", "content": system}] if system else []) + list(messages)


def _message(resp: Any) -> tuple[Any, str | None]:
    choice = resp.choices[0]
    msg = choice.message if hasattr(choice, "message") else choice["message"]
    finish = getattr(choice, "finish_reason", None) if not isinstance(choice, dict) else choice.get("finish_reason")
    return msg, finish


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?\s*```\s*$", re.S)


def parse_json(text: str) -> Any:
    """Parse JSON defensively: strip code fences, then fall back to the outermost {...} span."""
    if text is None:
        raise ValueError("empty response")
    s = text.strip()
    m = _FENCE.match(s)
    if m:
        s = m.group(1).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start != -1 and end > start:
            return json.loads(s[start:end + 1])
        raise


def _validate(data: Any, schema: dict) -> None:
    """Raise ValueError if ``data`` does not satisfy ``schema``."""
    try:
        import jsonschema
    except ImportError:  # pragma: no cover - jsonschema ships with litellm
        jsonschema = None
    if jsonschema is not None:
        try:
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError as exc:
            path = "/".join(str(p) for p in exc.absolute_path)
            raise ValueError(f"{exc.message}" + (f" at '{path}'" if path else "")) from exc
        return
    if schema.get("type") == "object" and not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    missing = [k for k in schema.get("required", []) if not isinstance(data, dict) or k not in data]
    if missing:
        raise ValueError(f"missing required keys: {missing}")


def _check_refusal(finish: str | None, content: Any) -> None:
    if finish in ("content_filter", "refusal"):
        raise LLMRefused("The model declined this request")


# --------------------------------------------------------------------------- public calls
def complete_json(system: str, messages: list[dict], schema: dict, *, group: str = "reasoning",
                  org_id: str | None = None, max_tokens: int = 8000) -> dict:
    """Return a dict from the model's JSON output, validated against ``schema`` (JSON Schema)."""
    response_format: dict | None = {"type": "json_schema",
                                    "json_schema": {"name": "result", "schema": schema, "strict": False}}
    sys_prompt = system
    convo = list(messages)
    retried_invalid = False
    while True:
        kwargs: dict[str, Any] = {"messages": _msgs(sys_prompt, convo), "max_tokens": max(max_tokens, MIN_OUTPUT_TOKENS),
                                  "drop_params": True}
        if response_format is not None:
            kwargs["response_format"] = response_format
        try:
            resp = _dispatch(group, org_id, **kwargs)
        except LLMUnavailable as exc:
            # Some providers reject a given JSON schema; retry once with the schema in the prompt instead.
            if response_format is not None and _classify(exc.__cause__ or exc) == "bad_request":
                log.info("structured output rejected; retrying with prompt-embedded schema")
                response_format = None
                sys_prompt = (f"{system}\n\nRespond with only a JSON object (no prose, no code fences) that "
                              f"conforms to this JSON Schema:\n{json.dumps(schema)}")
                continue
            raise
        msg, finish = _message(resp)
        content = _get(msg, "content") or ""
        _check_refusal(finish, content)
        try:
            data = parse_json(content)
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object at the top level")
            _validate(data, schema)
            return data
        except ValueError as exc:  # json.JSONDecodeError is a ValueError
            if retried_invalid:
                raise LLMInvalidOutput(f"Model returned invalid JSON twice: {exc}") from exc
            retried_invalid = True
            log.info("invalid JSON from model (%s); retrying once with the error", type(exc).__name__)
            convo = convo + [
                {"role": "assistant", "content": content[:20000]},
                {"role": "user", "content": f"That response was invalid: {exc}. Reply again with only a JSON "
                                            "object that conforms to the required schema."},
            ]


def stream_text(system: str, messages: list[dict], *, group: str = "reasoning", org_id: str | None = None,
                max_tokens: int = 8000) -> Iterator[str]:
    """Yield a plain-text answer in chunks as the model writes it.

    Retries and provider fallback apply until a provider accepts the request. Some providers only report a
    rate limit once the stream is read; if the stream fails before any text arrived, the answer is fetched
    again without streaming (full retry/fallback path). A failure after text was sent raises LLMUnavailable.
    """
    resp = _dispatch(group, org_id, messages=_msgs(system, messages),
                     max_tokens=max(max_tokens, MIN_OUTPUT_TOKENS), stream=True)
    if _get(resp, "choices") is not None:  # the provider answered in one piece
        msg, finish = _message(resp)
        text = _get(msg, "content") or ""
        _check_refusal(finish, text)
        if text:
            yield text
        return
    sent = False
    parts: list[str] = []
    t0 = time.monotonic()
    try:
        for chunk in resp:
            choices = _get(chunk, "choices") or []
            if not choices:
                continue
            choice = choices[0]
            _check_refusal(_get(choice, "finish_reason"), None)
            text = _get(_get(choice, "delta"), "content") if _get(choice, "delta") is not None else None
            if text:
                sent = True
                parts.append(text)
                yield text
        # Streams carry no usage here: estimate ~4 characters per token from the prompt and the answer.
        prompt_chars = len(system) + sum(len(str(m.get("content", ""))) for m in messages)
        _record(org_id, group, getattr(resp, "_archflow_model", primary_model(group)), prompt_chars // 4,
                len("".join(parts)) // 4, (time.monotonic() - t0) * 1000, estimated=True)
    except LLMRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - classified below
        if _classify(exc) == "refused":
            raise LLMRefused("The model declined this request (content policy)") from exc
        if sent:
            log.warning("llm stream interrupted group=%s org=%s type=%s", group, org_id, type(exc).__name__)
            raise LLMUnavailable(f"The answer was interrupted ({type(exc).__name__}). Try again.") from exc
        log.info("llm stream failed before any text (%s); retrying without streaming", type(exc).__name__)
        text, _ = complete_tools(system, messages, [], group=group, org_id=org_id, max_tokens=max_tokens)
        if text:
            yield text


def complete_tools(system: str, messages: list[dict], tools: list[dict], *, group: str = "reasoning",
                   org_id: str | None = None, max_tokens: int = 8000) -> tuple[str, list[dict]]:
    """Return (assistant_text, [{"name": str, "arguments": dict}, ...]) for OpenAI-format ``tools``."""
    kwargs: dict[str, Any] = {"messages": _msgs(system, messages), "max_tokens": max(max_tokens, MIN_OUTPUT_TOKENS)}
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    resp = _dispatch(group, org_id, **kwargs)
    msg, finish = _message(resp)
    text = _get(msg, "content") or ""
    _check_refusal(finish, text)
    calls: list[dict] = []
    for tc in _get(msg, "tool_calls") or []:
        fn = _get(tc, "function")
        name = _get(fn, "name")
        raw = _get(fn, "arguments")
        if not name:
            continue
        if isinstance(raw, dict):
            args = raw
        else:
            try:
                args = parse_json(raw or "{}")
            except ValueError:
                log.warning("dropping tool call %s: arguments are not valid JSON", name)
                continue
            if not isinstance(args, dict):
                log.warning("dropping tool call %s: arguments are not an object", name)
                continue
        calls.append({"name": name, "arguments": args})
    return (text.strip() if isinstance(text, str) else str(text)), calls


def test_connection(org_id: str | None, group: str = "reasoning") -> dict[str, Any]:
    """One tiny real call through the org's chain for ``group``: {ok, model, latency_ms, reply | error}."""
    tiers = _tiers(group, _org_keys(org_id), _org_config(org_id))
    if not tiers:
        return {"ok": False, "group": group, "error": "No provider with a key is configured for this group."}
    t0 = time.monotonic()
    try:
        with usage_scope(feature="settings"):
            resp = _dispatch(group, org_id, messages=[{"role": "user", "content": "Reply with the single word: OK"}],
                             max_tokens=max(64, MIN_OUTPUT_TOKENS))
        msg, _ = _message(resp)
        return {"ok": True, "group": group, "model": getattr(resp, "model", None) or tiers[0].model,
                "chain": [t.model for t in tiers], "latency_ms": int((time.monotonic() - t0) * 1000),
                "reply": str(_get(msg, "content") or "")[:80]}
    except (LLMUnavailable, LLMRefused) as exc:
        return {"ok": False, "group": group, "chain": [t.model for t in tiers], "error": str(exc)[:500]}


def default_chain(group: str) -> list[str]:
    """The server's default model chain for a group (environment / built-in), before checking keys."""
    return _group_models(group)


# --------------------------------------------------------------------------- embeddings and transcription
# Embedding models tried in order when ARCHFLOW_MODEL_EMBEDDING is not set: the first whose provider has a key.
# (Anthropic has no embedding API.) Verified against litellm's model map.
EMBEDDING_MODELS = (("gemini", "gemini/gemini-embedding-001"), ("openai", "openai/text-embedding-3-small"))
EMBED_BATCH = 64
EMBED_MAX_CHARS = 8000
TRANSCRIBE_MODEL = "openai/whisper-1"
TRANSCRIBE_USD_PER_MINUTE = 0.006


def _flag_off(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("off", "0", "false", "no")


def embedding_model(org_id: str | None = None) -> tuple[str, str | None] | None:
    """(LiteLLM model id, API key) used for embeddings for this org, or None when none can be used: AI offline,
    ``ARCHFLOW_EMBEDDINGS=off``, or no Gemini / OpenAI key (``ARCHFLOW_MODEL_EMBEDDING`` picks another model)."""
    if _mode() == "offline" or _flag_off("ARCHFLOW_EMBEDDINGS"):
        return None
    org_keys = _org_keys(org_id)
    chosen = os.environ.get("ARCHFLOW_MODEL_EMBEDDING", "").strip()
    candidates = [(_provider(chosen), chosen)] if chosen else list(EMBEDDING_MODELS)
    for provider, model in candidates:
        keys = _keys(provider, org_keys)
        if keys:
            return model, keys[0]
    return None


def _call_with_retries(fn: Callable[[], Any], what: str) -> Any:
    attempts = max(1, _env_int("ARCHFLOW_LLM_MAX_ATTEMPTS", 5))
    base, cap = _env_float("ARCHFLOW_LLM_BACKOFF_BASE", 0.5), _env_float("ARCHFLOW_LLM_BACKOFF_CAP", 20.0)
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - classified below
            kind = _classify(exc)
            log.warning("%s error attempt=%d kind=%s type=%s", what, attempt + 1, kind, type(exc).__name__)
            if kind not in ("rate_limit", "transient") or attempt + 1 >= attempts:
                raise LLMUnavailable(f"{what} failed: {kind} ({type(exc).__name__})") from exc
            hinted = _retry_after(exc)
            time.sleep(min(cap, hinted if hinted is not None else _backoff(attempt, base, cap)))
    raise LLMUnavailable(f"{what} failed")


def embed(texts: list[str], org_id: str | None = None, model: str | None = None) -> tuple[str, list[list[float]]]:
    """Embedding vectors for ``texts`` (same order) and the model that made them. ``model`` forces one model (a
    query must be embedded with the model its index was built with). Personal data and secrets are replaced with
    placeholders first, as for completions. Raises LLMUnavailable when no embedding model can be used."""
    picked = embedding_model(org_id)
    if picked is None:
        raise LLMUnavailable("No embedding model is available (needs a Gemini or OpenAI key)")
    name, key = picked
    if model and model != name:
        provider = _provider(model)
        keys = _keys(provider, _org_keys(org_id))
        if not keys:
            raise LLMUnavailable(f"The index was built with {model}, which has no key now")
        name, key = model, keys[0]
    blocked = budget_block(org_id)
    if blocked:
        raise LLMBudgetExceeded(blocked)
    import litellm

    guard = redact.Redactor()
    clean = [guard.redact(t[:EMBED_MAX_CHARS]) if guard.mode != "off" else t[:EMBED_MAX_CHARS] for t in texts]
    redact.report({"redacted": dict(guard.counts), "injection": {}})
    out: list[list[float]] = []
    for start in range(0, len(clean), EMBED_BATCH):
        batch = [t or " " for t in clean[start:start + EMBED_BATCH]]
        _acquire_org(org_id)
        t0 = time.monotonic()
        kwargs: dict[str, Any] = {"model": name, "input": batch, "timeout": _env_float("ARCHFLOW_LLM_TIMEOUT", 120)}
        if key:
            kwargs["api_key"] = key
        resp = _call_with_retries(lambda: litellm.embedding(**kwargs), f"embedding with {name}")
        data = sorted(_get(resp, "data") or [], key=lambda d: _get(d, "index", 0))
        vectors = [list(map(float, _get(d, "embedding") or [])) for d in data]
        if len(vectors) != len(batch) or any(not v for v in vectors):
            raise LLMUnavailable(f"{name} returned {len(vectors)} embeddings for {len(batch)} texts")
        out += vectors
        usage = _get(resp, "usage")
        _record(org_id, "embedding", name, int(_get(usage, "prompt_tokens", 0) or 0), 0,
                (time.monotonic() - t0) * 1000, resp)
    return name, out


def transcription_model(org_id: str | None = None) -> tuple[str, str | None] | None:
    """(model, key) for speech-to-text (``ARCHFLOW_MODEL_TRANSCRIBE``, default OpenAI Whisper), or None."""
    if _mode() == "offline" or _flag_off("ARCHFLOW_TRANSCRIBE"):
        return None
    model = os.environ.get("ARCHFLOW_MODEL_TRANSCRIBE", "").strip() or TRANSCRIBE_MODEL
    keys = _keys(_provider(model), _org_keys(org_id))
    return (model, keys[0]) if keys else None


def transcribe(data: bytes, filename: str, org_id: str | None = None) -> dict[str, Any]:
    """Speech to text: {model, text, segments: [{start, end, text}] (seconds)}. Audio cannot be redacted before it
    is sent; set ARCHFLOW_TRANSCRIBE=off to keep recordings on the server. Raises LLMUnavailable."""
    picked = transcription_model(org_id)
    if picked is None:
        raise LLMUnavailable("No speech-to-text model is available (needs an OpenAI key, or set "
                             "ARCHFLOW_MODEL_TRANSCRIBE)")
    model, key = picked
    blocked = budget_block(org_id)
    if blocked:
        raise LLMBudgetExceeded(blocked)
    _acquire_org(org_id)
    import litellm

    kwargs: dict[str, Any] = {"model": model, "file": (filename, data), "response_format": "verbose_json",
                              "timeout": _env_float("ARCHFLOW_LLM_TIMEOUT", 120) * 3}
    if key:
        kwargs["api_key"] = key
    t0 = time.monotonic()
    resp = _call_with_retries(lambda: litellm.transcription(**kwargs), f"transcription with {model}")
    segments = [{"start": float(_get(s, "start", 0) or 0), "end": float(_get(s, "end", 0) or 0),
                 "text": str(_get(s, "text", "") or "").strip()} for s in (_get(resp, "segments") or [])]
    text = str(_get(resp, "text", "") or "").strip()
    minutes = (segments[-1]["end"] if segments else float(_get(resp, "duration", 0) or 0)) / 60
    observers = _call_observers.get()
    event = {"org_id": org_id or "", **{k: current_scope().get(k, "") for k in ("project_id", "prompt", "user_id")},
             "feature": current_scope().get("feature", "other"), "group": "transcription", "model": model,
             "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": round(minutes * TRANSCRIBE_USD_PER_MINUTE, 6),
             "cost_source": "estimate", "latency_ms": int((time.monotonic() - t0) * 1000)}
    for observe in observers:
        try:
            observe(event)
        except Exception:  # noqa: BLE001
            log.exception("LLM call observer failed")
    if _usage_recorder is not None:
        try:
            _usage_recorder(event)
        except Exception:  # noqa: BLE001
            log.exception("recording transcription usage failed")
    return {"model": model, "text": text, "segments": segments}
