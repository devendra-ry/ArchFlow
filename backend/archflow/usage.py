"""AI spend: every LLM call's tokens and estimated cost, per organization, project and feature; monthly budgets.

* ``llm`` records each call through ``recorder`` and asks ``blocked`` before each call (wired by ``install``).
* ``UsageScopeMiddleware`` tags a request's LLM calls with its project and feature, from the API path.
* An organization admin sets a monthly budget (USD) in AI settings. Once this calendar month's (UTC) spend reaches
  it, ``llm.available`` turns False and calls raise ``LLMBudgetExceeded``: AI features fall back to their built-in
  non-AI behaviour and say why. At ``alert_percent`` the settings show a warning.

Costs are estimates: LiteLLM's price list when it knows the model, else ``llm.DEFAULT_PRICES`` /
``ARCHFLOW_LLM_PRICES``; streamed answers are estimated from their length.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import llm

CACHE_SECONDS = 15

FEATURES: list[tuple[str, str]] = [  # (path pattern after /api/projects/{pid}, feature)
    (r"/conversations/[^/]+/(ask|regenerate)", "assistant"),
    (r"/conversations/[^/]+/command", "canvas-edit"),
    (r"/ai/command", "canvas-edit"),
    (r"/ai/ask", "assistant"),
    (r"/ai/suggestions", "suggestions"),
    (r"/ai/review", "review"),
    (r"/chat", "design-chat"),
    (r"/openspec", "openspec"),
    (r"/nodes/[^/]+/lld", "low-level-design"),
    (r"/(evaluate|decisions)", "decisions"),
    (r"/merge", "merge"),
    (r"/codegraph", "code-analysis"),
    (r"/requirements/import", "intake"),
    (r"/meetings", "meetings"),
    (r"/knowledge", "knowledge"),
    (r"/costs", "costs"),
    (r"/generate", "generation"),
    (r"/(github|confluence)", "integrations"),
    (r"/jira", "jira"),
]
_PROJECT = re.compile(r"^/api/projects/([^/]+)(/.*)?$")


def scope_for_path(path: str) -> dict[str, str]:
    if path.startswith("/mcp"):
        return {"feature": "mcp"}
    m = _PROJECT.match(path)
    if not m:
        return {"feature": "other"}
    rest = m.group(2) or ""
    feature = next((f for pattern, f in FEATURES if re.match(pattern, rest)), "other")
    return {"project_id": m.group(1), "feature": feature}


class UsageScopeMiddleware:
    """Pure ASGI (streamed responses pass through): sets the LLM usage scope for the request's context."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            llm.set_usage_scope(**scope_for_path(scope.get("path", "")))
        await self.app(scope, receive, send)


def month_start(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat(timespec="seconds")


class Budgets:
    """Monthly spend and budget per org, cached for CACHE_SECONDS; recorded calls are added immediately."""

    def __init__(self, store: Any):
        self.store = store
        self._lock = threading.Lock()
        self._spend: dict[str, tuple[float, float, str]] = {}  # org → (spend, fetched_at, month)
        self._budget: dict[str, tuple[dict[str, Any], float]] = {}

    def spend(self, org_id: str) -> float:
        month = month_start()
        with self._lock:
            hit = self._spend.get(org_id)
            if hit and hit[2] == month and time.monotonic() - hit[1] < CACHE_SECONDS:
                return hit[0]
        value = self.store.ai_spend(org_id, month)
        with self._lock:
            self._spend[org_id] = (value, time.monotonic(), month)
        return value

    def budget(self, org_id: str) -> dict[str, Any]:
        with self._lock:
            hit = self._budget.get(org_id)
            if hit and time.monotonic() - hit[1] < CACHE_SECONDS:
                return hit[0]
        value = self.store.get_ai_budget(org_id)
        with self._lock:
            self._budget[org_id] = (value, time.monotonic())
        return value

    def forget(self, org_id: str) -> None:
        with self._lock:
            self._budget.pop(org_id, None)
            self._spend.pop(org_id, None)

    def record(self, event: dict[str, Any]) -> None:
        if not event.get("org_id"):
            return
        self.store.record_ai_usage(event)
        with self._lock:
            hit = self._spend.get(event["org_id"])
            if hit:
                self._spend[event["org_id"]] = (hit[0] + float(event.get("cost_usd") or 0), hit[1], hit[2])

    def blocked(self, org_id: str) -> str | None:
        limit = self.budget(org_id).get("monthly_limit_usd")
        if limit is None:
            return None
        spent = self.spend(org_id)
        if spent < float(limit):
            return None
        return (f"Your organization's monthly AI budget (${float(limit):,.2f}) is used up (${spent:,.2f} spent this "
                "month). An admin can raise it in AI settings; until then ArchFlow uses its built-in non-AI features.")


def install(store: Any) -> Budgets:
    budgets = Budgets(store)
    llm.set_usage_hooks(budgets.record, budgets.blocked)
    return budgets


# --------------------------------------------------------------------------- routes
class BudgetIn(BaseModel):
    monthly_limit_usd: float | None = Field(None, ge=0, le=1_000_000)  # None = no limit
    alert_percent: int = Field(80, ge=1, le=100)


def register_routes(app: FastAPI, store: Any, authz: Any, budgets: Budgets) -> None:
    admin = authz.org("admin")

    def report(org_id: str, days: int) -> dict[str, Any]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).replace(hour=0, minute=0, second=0,
                                                                            microsecond=0).isoformat(timespec="seconds")
        budget = store.get_ai_budget(org_id)
        spent = store.ai_spend(org_id, month_start())
        limit = budget.get("monthly_limit_usd")
        used = (spent / limit * 100) if limit else None
        projects = {p["id"]: p["name"] for p in store.list_projects(org_id)}
        summary = store.ai_usage_summary(org_id, since)
        for row in summary["by_project"]:
            row["name"] = projects.get(row["key"], "(no project)" if not row["key"] else "(deleted project)")
        people = {m["user_id"]: m["name"] for m in store.list_members(org_id)}
        for row in summary.get("by_user", []):
            row["name"] = people.get(row["key"], "(server or MCP)" if not row["key"] else "(former member)")
        return {"days": days, "month_start": month_start(), "month_spend_usd": round(spent, 4), "budget": budget,
                "used_percent": round(used, 1) if used is not None else None,
                "status": "blocked" if limit is not None and spent >= limit
                else "warning" if used is not None and used >= budget.get("alert_percent", 80) else "ok",
                **summary}

    @app.get("/api/orgs/{org_id}/ai-usage")
    def ai_usage(org_id: str, days: int = 30, a=Depends(admin)):
        return report(org_id, max(1, min(days, 366)))

    @app.put("/api/orgs/{org_id}/ai-budget")
    def set_budget(org_id: str, body: BudgetIn, a=Depends(admin)):
        store.set_ai_budget(org_id, body.monthly_limit_usd, body.alert_percent, a.actor)
        budgets.forget(org_id)
        return report(org_id, 30)
