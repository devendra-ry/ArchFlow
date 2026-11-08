"""Cost estimation: a monthly cost range for the architecture on the canvas, line by line.

Every top-level component is priced from its catalog kind (``catalog.KINDS``: containers, functions, relational
databases, caches, queues, object storage, CDN, identity, LLMs, ...) and its vendor (AWS, Azure, Google Cloud, open
source on self-managed VMs, or the SaaS product it names), sized from the workload assumptions:

* monthly active users, peak requests per second, stored data, data transfer out, LLM calls and tokens, emails and
  messages per user;
* defaults come from the project itself: numbers in the non-functional requirements ("10,000 concurrent users",
  "500 requests per second", "99.95% availability", "2 TB of documents") and the project memory; anything not stated
  uses a typical value, and every assumption says where it came from;
* availability of 99.95% or more doubles stateful tiers (multi-AZ databases and caches, two service instances).

A component can pin its own numbers through its properties: ``monthly_cost`` (a fixed amount), ``instances``,
``vcpu``, ``storage_gb``. LLM components use the model's token prices from LiteLLM's price map when it knows the
model (``llm.estimate_cost``).

Prices are indicative on-demand list prices (us-east, October 2026) built into ``PRICES``. They are for comparing
options and spotting the big lines, not quotes. ``ARCHFLOW_COST_PRICES`` (JSON ``{"aws:db.relational":
{"instance": 150}}``) overrides any parameter. Each line has a low-high range (usage-based lines are less certain
than fixed instances), the formula that produced it, and a confidence.

The estimate also compares the total with a budget stated in a cost requirement ("must cost less than $3,000 per
month") and lists the biggest cost drivers. ``costs.optimize`` (an agent) writes savings recommendations grounded in
the line items; without a model, rules suggest the standard ones (commitments for steady instances, serverless for
low traffic, a CDN for heavy egress, model routing for LLM-heavy designs).

=======  ==========================================  ===================================================
method   path                                        what
=======  ==========================================  ===================================================
POST     /api/projects/{pid}/costs                   {assumptions?, ai?} → estimate (+ recommendations)
GET      /api/projects/{pid}/costs.csv               the line items as CSV (default assumptions)
=======  ==========================================  ===================================================
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import math
import os
import re
from typing import Any, Callable

from fastapi import Depends, FastAPI
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import agents, catalog, llm
from .models import Node, ProjectState, active_requirements

log = logging.getLogger("archflow.costs")

PROMPT = "costs.optimize"
PRICE_DATE = "2026-10"
HOURS = 730  # hours per month
SECONDS = 2_592_000  # seconds per 30-day month
REGION = {"us": 1.0, "eu": 1.1, "uk": 1.12, "asia": 1.15, "other": 1.1}

TYPE_KIND = {"service": "compute.container", "database": "db.relational", "cache": "cache.kv", "queue": "msg.queue",
             "storage": "storage.object", "gateway": "edge.gateway", "llm": "ai.llm", "agent": "ai.agent",
             "mcp": "ai.mcp", "security": "security.identity", "client": "client.web", "external": "external"}
CATEGORY = {"compute": "Compute", "db": "Data", "cache": "Data", "msg": "Messaging", "storage": "Storage",
            "edge": "Networking", "security": "Security", "ai": "AI", "ops": "Operations", "client": "Clients",
            "external": "Third party", "transfer": "Networking"}

# Indicative on-demand list prices (USD). Units are in the parameter names: *_hour, *_month, *_gb (per GB-month or
# per GB transferred), *_m (per million requests / messages / tokens), *_mau (per monthly active user).
PRICES: dict[str, dict[str, dict[str, float]]] = {
    "compute.container": {"aws": {"vcpu_hour": 0.04048, "gb_hour": 0.004445}, "azure": {"vcpu_hour": 0.0864, "gb_hour": 0.0108},
                          "gcp": {"vcpu_hour": 0.0648, "gb_hour": 0.0072}, "oss": {"vcpu_hour": 0.025, "gb_hour": 0.0034}},
    "compute.kubernetes": {"aws": {"vcpu_hour": 0.0336, "gb_hour": 0.0042, "control_month": 73},
                           "azure": {"vcpu_hour": 0.0336, "gb_hour": 0.0042, "control_month": 0},
                           "gcp": {"vcpu_hour": 0.0316, "gb_hour": 0.0042, "control_month": 73},
                           "oss": {"vcpu_hour": 0.025, "gb_hour": 0.0034, "control_month": 0}},
    "compute.vm": {"aws": {"vcpu_hour": 0.0336, "gb_hour": 0.0042}, "azure": {"vcpu_hour": 0.0346, "gb_hour": 0.0046},
                   "gcp": {"vcpu_hour": 0.0316, "gb_hour": 0.0042}, "oss": {"vcpu_hour": 0.025, "gb_hour": 0.0034}},
    "compute.function": {"aws": {"requests_m": 0.20, "gb_second": 0.0000166667},
                         "azure": {"requests_m": 0.20, "gb_second": 0.000016},
                         "gcp": {"requests_m": 0.40, "gb_second": 0.0000025 * 4},
                         "oss": {"vcpu_hour": 0.025, "gb_hour": 0.0034}},
    "edge.gateway": {"aws": {"requests_m": 1.00}, "azure": {"requests_m": 3.50}, "gcp": {"requests_m": 3.00},
                     "oss": {"vm_month": 35}},
    "edge.lb": {"aws": {"base_month": 16.43, "per_100rps_month": 5.84}, "azure": {"base_month": 18.25, "per_100rps_month": 5.0},
                "gcp": {"base_month": 18.25, "per_100rps_month": 5.84}, "oss": {"vm_month": 35}},
    "edge.cdn": {"aws": {"egress_gb": 0.085}, "azure": {"egress_gb": 0.081}, "gcp": {"egress_gb": 0.08},
                 "saas": {"flat_month": 20}, "oss": {"egress_gb": 0.01}},
    "edge.dns": {"aws": {"flat_month": 2}, "azure": {"flat_month": 2}, "gcp": {"flat_month": 2}, "oss": {"flat_month": 0}},
    "db.relational": {"aws": {"vcpu_hour": 0.076, "storage_gb": 0.115}, "azure": {"vcpu_hour": 0.0857, "storage_gb": 0.115},
                      "gcp": {"vcpu_hour": 0.0826, "storage_gb": 0.17}, "oss": {"vcpu_hour": 0.034, "storage_gb": 0.08},
                      "saas": {"vcpu_hour": 0.07, "storage_gb": 0.12}},
    "db.document": {"aws": {"reads_m": 0.125, "writes_m": 0.625, "storage_gb": 0.25},
                    "azure": {"reads_m": 0.25, "writes_m": 1.25, "storage_gb": 0.25},
                    "gcp": {"reads_m": 0.6, "writes_m": 1.8, "storage_gb": 0.18}, "oss": {"vm_month": 70, "storage_gb": 0.08}},
    "db.warehouse": {"aws": {"base_month": 300, "storage_gb": 0.024}, "azure": {"scanned_tb": 5.0, "storage_gb": 0.023},
                     "gcp": {"scanned_tb": 6.25, "storage_gb": 0.02}, "saas": {"base_month": 400, "storage_gb": 0.023},
                     "oss": {"vm_month": 140, "storage_gb": 0.08}},
    "db.vector": {"aws": {"base_month": 175}, "azure": {"base_month": 75}, "gcp": {"base_month": 70},
                  "saas": {"base_month": 50}, "oss": {"vm_month": 40}},
    "db.search": {"aws": {"node_month": 53}, "azure": {"node_month": 75}, "gcp": {"node_month": 75},
                  "oss": {"vm_month": 70}},
    "cache.kv": {"aws": {"small_month": 47, "large_month": 109}, "azure": {"small_month": 55, "large_month": 160},
                 "gcp": {"small_month": 36, "large_month": 98}, "saas": {"commands_m": 2.0, "cap_month": 120},
                 "oss": {"vm_month": 35}},
    "msg.queue": {"aws": {"requests_m": 0.40}, "azure": {"base_month": 10, "requests_m": 0.80}, "gcp": {"requests_m": 0.40},
                  "oss": {"vm_month": 35}},
    "msg.stream": {"aws": {"base_month": 110}, "azure": {"base_month": 22}, "gcp": {"base_month": 200},
                   "saas": {"base_month": 50}, "oss": {"vm_month": 105}},
    "msg.pubsub": {"aws": {"requests_m": 0.50}, "azure": {"requests_m": 0.60}, "gcp": {"requests_m": 0.40},
                   "oss": {"vm_month": 35}},
    "storage.object": {"aws": {"storage_gb": 0.023, "requests_m": 2.0}, "azure": {"storage_gb": 0.0184, "requests_m": 2.0},
                       "gcp": {"storage_gb": 0.020, "requests_m": 2.0}, "oss": {"storage_gb": 0.08}},
    "storage.file": {"aws": {"storage_gb": 0.30}, "azure": {"storage_gb": 0.06}, "gcp": {"storage_gb": 0.20, "min_gb": 1024},
                     "oss": {"storage_gb": 0.08}},
    "security.identity": {"aws": {"free_mau": 10000, "mau": 0.0055}, "azure": {"free_mau": 50000, "mau": 0.00325},
                          "gcp": {"free_mau": 50000, "mau": 0.0055}, "saas": {"base_month": 25, "free_mau": 10000, "mau": 0.02},
                          "oss": {"vm_month": 35}},
    "security.secrets": {"aws": {"flat_month": 10}, "azure": {"flat_month": 3}, "gcp": {"flat_month": 3}, "oss": {"vm_month": 35}},
    "security.waf": {"aws": {"base_month": 15, "requests_m": 0.60}, "azure": {"base_month": 35, "requests_m": 0.60},
                     "gcp": {"base_month": 15, "requests_m": 0.75}, "oss": {"flat_month": 0}},
    "ai.llm": {"any": {"fallback_in_m": 3.0, "fallback_out_m": 15.0}},
    "ai.agent": {"aws": {"vcpu_hour": 0.04048, "gb_hour": 0.004445}, "azure": {"vcpu_hour": 0.0864, "gb_hour": 0.0108},
                 "gcp": {"vcpu_hour": 0.0648, "gb_hour": 0.0072}, "oss": {"vcpu_hour": 0.025, "gb_hour": 0.0034}},
    "ai.ml": {"aws": {"endpoint_month": 84}, "azure": {"endpoint_month": 90}, "gcp": {"endpoint_month": 80},
              "oss": {"vm_month": 70}},
    "ai.mcp": {"any": {"vcpu_hour": 0.04048, "gb_hour": 0.004445}},
    "ops.monitoring": {"aws": {"logs_gb": 0.50, "base_month": 10}, "azure": {"logs_gb": 2.30, "base_month": 0},
                       "gcp": {"logs_gb": 0.50, "free_gb": 50, "base_month": 0}, "saas": {"host_month": 15, "logs_gb": 0.10},
                       "oss": {"vm_month": 35}},
    "ops.workflow": {"aws": {"transitions_m": 25.0}, "azure": {"transitions_m": 125.0}, "gcp": {"transitions_m": 10.0},
                     "oss": {"vm_month": 70}},
    "ops.email": {"aws": {"emails_k": 0.10}, "azure": {"emails_k": 0.25}, "gcp": {"emails_k": 0.10},
                  "saas": {"flat_month": 19.95, "included": 50000, "emails_k": 0.60}},
    "ops.messaging": {"saas": {"message": 0.0083}, "any": {"message": 0.0083}},
    "ops.payments": {"any": {}},
    "client.web": {"any": {"flat_month": 5}, "saas": {"flat_month": 20}},
    "client.mobile": {"any": {"flat_month": 8}},
    "transfer": {"aws": {"egress_gb": 0.09}, "azure": {"egress_gb": 0.087}, "gcp": {"egress_gb": 0.12},
                 "oss": {"egress_gb": 0.02}, "saas": {"egress_gb": 0.09}},
}

ASSUMPTION_KEYS = ("monthly_active_users", "peak_rps", "storage_gb", "egress_gb", "availability",
                   "llm_calls_per_user", "llm_input_tokens", "llm_output_tokens", "emails_per_user",
                   "messages_per_user", "region", "include_staging", "staging_share")
DEFAULTS: dict[str, Any] = {"monthly_active_users": 10000, "peak_rps": None, "storage_gb": 100, "egress_gb": None,
                            "availability": 99.9, "llm_calls_per_user": 5, "llm_input_tokens": 2000,
                            "llm_output_tokens": 500, "emails_per_user": 4, "messages_per_user": 0, "region": "us",
                            "include_staging": True, "staging_share": 0.3}
RPS_PER_VCPU = 50  # a typical web service: requests per second one vCPU handles at peak


def prices() -> dict[str, dict[str, dict[str, float]]]:
    out = {k: {v: dict(p) for v, p in vs.items()} for k, vs in PRICES.items()}
    raw = os.environ.get("ARCHFLOW_COST_PRICES", "")
    if raw:
        try:
            for key, params in json.loads(raw).items():
                vendor, kind = key.split(":", 1)
                out.setdefault(kind, {}).setdefault(vendor, {}).update({k: float(v) for k, v in params.items()})
        except (ValueError, TypeError, AttributeError):
            log.warning("ARCHFLOW_COST_PRICES is not valid JSON {\"vendor:kind\": {param: number}}; using defaults")
    return out


# --------------------------------------------------------------------------- assumptions from the project
_NUM = r"(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|m|million|b|billion)?"


def _number(raw: str, unit: str | None) -> float:
    n = float(raw.replace(",", ""))
    mult = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}
    return n * mult.get((unit or "").lower(), 1)


def _texts(state: ProjectState, memory: list[dict[str, Any]]) -> list[tuple[str, str]]:
    out = [(f"{r.id}", f"{r.title}. {r.description}") for r in active_requirements(state).values()]
    out += [("memory", m.get("text", "")) for m in memory]
    return out


def derive(state: ProjectState, memory: list[dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    """Workload numbers stated in the requirements and memory: {key: {value, source, text}}."""
    found: dict[str, dict[str, Any]] = {}

    def put(key: str, value: float, source: str, text: str) -> None:
        if key not in found or value > found[key]["value"]:
            found[key] = {"value": value, "source": source, "text": text[:200]}

    for source, text in _texts(state, memory or []):
        low = text.lower()
        for m in re.finditer(_NUM + r"\s*(?:monthly active |active |concurrent |daily active |registered )?(?:users|customers|"
                             r"members|subscribers|tenants|drivers|patients)", low):
            n = _number(m.group(1), m.group(2))
            ctx = low[max(0, m.start() - 20):m.end()]
            if "concurrent" in ctx:
                put("peak_rps", n * 0.2, source, text)  # a concurrent user makes a request every ~5 s
                put("monthly_active_users", n * 10, source, text)
            elif "daily" in ctx:
                put("monthly_active_users", n * 3, source, text)
            else:
                put("monthly_active_users", n, source, text)
        for m in re.finditer(_NUM + r"\s*(?:requests|req|calls|transactions|orders|messages|events)\s*(?:per|/|a)\s*"
                             r"(second|sec|s|minute|min|hour|day)\b", low):
            n = _number(m.group(1), m.group(2))
            per = {"second": 1, "sec": 1, "s": 1, "minute": 60, "min": 60, "hour": 3600, "day": 86400}[m.group(3)]
            put("peak_rps", n / per * (1 if per == 1 else 3), source, text)
        for m in re.finditer(_NUM + r"\s*(?:rps|tps|qps)\b", low):
            put("peak_rps", _number(m.group(1), m.group(2)), source, text)
        for m in re.finditer(r"(\d[\d,]*(?:\.\d+)?)\s*(gb|tb|pb)\b", low):
            gb = float(m.group(1).replace(",", "")) * {"gb": 1, "tb": 1024, "pb": 1024 ** 2}[m.group(2)]
            ctx = low[max(0, m.start() - 60):m.end() + 40]
            if re.search(r"transfer|egress|bandwidth|download|stream", ctx):
                put("egress_gb", gb, source, text)
            elif re.search(r"stor|data|document|file|image|video|archive|retain|backup|database", ctx):
                put("storage_gb", gb, source, text)
        for m in re.finditer(r"(99(?:\.\d+)?|100)\s*%", low):
            if re.search(r"availab|uptime|sla", low):
                put("availability", float(m.group(1)), source, text)
    return found


def budget(state: ProjectState) -> dict[str, Any] | None:
    """A monthly budget stated in a cost requirement: {monthly, source, text}."""
    for r in active_requirements(state).values():
        text = f"{r.title}. {r.description}"
        low = text.lower()
        if r.category != "cost" and not re.search(r"\bbudget|cost|spend|\$|usd|eur|€", low):
            continue
        m = re.search(r"(?:\$|usd\s*|eur\s*|€)\s*(\d[\d,]*(?:\.\d+)?)\s*(k|m)?|(\d[\d,]*(?:\.\d+)?)\s*(k|m)?\s*(?:usd|dollars|"
                      r"eur|euros)", low)
        if not m:
            continue
        raw, unit = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        amount = _number(raw, unit)
        per_year = bool(re.search(r"per year|a year|/\s*year|annual|yearly|per annum", low))
        return {"monthly": round(amount / 12 if per_year else amount, 2), "source": r.id, "text": text[:200]}
    return None


def assumptions(state: ProjectState, memory: list[dict[str, Any]] | None = None,
                overrides: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """Every assumption with its value and where it came from (you | requirement id | memory | default)."""
    found = derive(state, memory)
    out: dict[str, dict[str, Any]] = {}
    for key in ASSUMPTION_KEYS:
        if overrides and overrides.get(key) is not None:
            out[key] = {"value": overrides[key], "source": "you"}
        elif key in found:
            out[key] = {"value": round(found[key]["value"], 2), "source": found[key]["source"], "text": found[key]["text"]}
        else:
            out[key] = {"value": DEFAULTS[key], "source": "default"}
    mau = float(out["monthly_active_users"]["value"])
    if out["peak_rps"]["value"] is None:
        # 30 requests per user per active day, 10 active days a month, peak 4x the average
        out["peak_rps"] = {"value": round(max(1.0, mau * 30 * 10 / SECONDS * 4), 1), "source": "derived",
                           "text": "30 requests per user on 10 active days a month, peak 4x the average"}
    if out["egress_gb"]["value"] is None:
        out["egress_gb"] = {"value": round(max(10.0, mau * 0.02), 1), "source": "derived",
                            "text": "about 20 MB of responses per user per month"}
    if out["region"]["value"] not in REGION:
        out["region"]["value"] = "us"
    return out


# --------------------------------------------------------------------------- pricing
def _vendor(n: Node, table: dict[str, dict[str, float]]) -> tuple[str, str]:
    """(vendor used for prices, note) for a component."""
    entry = catalog.match(n.model_dump()) if not n.vendor or n.vendor == "generic" else None
    vendor = n.vendor if n.vendor and n.vendor != "generic" else (entry["vendor"] if entry else "")
    if vendor in table:
        return vendor, ""
    if "any" in table:
        return "any", ""
    if vendor == "saas" and "oss" in table and "saas" not in table:
        return "aws", "priced like the AWS equivalent (no SaaS price in the price book)"
    return "aws", ("no platform chosen: priced like AWS" if not vendor or vendor == "generic"
                   else f"priced like the AWS equivalent (no {vendor} price in the price book)")


def kind_of(n: Node) -> str:
    if n.catalog_id:
        entry = next((c for c in catalog.CATALOG if c["id"] == n.catalog_id), None)
        if entry and entry["kind"]:
            return entry["kind"]
    entry = catalog.match(n.model_dump())
    if entry and entry.get("kind"):
        return entry["kind"]
    return TYPE_KIND.get(n.type, "")


def _prop(n: Node, *names: str) -> float | None:
    for name in names:
        raw = (n.props or {}).get(name)
        if raw is not None:
            try:
                return float(str(raw).replace(",", "").replace("$", "").strip())
            except ValueError:
                pass
    return None


def _llm_model(n: Node) -> str:
    t = f"{n.technology} {n.name}".lower()
    for key, model in (("opus", "claude-opus"), ("haiku", "claude-haiku"), ("sonnet", "claude-sonnet"),
                       ("claude", "claude-sonnet"), ("anthropic", "claude-sonnet"), ("gpt-4o-mini", "gpt-4o-mini"),
                       ("gpt-4o", "gpt-4o"), ("mini", "gpt-5-mini"), ("gpt", "gpt-5"), ("openai", "gpt-5"),
                       ("flash", "gemini-3.8-flash"), ("gemini", "gemini-3.1-pro"), ("vertex", "gemini-3.1-pro"),
                       ("bedrock", "claude-sonnet")):
        if key in t:
            return model
    return ""


def price_node(n: Node, a: dict[str, Any], table: dict[str, dict[str, dict[str, float]]], ha: bool,
               has_cdn: bool) -> dict[str, Any] | None:
    """One line item, or None for components that are not priced (notes, low-level elements)."""
    kind = kind_of(n)
    if not kind:
        return None
    base = {"node_id": n.id, "name": n.name, "type": n.type, "kind": kind, "technology": n.technology,
            "category": CATEGORY.get(kind.split(".")[0], "Other")}
    fixed = _prop(n, "monthly_cost", "monthly_usd", "cost_usd")
    if fixed is not None:
        return {**base, "vendor": n.vendor or "", "monthly": round(fixed, 2), "low": round(fixed, 2), "high": round(fixed, 2),
                "basis": "fixed amount from the component's monthly_cost property", "confidence": "high", "notes": []}
    if kind == "external":
        return {**base, "vendor": n.vendor or "", "monthly": 0.0, "low": 0.0, "high": 0.0, "confidence": "low",
                "basis": "third-party service: priced by its provider (add a monthly_cost property to include it)",
                "notes": []}
    vendors = table.get(kind) or {}
    vendor, note = _vendor(n, vendors)
    p = vendors.get(vendor) or {}
    mau = float(a["monthly_active_users"])
    peak = float(a["peak_rps"])
    avg = peak / 4
    monthly_req = avg * SECONDS
    req_m = monthly_req / 1e6
    storage = _prop(n, "storage_gb") or float(a["storage_gb"])
    notes = [note] if note else []
    usage = 1.8  # high/expected for usage-priced lines; instance lines use 1.25
    vcpu_override, inst_override = _prop(n, "vcpu"), _prop(n, "instances", "replicas")

    def instance_cost(vcpu: float, instances: float, mem_per_vcpu: float = 2.0) -> tuple[float, str]:
        if "vcpu_hour" not in p:
            return p.get("vm_month", 35) * instances, f"{instances:g} self-managed VM(s) × ${p.get('vm_month', 35):g}"
        per = vcpu * p["vcpu_hour"] * HOURS + vcpu * mem_per_vcpu * p.get("gb_hour", 0) * HOURS
        return per * instances, (f"{instances:g} × {vcpu:g} vCPU / {vcpu * mem_per_vcpu:g} GB × {HOURS} h "
                                 f"(${p['vcpu_hour']:g}/vCPU-h)")

    cost, basis, spread = 0.0, "", 1.25
    if kind in ("compute.container", "compute.kubernetes", "compute.vm", "ai.agent", "ai.mcp"):
        need = max(0.25 if kind == "ai.mcp" else 0.5, peak / RPS_PER_VCPU / (2 if kind == "ai.mcp" else 1))
        instances = inst_override or (2 if ha else 1)
        vcpu = vcpu_override or max(0.25, round(need / instances * 4) / 4)
        cost, basis = instance_cost(vcpu, instances)
        if kind == "compute.kubernetes" and p.get("control_month"):
            cost += p["control_month"]
            basis += f" + control plane ${p['control_month']:g}"
        if kind == "ai.agent":
            notes.append("model calls are counted on the LLM components")
    elif kind == "compute.function":
        if "requests_m" in p:
            gbs = monthly_req * 0.5 * 0.2  # 512 MB for 200 ms per request
            cost = req_m * p["requests_m"] + gbs * p["gb_second"]
            basis = f"{req_m:,.1f}M requests × ${p['requests_m']:g}/M + {gbs:,.0f} GB-s (512 MB, 200 ms each)"
            spread = usage
        else:
            cost, basis = instance_cost(1, 2 if ha else 1)
    elif kind == "edge.gateway":
        if "requests_m" in p:
            cost, basis, spread = req_m * p["requests_m"], f"{req_m:,.1f}M requests × ${p['requests_m']:g}/M", usage
        else:
            cost, basis = instance_cost(1, 2 if ha else 1)
    elif kind == "edge.lb":
        if "base_month" in p:
            cost = p["base_month"] + math.ceil(peak / 100) * p["per_100rps_month"]
            basis = f"${p['base_month']:g} + {math.ceil(peak / 100)} × ${p['per_100rps_month']:g} per 100 peak rps"
        else:
            cost, basis = instance_cost(1, 2 if ha else 1)
    elif kind == "edge.cdn":
        egress = float(a["egress_gb"])
        if "flat_month" in p:
            cost, basis = p["flat_month"], f"plan ${p['flat_month']:g}/month"
        else:
            cost, basis, spread = egress * p["egress_gb"], f"{egress:,.0f} GB out × ${p['egress_gb']:g}/GB", usage
    elif kind == "edge.dns":
        cost, basis = p.get("flat_month", 2), "hosted zone and queries"
    elif kind == "db.relational":
        if peak < 200:
            vcpu = 2
        elif peak < 1000:
            vcpu = 4
        elif peak < 5000:
            vcpu = 8
        else:
            vcpu = 16
        vcpu = vcpu_override or vcpu
        replicas = inst_override or (2 if ha else 1)
        compute = vcpu * p["vcpu_hour"] * HOURS * replicas
        cost = compute + storage * p["storage_gb"] * replicas
        basis = (f"{replicas:g} × {vcpu:g} vCPU instance{' (multi-AZ)' if replicas > 1 else ''} × ${p['vcpu_hour']:g}"
                 f"/vCPU-h + {storage:,.0f} GB × ${p['storage_gb']:g}" + (" × 2" if replicas > 1 else ""))
    elif kind == "db.document":
        if "reads_m" in p:
            reads, writes = req_m * 2 * 0.8, req_m * 2 * 0.2  # two database operations per request
            cost = reads * p["reads_m"] + writes * p["writes_m"] + storage * p["storage_gb"]
            basis = (f"{reads:,.1f}M reads × ${p['reads_m']:g} + {writes:,.1f}M writes × ${p['writes_m']:g} + "
                     f"{storage:,.0f} GB × ${p['storage_gb']:g}")
            spread = usage
        else:
            instances = inst_override or (3 if ha else 1)
            cost = p["vm_month"] * instances + storage * p["storage_gb"] * instances
            basis = f"{instances:g} self-managed node(s) × ${p['vm_month']:g} + storage"
    elif kind == "db.warehouse":
        if "scanned_tb" in p:
            scanned = max(1.0, storage / 1024 * 20)  # each byte scanned ~20 times a month
            cost = scanned * p["scanned_tb"] + storage * p["storage_gb"]
            basis = f"{scanned:,.1f} TB scanned × ${p['scanned_tb']:g} + {storage:,.0f} GB stored"
            spread = 2.5
        else:
            cost = p.get("base_month", p.get("vm_month", 140)) + storage * p["storage_gb"]
            basis = f"baseline ${p.get('base_month', p.get('vm_month', 140)):g} + {storage:,.0f} GB stored"
            spread = 2.0
    elif kind == "db.vector":
        cost = p.get("base_month", p.get("vm_month", 40)) * (2 if ha and "vm_month" in p else 1)
        basis = "smallest production tier" if "base_month" in p else "self-managed node(s)"
        spread = 2.0
    elif kind == "db.search":
        nodes = inst_override or (2 if ha else 1)
        unit = p.get("node_month", p.get("vm_month", 70))
        cost, basis = unit * nodes, f"{nodes:g} node(s) × ${unit:g}"
    elif kind == "cache.kv":
        if "commands_m" in p:
            cmds = req_m * 3
            cost = min(p["cap_month"], cmds * p["commands_m"])
            basis, spread = f"{cmds:,.1f}M commands × ${p['commands_m']:g}/M (capped at ${p['cap_month']:g})", usage
        elif "small_month" in p:
            unit = p["large_month"] if peak >= 2000 else p["small_month"]
            nodes = inst_override or (2 if ha else 1)
            cost, basis = unit * nodes, f"{nodes:g} {'large' if peak >= 2000 else 'small'} node(s) × ${unit:g}"
        else:
            nodes = inst_override or (2 if ha else 1)
            cost, basis = p["vm_month"] * nodes, f"{nodes:g} self-managed node(s) × ${p['vm_month']:g}"
    elif kind in ("msg.queue", "msg.pubsub"):
        msgs = req_m * 0.3 * 3  # 30% of requests enqueue work: send, receive, delete
        if "requests_m" in p:
            cost = p.get("base_month", 0) + msgs * p["requests_m"]
            basis, spread = f"{msgs:,.1f}M API calls × ${p['requests_m']:g}/M" + (
                f" + ${p['base_month']:g} base" if p.get("base_month") else ""), usage
        else:
            nodes = inst_override or (2 if ha else 1)
            cost, basis = p["vm_month"] * nodes, f"{nodes:g} self-managed broker(s) × ${p['vm_month']:g}"
    elif kind == "msg.stream":
        cost = p.get("base_month", p.get("vm_month", 105))
        basis, spread = "smallest production cluster (3 brokers)" if "vm_month" not in p else "3 self-managed brokers", 2.0
    elif kind == "storage.object":
        ops = req_m * 0.2
        cost = storage * p["storage_gb"] + ops * p.get("requests_m", 0)
        basis = f"{storage:,.0f} GB × ${p['storage_gb']:g}" + (f" + {ops:,.1f}M requests" if p.get("requests_m") else "")
        spread = usage
    elif kind == "storage.file":
        gb = max(storage, p.get("min_gb", 0))
        cost, basis = gb * p["storage_gb"], f"{gb:,.0f} GB × ${p['storage_gb']:g}" + (
            f" (minimum {p['min_gb']:g} GB)" if p.get("min_gb") else "")
    elif kind == "security.identity":
        if "mau" in p:
            billable = max(0.0, mau - p.get("free_mau", 0))
            cost = p.get("base_month", 0) + billable * p["mau"]
            basis = (f"{billable:,.0f} MAU above the {p.get('free_mau', 0):,.0f} free × ${p['mau']:g}"
                     + (f" + ${p['base_month']:g} plan" if p.get("base_month") else ""))
            spread = 1.5
        else:
            nodes = 2 if ha else 1
            cost, basis = p["vm_month"] * nodes, f"{nodes} self-managed node(s) × ${p['vm_month']:g}"
    elif kind == "security.secrets":
        cost = p.get("flat_month", p.get("vm_month", 35))
        basis = "about 20 secrets and their API calls" if "flat_month" in p else "self-managed node"
    elif kind == "security.waf":
        cost = p.get("base_month", 0) + req_m * p.get("requests_m", 0) + p.get("flat_month", 0)
        basis, spread = f"policy and rules + {req_m:,.1f}M requests inspected", usage
    elif kind == "ai.llm":
        calls = mau * float(a["llm_calls_per_user"])
        tin, tout = calls * float(a["llm_input_tokens"]), calls * float(a["llm_output_tokens"])
        model = _llm_model(n)
        usd, src = llm.estimate_cost(model, int(tin), int(tout)) if model else (0.0, "unknown")
        if src == "unknown" or not model:
            fb = (table.get("ai.llm") or {}).get("any") or {}
            usd = tin / 1e6 * fb.get("fallback_in_m", 3) + tout / 1e6 * fb.get("fallback_out_m", 15)
            notes.append("model not recognised: priced at $3 / $15 per million input / output tokens")
            model = model or "unknown model"
        cost = usd
        basis = (f"{calls:,.0f} calls × ({a['llm_input_tokens']:g} in + {a['llm_output_tokens']:g} out tokens) "
                 f"on {model}")
        spread, vendor = 2.5, n.vendor or vendor
    elif kind == "ai.ml":
        unit = p.get("endpoint_month", p.get("vm_month", 70))
        nodes = inst_override or (2 if ha else 1)
        cost, basis, spread = unit * nodes, f"{nodes:g} inference endpoint(s) × ${unit:g}", 1.6
    elif kind == "ops.monitoring":
        logs = max(1.0, monthly_req * 2 / 1e6)  # ~2 KB of logs per request
        if "host_month" in p:
            hosts = max(2, math.ceil(peak / RPS_PER_VCPU / 2))
            cost = hosts * p["host_month"] + logs * p["logs_gb"]
            basis = f"{hosts} hosts × ${p['host_month']:g} + {logs:,.0f} GB logs × ${p['logs_gb']:g}"
        elif "logs_gb" in p:
            billable = max(0.0, logs - p.get("free_gb", 0))
            cost = p.get("base_month", 0) + billable * p["logs_gb"]
            basis = f"{logs:,.0f} GB logs (≈2 KB per request) × ${p['logs_gb']:g}/GB"
        else:
            cost, basis = p["vm_month"], "self-managed monitoring node"
        spread = usage
    elif kind == "ops.workflow":
        if "transitions_m" in p:
            steps = req_m * 0.05 * 10  # 5% of requests start a 10-step workflow
            cost, basis, spread = steps * p["transitions_m"], f"{steps:,.2f}M state transitions × ${p['transitions_m']:g}/M", usage
        else:
            cost, basis = p["vm_month"] * (2 if ha else 1), "self-managed workflow servers"
    elif kind == "ops.email":
        emails = mau * float(a["emails_per_user"])
        if "flat_month" in p:
            extra = max(0.0, emails - p.get("included", 0))
            cost = p["flat_month"] + extra / 1000 * p["emails_k"]
            basis = f"plan ${p['flat_month']:g} ({p.get('included', 0):,.0f} emails) + {extra:,.0f} more"
        else:
            cost, basis = emails / 1000 * p["emails_k"], f"{emails:,.0f} emails × ${p['emails_k']:g}/1000"
        spread = 1.5
    elif kind == "ops.messaging":
        msgs = mau * float(a["messages_per_user"])
        cost, basis, spread = msgs * p.get("message", 0.0083), f"{msgs:,.0f} messages × ${p.get('message', 0.0083):g}", 1.5
        if not msgs:
            notes.append("set messages per user to price SMS / chat messages")
    elif kind == "ops.payments":
        cost, basis = 0.0, "transaction fees (e.g. 2.9% + $0.30 per card payment), not infrastructure"
    elif kind in ("client.web", "client.mobile"):
        cost = p.get("flat_month", 5)
        basis = ("static hosting" if kind == "client.web" else "app store developer accounts") + (
            "" if has_cdn or kind == "client.mobile" else "; data transfer is counted separately")
    else:
        return None
    region = REGION.get(str(a["region"]), 1.0)
    cost *= region
    low_f = 1 / spread if spread > 1.3 else 0.8
    return {**base, "vendor": vendor if vendor != "any" else (n.vendor or ""), "monthly": round(cost, 2),
            "low": round(cost * low_f, 2), "high": round(cost * spread, 2), "basis": basis,
            "confidence": "medium" if spread <= 1.3 else "low", "notes": notes}


def estimate(state: ProjectState, memory: list[dict[str, Any]] | None = None,
             overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """The cost estimate of the branch's top-level architecture (no recommendations)."""
    assum = assumptions(state, memory, overrides)
    a = {k: v["value"] for k, v in assum.items()}
    table = prices()
    ha = float(a["availability"]) >= 99.95
    nodes = [n for n in state.nodes.values() if not n.parent_id and n.type != "note"]
    has_cdn = any(kind_of(n) == "edge.cdn" for n in nodes)
    lines = [x for n in sorted(nodes, key=lambda n: n.name.lower()) if (x := price_node(n, a, table, ha, has_cdn))]
    if not has_cdn and lines:
        vendors = [ln["vendor"] for ln in lines if ln["vendor"] in ("aws", "azure", "gcp", "oss")]
        vendor = max(set(vendors), key=vendors.count) if vendors else "aws"
        p = table["transfer"].get(vendor, table["transfer"]["aws"])
        egress = float(a["egress_gb"])
        billable = max(0.0, egress - 100)  # the first 100 GB a month are free on the big clouds
        cost = billable * p["egress_gb"] * REGION.get(str(a["region"]), 1.0)
        lines.append({"node_id": "", "name": "Data transfer out", "type": "", "kind": "transfer", "technology": "",
                      "category": "Networking", "vendor": vendor, "monthly": round(cost, 2), "low": round(cost / 1.8, 2),
                      "high": round(cost * 1.8, 2), "confidence": "low", "notes": [],
                      "basis": f"{egress:,.0f} GB out (first 100 GB free) × ${p['egress_gb']:g}/GB; a CDN would carry most of it"})
    prod = {k: round(sum(ln[k] for ln in lines), 2) for k in ("monthly", "low", "high")}
    share = float(a["staging_share"]) if a["include_staging"] else 0.0
    staging = {k: round(v * share, 2) for k, v in prod.items()}
    total = {k: round(prod[k] + staging[k], 2) for k in prod}
    by_cat: dict[str, float] = {}
    for ln in lines:
        by_cat[ln["category"]] = round(by_cat.get(ln["category"], 0) + ln["monthly"], 2)
    drivers = sorted((ln for ln in lines if ln["monthly"] > 0), key=lambda ln: -ln["monthly"])[:3]
    out: dict[str, Any] = {
        "currency": "USD", "price_date": PRICE_DATE, "assumptions": assum, "high_availability": ha,
        "lines": lines, "production": prod, "staging": staging, "total": total,
        "annual": {k: round(v * 12, 2) for k, v in total.items()},
        "by_category": dict(sorted(by_cat.items(), key=lambda x: -x[1])),
        "drivers": [{"name": d["name"], "monthly": d["monthly"],
                     "share": round(d["monthly"] / prod["monthly"], 3) if prod["monthly"] else 0} for d in drivers],
        "unpriced": [n.name for n in nodes if not any(ln["node_id"] == n.id for ln in lines)],
        "disclaimer": (f"Indicative on-demand list prices ({PRICE_DATE}, {a['region']} region multiplier "
                       f"{REGION.get(str(a['region']), 1.0):g}) before discounts, free tiers (except where noted), tax "
                       "and support plans. Use it to compare options and find the big lines, then check the vendor's "
                       "pricing calculator."),
    }
    b = budget(state)
    if b:
        status = "within" if total["high"] <= b["monthly"] else "over" if total["low"] > b["monthly"] else "at_risk"
        out["budget"] = {**b, "status": status}
    return out


