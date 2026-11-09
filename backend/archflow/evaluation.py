"""Evaluate 2–4 architecture alternatives (draft branches) against the project's requirements.

Engines; the result always says which one produced the scores (``engine``):

1. ``"jev"``: the Jev decision API (``jev.py``, when JEV_API_KEY is set) scores every alternative on every
   criterion and picks the best fit. It runs in parallel with
2. ``"llm"``: a Pydantic AI agent on the LLM gateway (prompt ``compare.evaluate``) that scores, explains every
   score, lists strengths, risks and follow-up questions. With Jev it explains Jev's scores and is kept as a
   ``second_opinion`` (``agrees`` says whether both picked the same option); without Jev its scores are used.
3. ``"heuristic"``: a deterministic offline estimate from the model's structure (component counts,
   caches, queues, auth, replicas) and ``analysis.health``.

Result shape::

    {"engine": "jev" | "llm" | "heuristic", "recommended": <branch>, "alternatives": [<branch>, ...],
     "criteria": [...], "scores": {branch: {criterion: number 1..5}}, "totals": {branch: number},
     "rationale": str, "confidence": float | None, "notes": [why earlier engines were skipped], "raw": ...,
     "weights": {criterion: number},             # NFR-derived weights behind ``totals``
     "details": {branch: {"summary": str, "strengths": [str], "risks": [str], "best_when": str,
                          "reasons": {criterion: str}}},
     "deciding_factors": [str], "flip_conditions": str, "follow_up_questions": [str],
     "probabilities": {branch: p},                # jev only
     "second_opinion": {"engine": "llm", "recommended", "totals", "scores", "rationale", "agrees"}}  # jev + llm

``ask`` answers a follow-up question about a comparison (LLM only).
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from . import agents, jev, llm
from .agents import tracing
from .analysis import health, validate_architecture
from .models import ProjectState, active_requirements

log = logging.getLogger("archflow.evaluation")


# Criterion -> what a 5 means. Every scale is "higher is better" (cost 5 = cheapest).
CRITERIA: dict[str, str] = {
    "cost": "Cost efficiency: infrastructure, licensing and running cost (5 = cheapest to build and run)",
    "scalability": "Scalability: ability to handle growth in users, data and traffic (5 = scales furthest with least rework)",
    "performance": "Performance: latency and throughput for the main user flows (5 = fastest)",
    "security": "Security: authentication, authorization, data protection, attack surface (5 = strongest posture)",
    "reliability": "Reliability: availability, fault tolerance, recovery from failure (5 = most resilient)",
    "operability": "Operability: ease of deploying, monitoring, debugging and maintaining (5 = easiest to operate)",
    "delivery_speed": "Delivery speed: how quickly a typical team can build and ship it (5 = fastest to deliver)",
}
CRITERIA_LABELS = {k: v.split(":")[0] for k, v in CRITERIA.items()}
SCALE = [
    "1 - Very poor: clearly fails this criterion for the stated requirements",
    "2 - Poor: significant gaps or risks",
    "3 - Adequate: acceptable with some trade-offs",
    "4 - Good: meets the requirements well",
    "5 - Excellent: best-in-class for these requirements",
]
# NFR categories that raise a criterion's weight when ranking alternatives.
NFR_WEIGHTS = {"cost": "cost", "scalability": "scalability", "performance": "performance", "latency": "performance",
               "security": "security", "privacy": "security", "availability": "reliability",
               "reliability": "reliability", "observability": "operability", "maintainability": "operability"}


# --------------------------------------------------------------------------- state building
def architecture_summary(state: ProjectState, limit: int = 80) -> dict[str, Any]:
    """A compact, model-friendly description of one alternative's architecture."""
    names = {n.id: n.name for n in state.nodes.values()}
    comps = []
    for n in list(state.nodes.values())[:limit]:
        if n.type == "note":
            continue
        c: dict[str, Any] = {"name": n.name, "type": n.type}
        if n.technology:
            c["technology"] = n.technology
        if n.vendor:
            c["vendor"] = n.vendor
        if n.description:
            c["description"] = n.description[:200]
        if n.props:
            c["props"] = dict(list(n.props.items())[:6])
        comps.append(c)
    conns = []
    for e in list(state.edges.values())[: limit * 2]:
        line = f"{names.get(e.source, e.source)} -> {names.get(e.target, e.target)}"
        if e.protocol or e.label:
            line += f" ({e.protocol or e.label})"
        conns.append(line)
    return {"components": comps, "connections": conns,
            "counts": {"components": len(comps), "connections": len(state.edges)}}


def requirements_summary(state: ProjectState) -> list[dict[str, Any]]:
    out = []
    for r in active_requirements(state).values():
        item: dict[str, Any] = {"id": r.id, "title": r.title, "kind": r.kind, "priority": r.priority}
        if r.category:
            item["category"] = r.category
        if r.description:
            item["description"] = r.description[:400]
        if r.acceptance_criteria:
            item["acceptance_criteria"] = r.acceptance_criteria[:6]
        out.append(item)
    return out


def merged_requirements(base: ProjectState | None, alternatives: dict[str, ProjectState]) -> ProjectState:
    """Requirements from the base branch plus any extra ones that only exist on an alternative."""
    out = ProjectState()
    for st in [base, *alternatives.values()]:
        if st is None:
            continue
        for rid, r in active_requirements(st).items():
            out.requirements.setdefault(rid, r)
    return out


def option_keys(branches: list[str]) -> dict[str, str]:
    """Stable, API-safe option keys (A, B, C, D) for branch names."""
    return {chr(ord("A") + i): b for i, b in enumerate(branches)}


def build_state(requirements: ProjectState, alternatives: dict[str, ProjectState]) -> dict[str, Any]:
    keys = option_keys(list(alternatives))
    return {
        "task": "Compare candidate software architectures for one system and judge them against its requirements.",
        "requirements": requirements_summary(requirements),
        "alternatives": {k: {"branch": b, **architecture_summary(alternatives[b])} for k, b in keys.items()},
    }


# --------------------------------------------------------------------------- HTTP client
# --------------------------------------------------------------------------- scoring helpers
def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def criterion_weights(requirements: ProjectState) -> dict[str, float]:
    w = {c: 1.0 for c in CRITERIA}
    for r in active_requirements(requirements).values():
        if r.kind != "non_functional":
            continue
        crit = NFR_WEIGHTS.get(r.category.lower().strip())
        if crit:
            w[crit] += {"critical": 1.5, "high": 1.0, "medium": 0.5, "low": 0.25}.get(r.priority, 0.5)
    return w


def _finish(engine: str, branches: list[str], scores: dict[str, dict[str, float]], recommended: str | None,
            rationale: str, weights: dict[str, float] | None) -> dict[str, Any]:
    w = weights or {c: 1.0 for c in CRITERIA}
    totals: dict[str, float] = {}
    for b in branches:
        vals = [(scores[b][c], w[c]) for c in CRITERIA if c in scores.get(b, {})]
        totals[b] = round(sum(v * wt for v, wt in vals) / sum(wt for _, wt in vals), 2) if vals else 0.0
    if recommended not in branches:
        recommended = max(branches, key=lambda b: (totals[b], -branches.index(b)))
    return {"engine": engine, "recommended": recommended, "alternatives": list(branches),
            "criteria": list(CRITERIA), "criteria_labels": CRITERIA_LABELS, "scores": scores, "totals": totals,
            "rationale": rationale, "confidence": None, "notes": [], "weights": {c: round(w[c], 2) for c in CRITERIA},
            "details": {b: {"summary": "", "strengths": [], "risks": [], "best_when": "", "reasons": {}}
                        for b in branches},
            "deciding_factors": [], "flip_conditions": "", "follow_up_questions": []}


def _strengths_text(result: dict[str, Any]) -> str:
    rec = result["recommended"]
    s = result["scores"].get(rec, {})
    if not s:
        return ""
    best = [CRITERIA_LABELS[c].lower() for c, v in s.items() if v >= 4]
    weak = [CRITERIA_LABELS[c].lower() for c, v in s.items() if v <= 2]
    parts = []
    if best:
        parts.append(f"Strongest on {', '.join(best)}.")
    if weak:
        parts.append(f"Weakest on {', '.join(weak)}.")
    return " ".join(parts)