# --------------------------------------------------------------------------- recommendations
REC_SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "recommendations": {"type": "array", "items": {"type": "object", "properties": {
        "title": {"type": "string"}, "detail": {"type": "string"},
        "saving_monthly": {"type": "number"}, "components": {"type": "array", "items": {"type": "string"}},
        "tradeoff": {"type": "string"}, "effort": {"type": "string", "enum": ["low", "medium", "high"]}},
        "required": ["title", "detail", "saving_monthly", "components", "tradeoff", "effort"]}},
    "questions": {"type": "array", "items": {"type": "string"}}},
    "required": ["recommendations"]}


def offline_recommendations(est: dict[str, Any]) -> list[dict[str, Any]]:
    """Standard savings, from rules (used only without a model)."""
    recs = []
    lines = est["lines"]
    peak = float(est["assumptions"]["peak_rps"]["value"])
    steady = [ln for ln in lines if ln["confidence"] == "medium" and ln["monthly"] >= 100]
    if steady:
        s = sum(ln["monthly"] for ln in steady)
        recs.append({"title": "Commit to steady instances", "detail": "Reserved instances or savings plans for the "
                     "always-on lines typically cut 30-40%.", "saving_monthly": round(s * 0.3, 2),
                     "components": [ln["name"] for ln in steady], "tradeoff": "A 1- or 3-year commitment.",
                     "effort": "low"})
    if peak < 20:
        small = [ln for ln in lines if ln["kind"] in ("db.relational", "cache.kv", "compute.container")
                 and ln["monthly"] >= 40]
        if small:
            s = sum(ln["monthly"] for ln in small)
            recs.append({"title": "Use serverless tiers for low traffic", "detail": f"At {peak:g} requests per second "
                         "at peak, serverless databases, caches and containers that scale to zero cost less than "
                         "always-on instances.", "saving_monthly": round(s * 0.4, 2),
                         "components": [ln["name"] for ln in small], "tradeoff": "Cold starts and less predictable bills.",
                         "effort": "medium"})
    transfer = next((ln for ln in lines if ln["kind"] == "transfer"), None)
    if transfer and transfer["monthly"] >= 50:
        recs.append({"title": "Serve downloads through a CDN", "detail": "Data from the cloud to a CDN is free or "
                     "cheap, and CDN delivery costs less per GB than direct transfer.",
                     "saving_monthly": round(transfer["monthly"] * 0.3, 2), "components": [transfer["name"]],
                     "tradeoff": "Cache invalidation for changing content.", "effort": "low"})
    llms = [ln for ln in lines if ln["kind"] == "ai.llm"]
    if llms and est["production"]["monthly"] and sum(ln["monthly"] for ln in llms) / est["production"]["monthly"] > 0.3:
        s = sum(ln["monthly"] for ln in llms)
        recs.append({"title": "Route simple requests to a smaller model", "detail": "Model calls are a large share of "
                     "the bill: send routine requests to a fast, cheaper model, cache repeated prompts and keep the "
                     "large model for hard cases.", "saving_monthly": round(s * 0.4, 2),
                     "components": [ln["name"] for ln in llms], "tradeoff": "Needs an evaluation set to keep quality.",
                     "effort": "medium"})
    return recs