# --------------------------------------------------------------------------- engines
EVALUATE_PROMPT = "compare.evaluate"  # prompts/compare.evaluate.yaml
ASK_PROMPT = "compare.ask"            # prompts/compare.ask.yaml


def _str_list(value: Any, limit: int = 5) -> list[str]:
    return [str(x).strip()[:240] for x in (value or []) if str(x).strip()][:limit] if isinstance(value, list) else []


def _llm_schema(keys: list[str]) -> dict[str, Any]:
    crit_props = {c: {"type": "integer", "minimum": 1, "maximum": 5} for c in CRITERIA}
    reason_props = {c: {"type": "string"} for c in CRITERIA}
    strings = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "properties": {
            "recommended": {"type": "string", "enum": keys},
            "rationale": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "deciding_factors": strings,
            "flip_conditions": {"type": "string"},
            "follow_up_questions": strings,
            "scores": {"type": "array", "items": {
                "type": "object",
                "properties": {"alternative": {"type": "string", "enum": keys}, **crit_props,
                               "reasons": {"type": "object", "properties": reason_props},
                               "summary": {"type": "string"}, "strengths": strings, "risks": strings,
                               "best_when": {"type": "string"}},
                "required": ["alternative", *CRITERIA, "reasons", "summary", "strengths", "risks", "best_when"],
            }},
        },
        "required": ["recommended", "rationale", "scores", "confidence", "deciding_factors", "flip_conditions",
                     "follow_up_questions"],
    }


def evaluate_llm(requirements: ProjectState, alternatives: dict[str, ProjectState],
                 org_id: str | None = None) -> dict[str, Any]:
    branches = list(alternatives)
    keys = option_keys(branches)
    data = agents.run_json(EVALUATE_PROMPT, _llm_schema(list(keys)), org_id=org_id, variables={
        "criteria": "\n".join(f"- {c}: {d}" for c, d in CRITERIA.items()), "scale": "\n".join(SCALE),
        "input_json": json.dumps(build_state(requirements, alternatives), indent=1), "keys": ", ".join(keys)})
    scores: dict[str, dict[str, float]] = {b: {} for b in branches}
    rows: dict[str, dict[str, Any]] = {}
    for row in data.get("scores") or []:
        b = keys.get(row.get("alternative"))
        if not b:
            continue
        rows[b] = row
        for c in CRITERIA:
            v = _num(row.get(c))
            if v is not None:
                scores[b][c] = float(min(5, max(1, round(v))))
    out = _finish("llm", branches, scores, keys.get(data.get("recommended")), str(data.get("rationale", "")).strip(),
                  criterion_weights(requirements))
    for b, row in rows.items():
        reasons = row.get("reasons") if isinstance(row.get("reasons"), dict) else {}
        out["details"][b] = {
            "summary": str(row.get("summary") or "").strip(),
            "strengths": _str_list(row.get("strengths")), "risks": _str_list(row.get("risks")),
            "best_when": str(row.get("best_when") or "").strip(),
            "reasons": {c: str(reasons[c]).strip()[:300] for c in CRITERIA if str(reasons.get(c) or "").strip()},
        }
    out["deciding_factors"] = _str_list(data.get("deciding_factors"))
    out["flip_conditions"] = str(data.get("flip_conditions") or "").strip()
    out["follow_up_questions"] = _str_list(data.get("follow_up_questions"), 4)
    conf = _num(data.get("confidence"))
    out["confidence"] = round(min(1.0, max(0.0, conf)), 2) if conf is not None else None
    if not out["rationale"]:
        out["rationale"] = _strengths_text(out)
    return out


_CACHE_RE = re.compile(r"redis|memcache|cache|cdn|cloudfront|valkey", re.I)
_QUEUE_RE = re.compile(r"kafka|rabbit|sqs|sns|pub/?sub|queue|event ?bus|kinesis|nats|service bus", re.I)
_AUTH_RE = re.compile(r"auth|identity|cognito|keycloak|oauth|okta|iam|entra|sso|waf", re.I)
_OBS_RE = re.compile(r"monitor|observab|grafana|prometheus|datadog|logging|tracing|otel|cloudwatch|sentry", re.I)
_REPLICA_RE = re.compile(r"replica|cluster|multi-?az|ha\b|failover|autoscal|load balancer|\blb\b", re.I)


def _features(state: ProjectState) -> dict[str, Any]:
    nodes = [n for n in state.nodes.values() if n.type != "note"]
    text = lambda n: f"{n.name} {n.technology} {n.description} {' '.join(f'{k} {v}' for k, v in n.props.items())}"  # noqa: E731
    types = [n.type for n in nodes]
    replicas = 0
    for n in nodes:
        r = _num(n.props.get("replicas")) if n.props else None
        if (r and r > 1) or _REPLICA_RE.search(text(n)):
            replicas += 1
    issues = validate_architecture(state)
    return {
        "n": len(nodes), "edges": len(state.edges),
        "cache": "cache" in types or any(_CACHE_RE.search(text(n)) for n in nodes),
        "queue": "queue" in types or any(_QUEUE_RE.search(text(n)) for n in nodes),
        "auth": "security" in types or any(_AUTH_RE.search(text(n)) for n in nodes),
        "gateway": "gateway" in types,
        "obs": any(_OBS_RE.search(text(n)) for n in nodes),
        "replicas": replicas,
        "services": types.count("service") + types.count("agent"),
        "datastores": types.count("database") + types.count("storage"),
        "managed": sum(1 for n in nodes if n.vendor in ("aws", "azure", "gcp", "saas")),
        "errors": sum(1 for i in issues if i["severity"] == "error"),
        "arch_score": health(state)["architecture_score"],
    }


def _clamp(v: float) -> float:
    return round(min(5.0, max(1.0, v)), 1)


def heuristic_scores(state: ProjectState) -> dict[str, float]:
    f = _features(state)
    n, quality = f["n"], f["arch_score"] / 100
    managed_share = f["managed"] / n if n else 0
    return {
        "cost": _clamp(5 - max(0, n - 4) * 0.25 - f["replicas"] * 0.3 - (0.3 if f["queue"] else 0)),
        "scalability": _clamp(1.5 + 1.0 * f["cache"] + 1.0 * f["queue"] + 0.5 * min(f["replicas"], 2)
                              + 0.5 * f["gateway"] + 0.5 * (f["services"] > 1)),
        "performance": _clamp(2.0 + 1.5 * f["cache"] + 0.5 * f["queue"] + 0.3 * min(f["replicas"], 2)
                              - 0.1 * max(0, n - 10)),
        "security": _clamp(1.5 + 1.5 * f["auth"] + 1.0 * f["gateway"] + 0.5 * quality - 0.75 * f["errors"]),
        "reliability": _clamp(1.5 + 0.75 * min(f["replicas"], 2) + 1.0 * f["queue"] + 1.0 * quality
                              + 0.5 * f["obs"] - 0.5 * f["errors"]),
        "operability": _clamp(1.5 + 2.0 * quality + 1.0 * f["obs"] + 0.75 * managed_share - 0.08 * max(0, n - 8)),
        "delivery_speed": _clamp(5 - max(0, n - 3) * 0.3 + 0.75 * managed_share),
    }