def recommend(est: dict[str, Any], state: ProjectState, project_name: str, org_id: str | None, *,
              use_ai: bool | None = None, version: str | None = None) -> tuple[list[dict[str, Any]], list[str], str]:
    """(recommendations, questions, engine)."""
    use_ai = llm.available(org_id) if use_ai is None else use_ai
    if use_ai:
        lines = [{k: ln[k] for k in ("name", "kind", "technology", "vendor", "monthly", "basis", "confidence")}
                 for ln in est["lines"]]
        nfrs = [{"id": r.id, "title": r.title, "category": r.category, "description": r.description[:300]}
                for r in active_requirements(state).values() if r.kind == "non_functional"]
        try:
            raw = agents.run_json(PROMPT, REC_SCHEMA, org_id=org_id, version=version,
                                  question=f"Find savings in the {project_name} architecture",
                                  variables={"project": project_name, "lines_json": json.dumps(lines, indent=1),
                                             "assumptions_json": json.dumps({k: v["value"] for k, v in
                                                                             est["assumptions"].items()}),
                                             "total": f"{est['production']['monthly']:,.0f}",
                                             "budget": json.dumps(est.get("budget") or {}),
                                             "requirements_json": json.dumps(nfrs, indent=1)})
            names = {ln["name"] for ln in est["lines"]}
            recs = []
            for r in raw.get("recommendations") or []:
                comps = [c for c in (r.get("components") or []) if c in names]
                saving = max(0.0, float(r.get("saving_monthly") or 0))
                cap = sum(ln["monthly"] for ln in est["lines"] if ln["name"] in comps) if comps else est["production"]["monthly"]
                recs.append({"title": str(r.get("title") or "")[:120], "detail": str(r.get("detail") or "")[:800],
                             "saving_monthly": round(min(saving, cap), 2), "components": comps,
                             "tradeoff": str(r.get("tradeoff") or "")[:300],
                             "effort": r.get("effort") if r.get("effort") in ("low", "medium", "high") else "medium"})
            questions = [str(q)[:200] for q in (raw.get("questions") or [])[:4] if str(q).strip()]
            return [r for r in recs if r["title"]][:8], questions, "llm"
        except llm.LLMUnavailable as exc:
            log.info("costs.optimize fell back to rules: %s", exc)
    return offline_recommendations(est), [], "offline"