def heuristic_details(state: ProjectState) -> dict[str, Any]:
    """Strengths, risks and per-criterion reasons read off the same structural features the scores use."""
    f = _features(state)
    has = {"cache": "a cache", "queue": "a message queue", "auth": "an identity / auth component",
           "gateway": "an API gateway", "obs": "monitoring / observability"}
    strengths = [f"Has {label}" for k, label in has.items() if f[k]]
    if f["replicas"]:
        strengths.append(f"{f['replicas']} component(s) replicated or load-balanced")
    if f["n"] <= 5:
        strengths.append(f"Small footprint ({f['n']} components): quick to build and cheap to run")
    missing = {"cache": "No cache in front of the data stores", "queue": "No queue: work is synchronous end to end",
               "auth": "No identity / auth component", "obs": "No monitoring or observability"}
    risks = [text for k, text in missing.items() if not f[k]]
    if not f["replicas"]:
        risks.append("No replicated components: single points of failure")
    if f["errors"]:
        risks.append(f"{f['errors']} architecture error(s) in the health check")
    if f["n"] > 12:
        risks.append(f"Large footprint ({f['n']} components) to build and operate")
    yn = lambda k: "has" if f[k] else "has no"  # noqa: E731
    reasons = {
        "cost": f"{f['n']} components, {f['replicas']} replicated" + (", plus a queue to run" if f["queue"] else ""),
        "scalability": f"{yn('cache').capitalize()} cache, {yn('queue')} queue, {f['services']} service(s)",
        "performance": f"{yn('cache').capitalize()} cache" + (", async work via a queue" if f["queue"] else ""),
        "security": f"{yn('auth').capitalize()} auth component, {yn('gateway')} gateway",
        "reliability": f"{f['replicas']} replicated component(s), {yn('obs')} observability",
        "operability": f"Health score {f['arch_score']}/100, {f['managed']} managed service(s)",
        "delivery_speed": f"{f['n']} components, {f['managed']} managed service(s)",
    }
    return {"summary": f"{f['n']} components, {f['edges']} connections, {f['datastores']} data store(s).",
            "strengths": strengths[:4], "risks": risks[:4], "best_when": "", "reasons": reasons}


def evaluate_heuristic(requirements: ProjectState, alternatives: dict[str, ProjectState]) -> dict[str, Any]:
    branches = list(alternatives)
    scores = {b: heuristic_scores(s) for b, s in alternatives.items()}
    out = _finish("heuristic", branches, scores, None, "", criterion_weights(requirements))
    out["details"] = {b: heuristic_details(s) for b, s in alternatives.items()}
    w = criterion_weights(requirements)
    emphasis = [CRITERIA_LABELS[c].lower() for c, v in w.items() if v > 1]
    out["rationale"] = (
        f"Offline estimate from each draft's structure (component counts, caches, queues, auth, replicas, "
        f"observability and the architecture health score). '{out['recommended']}' has the best weighted average "
        f"({out['totals'][out['recommended']]} / 5)"
        + (f", with extra weight on {', '.join(emphasis)} from the NFRs" if emphasis else "") + ". "
        + _strengths_text(out) + " Configure an LLM key (AI settings) for a requirement-aware evaluation.")
    return out


def evaluate_jev(requirements: ProjectState, alternatives: dict[str, ProjectState],
                 org_id: str | None = None, **kw: Any) -> dict[str, Any]:
    """Scores and the recommendation from the Jev decision API (traced as an API call)."""
    branches = list(alternatives)
    keys = option_keys(branches)
    state, questions = build_state(requirements, alternatives), jev.build_questions(keys, CRITERIA, SCALE)
    with tracing.trace_run("jev.systemone@api", "compare.jev", org_id=org_id,
                           input={"questions": len(questions), "alternatives": branches}) as tr:
        with tracing.api_span("jev.systemone", {"alternatives": branches, "questions": list(questions)}) as span:
            parsed = jev.parse(jev.call(state, questions, **kw), keys, list(CRITERIA))
            if span is not None:
                span.output = tracing.clip({k: parsed[k] for k in ("recommended", "probabilities", "confidence", "usage")})
                span.model = str(parsed.get("model") or "jev")
        out = _finish("jev", branches, parsed["scores"], parsed["recommended"], "", criterion_weights(requirements))
        out["confidence"], out["probabilities"] = parsed["confidence"], parsed["probabilities"]
        pct = (f" ({round(100 * parsed['probabilities'][out['recommended']])}% probability)"
               if out["recommended"] in parsed["probabilities"] else "")
        out["rationale"] = f"Jev picked '{out['recommended']}' as the best fit for the requirements{pct}. " + _strengths_text(out)
        tracing.set_answer(tr, {"recommended": out["recommended"], "totals": out["totals"]})
        return out


_EXPLAINS = ("details", "deciding_factors", "flip_conditions", "follow_up_questions")


def _merge(jev_out: dict[str, Any], llm_out: dict[str, Any]) -> dict[str, Any]:
    """Jev's scores and pick, explained by the LLM agent's reasons, strengths, risks and follow-ups. The agent's
    own pick is kept as a second opinion, and the result says whether the two agree."""
    out = dict(jev_out)
    for k in _EXPLAINS:
        out[k] = llm_out.get(k) or out.get(k)
    agree = llm_out["recommended"] == jev_out["recommended"]
    out["second_opinion"] = {"engine": "llm", "recommended": llm_out["recommended"], "totals": llm_out["totals"],
                             "scores": llm_out["scores"], "rationale": llm_out["rationale"],
                             "confidence": llm_out.get("confidence"), "agrees": agree}
    out["rationale"] = (f"{jev_out['rationale']} The AI review agrees: {llm_out['rationale']}" if agree else
                        f"{jev_out['rationale']} The AI review would pick '{llm_out['recommended']}' instead: "
                        f"{llm_out['rationale']}")
    return out


def evaluate(requirements: ProjectState, alternatives: dict[str, ProjectState],
             org_id: str | None = None) -> dict[str, Any]:
    """Compare 2–4 alternatives (branch name → state).

    Jev (decision API, when JEV_API_KEY is set) and the LLM agent run in parallel: Jev scores and picks, the agent
    explains (reasons per score, strengths, risks, follow-ups) and gives a second opinion. If Jev fails the agent's
    own scores are used; with neither, an offline heuristic."""
    if not 2 <= len(alternatives) <= 4:
        raise ValueError("compare between 2 and 4 alternatives")
    notes: list[str] = []
    tasks: dict[str, Any] = {}
    if jev.available():
        tasks["jev"] = lambda: evaluate_jev(requirements, alternatives, org_id=org_id)
    else:
        notes.append("Jev not configured")
    if llm.available(org_id):
        tasks["llm"] = lambda: evaluate_llm(requirements, alternatives, org_id=org_id)
    else:
        notes.append("no LLM configured")
    results = agents.parallel(tasks, name="compare.panel", org_id=org_id,
                              question=f"Which of {', '.join(alternatives)} fits the requirements best?") if tasks else {}
    j, a = results.get("jev"), results.get("llm")
    for name, r in (("Jev", j), ("LLM evaluation", a)):
        if isinstance(r, Exception):
            log.warning("%s failed: %s", name, r)
            notes.append(f"{name} failed: {type(r).__name__}: {str(r)[:160]}")
    if isinstance(j, dict):
        out = _merge(j, a) if isinstance(a, dict) else j
    elif isinstance(a, dict):
        out = a
    else:
        out = evaluate_heuristic(requirements, alternatives)
    out["notes"] = notes
    return out


ASK_SCHEMA = {"type": "object", "properties": {
    "answer": {"type": "string"}, "follow_ups": {"type": "array", "items": {"type": "string"}}},
    "required": ["answer", "follow_ups"]}


def ask(requirements: ProjectState, alternatives: dict[str, ProjectState], evaluation: dict[str, Any] | None,
        question: str, org_id: str | None = None) -> dict[str, Any]:
    """{"answer", "follow_ups"} for a follow-up question about a comparison. Raises llm.LLMUnavailable."""
    data = build_state(requirements, alternatives)
    data["alternatives"] = {v["branch"]: {k: x for k, x in v.items() if k != "branch"}
                            for v in data["alternatives"].values()}
    if evaluation:
        data["evaluation"] = {k: evaluation.get(k) for k in
                              ("engine", "recommended", "scores", "totals", "weights", "rationale", "details",
                               "deciding_factors", "flip_conditions") if evaluation.get(k)}
    out = agents.run_json(ASK_PROMPT, ASK_SCHEMA, org_id=org_id,
                          variables={"input_json": json.dumps(data, indent=1), "question": question})
    return {"answer": str(out.get("answer") or "").strip(), "follow_ups": _str_list(out.get("follow_ups"), 3)}