def to_csv(est: dict[str, Any]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Component", "Category", "Kind", "Vendor", "Technology", "Monthly (USD)", "Low", "High", "Confidence",
                "Basis"])
    for ln in est["lines"]:
        w.writerow([ln["name"], ln["category"], ln["kind"], ln["vendor"], ln["technology"], f"{ln['monthly']:.2f}",
                    f"{ln['low']:.2f}", f"{ln['high']:.2f}", ln["confidence"], ln["basis"]])
    w.writerow([])
    for label, key in (("Production", "production"), ("Staging", "staging"), ("Total per month", "total"),
                       ("Total per year", "annual")):
        w.writerow([label, "", "", "", "", f"{est[key]['monthly']:.2f}", f"{est[key]['low']:.2f}",
                    f"{est[key]['high']:.2f}", "", ""])
    return buf.getvalue()


# --------------------------------------------------------------------------- routes
class EstimateIn(BaseModel):
    assumptions: dict[str, Any] = Field(default_factory=dict)
    ai: bool = True


def _overrides(raw: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k not in ASSUMPTION_KEYS or v is None or v == "":
            continue
        if k == "region":
            out[k] = str(v) if str(v) in REGION else "us"
        elif k == "include_staging":
            out[k] = bool(v)
        else:
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(f) or f < 0:
                continue
            out[k] = min(f, 1.0) if k == "staging_share" else min(f, 100.0) if k == "availability" else min(f, 1e12)
    return out


def register_routes(app: FastAPI, store: Any, *, authz: Any, state_or_404: Callable[[str, str], ProjectState]) -> None:
    read = authz.project("read")

    @app.post("/api/projects/{pid}/costs")
    async def cost_estimate(pid: str, body: EstimateIn, branch: str = "main", a=Depends(read)):
        state = state_or_404(pid, branch)
        memory = store.list_memory(pid)

        def run() -> dict[str, Any]:
            est = estimate(state, memory, _overrides(body.assumptions))
            recs, questions, engine = recommend(est, state, a.project["name"], a.org_id,
                                                use_ai=body.ai and llm.available(a.org_id))
            return {**est, "branch": branch, "recommendations": recs, "questions": questions, "engine": engine}

        return await asyncio.to_thread(run)

    @app.get("/api/projects/{pid}/costs.csv")
    def cost_csv(pid: str, branch: str = "main", a=Depends(read)):
        est = estimate(state_or_404(pid, branch), store.list_memory(pid))
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{a.project['name']}-{branch}-costs").strip("-")[:80]
        return Response(to_csv(est), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})

