"""Guided design chat: requirements by multiple choice, then architecture options to choose from.

Each turn returns a *card* the UI renders as selectable options:

* ``question`` — one question with 2–5 options (single or multi-select, one marked recommended,
  free-text "other" always allowed).
* ``architectures`` — 2–4 alternative architectures with pros/cons, cost and complexity. Choosing one
  applies it to the canvas (or to a new draft branch) through the normal operation pipeline.

The server is stateless: the client sends the conversation each turn. With an LLM configured the
interviewer adapts its questions to the idea; without one, a built-in question bank and architecture
composer produce the same card shapes so the flow always works.
"""

from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from . import checklists, option_review
from .models import NODE_TYPES, ProjectState, active_requirements, requirement_number
from .ops import next_requirement_id, slugify, unique_slug

log = logging.getLogger("archflow.chat")

MAX_QUESTIONS = 7
LAYER_Y = 170
COL_X = 260


# --------------------------------------------------------------------------- conversation helpers
def _answers(turns: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map question id → {"labels": [...], "option_ids": [...], "other": str} from the transcript."""
    out: dict[str, dict[str, Any]] = {}
    pending: dict[str, Any] | None = None
    for t in turns:
        card = t.get("card")
        if t.get("role") == "assistant" and card and card.get("type") == "question":
            pending = card
        elif t.get("role") == "user" and t.get("answer") and pending:
            ans = t["answer"]
            ids = ans.get("option_ids") or []
            labels = [o["label"] for o in pending.get("options", []) if o["id"] in ids]
            if ans.get("other"):
                labels.append(ans["other"])
            out[pending["id"]] = {"option_ids": ids, "labels": labels, "other": ans.get("other", ""),
                                  "question": pending.get("question", "")}
            pending = None
    return out


def _idea(turns: list[dict[str, Any]]) -> str:
    for t in turns:
        if t.get("role") == "user" and not t.get("answer") and t.get("content"):
            return t["content"]
    return ""


def _wants_options_now(turns: list[dict[str, Any]]) -> bool:
    last = next((t for t in reversed(turns) if t.get("role") == "user"), None)
    text = (last or {}).get("content", "").lower()
    return bool(re.search(r"\b(skip|propose|show (me )?(the )?options|enough|go ahead|generate)\b", text))


def transcript_text(turns: list[dict[str, Any]]) -> str:
    lines = []
    pending = None
    for t in turns:
        card = t.get("card")
        if t.get("role") == "assistant":
            if card and card.get("type") == "question":
                pending = card
                opts = "; ".join(o["label"] for o in card.get("options", []))
                lines.append(f"ArchFlow asked: {card['question']} (options: {opts})")
            elif card and card.get("type") == "architectures":
                lines.append("ArchFlow proposed architectures: " + ", ".join(a["name"] for a in card.get("options", [])))
            elif t.get("content"):
                lines.append(f"ArchFlow: {t['content']}")
        else:
            if t.get("answer") and pending:
                ids = t["answer"].get("option_ids") or []
                chosen = [o["label"] for o in pending.get("options", []) if o["id"] in ids]
                if t["answer"].get("other"):
                    chosen.append(f"other: {t['answer']['other']}")
                lines.append(f"User chose: {', '.join(chosen) or '(nothing)'}")
                pending = None
            elif t.get("content"):
                lines.append(f"User: {t['content']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- offline question bank
QUESTION_BANK: list[dict[str, Any]] = [
    {"id": "users", "question": "Who will use the system?", "why": "Determines authentication, roles and entry points.",
     "multi": True, "options": [
         {"id": "customers", "label": "External customers", "description": "Public sign-up, self-service"},
         {"id": "employees", "label": "Internal employees", "description": "SSO, admin and back-office tools"},
         {"id": "partners", "label": "Partners / developers", "description": "API keys, integrations"},
         {"id": "agents", "label": "AI agents", "description": "Other agents call us via MCP / A2A"}]},
    {"id": "channels", "question": "Which channels should it support?", "why": "Each channel adds a client and an integration path.",
     "multi": True, "options": [
         {"id": "web", "label": "Web app", "description": "Browser SPA", "recommended": True},
         {"id": "mobile", "label": "Mobile apps", "description": "iOS / Android"},
         {"id": "messaging", "label": "WhatsApp / Telegram / Slack", "description": "Chat-based access through webhooks"},
         {"id": "api", "label": "Public API", "description": "REST/GraphQL for third parties"},
         {"id": "voice", "label": "Voice / meetings", "description": "Phone or video-call agents"}]},
    {"id": "scale", "question": "What scale do you expect in the first year?", "why": "Drives the choice between a monolith, serverless and distributed services.",
     "multi": False, "options": [
         {"id": "s", "label": "< 1,000 users", "description": "Pilot / internal"},
         {"id": "m", "label": "1k – 100k users", "description": "Growing product", "recommended": True},
         {"id": "l", "label": "100k – 1M users", "description": "High traffic, needs horizontal scaling"},
         {"id": "xl", "label": "> 1M users", "description": "Global scale, multi-region"}]},
    {"id": "cloud", "question": "Where should it run?", "why": "Maps generic components to concrete managed services.",
     "multi": False, "options": [
         {"id": "aws", "label": "AWS", "description": "Lambda/ECS, RDS, SQS, S3"},
         {"id": "azure", "label": "Azure", "description": "Functions/AKS, Azure SQL, Service Bus"},
         {"id": "gcp", "label": "Google Cloud", "description": "Cloud Run, Cloud SQL, Pub/Sub"},
         {"id": "agnostic", "label": "Cloud-agnostic (Kubernetes)", "description": "Portable, open-source stack", "recommended": True}]},
    {"id": "ai", "question": "What AI capabilities does it need?", "why": "Adds LLM, retrieval, agent and tool (MCP) components.",
     "multi": True, "options": [
         {"id": "none", "label": "None", "description": "Traditional application"},
         {"id": "assistant", "label": "Chat assistant / RAG", "description": "Answers from your documents and data"},
         {"id": "agents", "label": "Autonomous agents with tools", "description": "Agents act on systems via MCP"},
         {"id": "ml", "label": "Predictions / ML models", "description": "Scoring, recommendations, forecasting"}]},
    {"id": "data", "question": "What kind of data will it manage?", "why": "Chooses the right data stores.",
     "multi": True, "options": [
         {"id": "transactional", "label": "Transactional records", "description": "Orders, accounts, payments", "recommended": True},
         {"id": "documents", "label": "Flexible documents", "description": "Varying schemas, JSON"},
         {"id": "files", "label": "Files & media", "description": "Uploads, images, PDFs"},
         {"id": "analytics", "label": "Analytics & reporting", "description": "Dashboards, BI, history"}]},
    {"id": "priority", "question": "What matters most right now?", "why": "Used to rank the architecture options.",
     "multi": False, "options": [
         {"id": "speed", "label": "Time to market", "description": "Ship quickly with a small team", "recommended": True},
         {"id": "cost", "label": "Low running cost", "description": "Pay per use, scale to zero"},
         {"id": "scale", "label": "Scalability", "description": "Independent scaling of parts"},
         {"id": "compliance", "label": "Security & compliance", "description": "Audit, isolation, data residency"}]},
]


def offline_turn(turns: list[dict[str, Any]], state: ProjectState) -> dict[str, Any]:
    answers = _answers(turns)
    if not _wants_options_now(turns):
        for q in QUESTION_BANK:
            if q["id"] not in answers:
                n = len(answers)
                intro = ("Let's shape the requirements. Pick what fits — you can choose “Other” any time, "
                         "or say “show options” to skip ahead." if n == 0 else "Got it.")
                return {"message": intro, "card": {"type": "question", **q, "step": n + 1, "total": len(QUESTION_BANK)}}
    return {"message": "Here are architecture options based on your answers. Pick one to put it on the canvas.",
            "card": compose_architectures(_idea(turns), answers)}


# --------------------------------------------------------------------------- offline architecture composer
TECH = {
    "aws": {"gateway": "Amazon API Gateway", "auth": "Amazon Cognito", "db": "Amazon RDS PostgreSQL", "docdb": "Amazon DynamoDB",
            "cache": "ElastiCache Redis", "queue": "Amazon SQS", "stream": "Amazon MSK (Kafka)", "storage": "Amazon S3",
            "fn": "AWS Lambda", "container": "Amazon ECS Fargate", "warehouse": "Amazon Redshift", "llm": "Amazon Bedrock (Claude)",
            "vector": "Amazon OpenSearch Service", "cdn": "CloudFront"},
    "azure": {"gateway": "Azure API Management", "auth": "Microsoft Entra ID", "db": "Azure Database for PostgreSQL", "docdb": "Azure Cosmos DB",
              "cache": "Azure Cache for Redis", "queue": "Azure Service Bus", "stream": "Azure Event Hubs", "storage": "Azure Blob Storage",
              "fn": "Azure Functions", "container": "Azure Container Apps", "warehouse": "Azure Synapse", "llm": "Azure OpenAI",
              "vector": "Azure AI Search", "cdn": "Azure Front Door"},
    "gcp": {"gateway": "Google API Gateway", "auth": "Identity Platform", "db": "Cloud SQL PostgreSQL", "docdb": "Firestore",
            "cache": "Memorystore Redis", "queue": "Cloud Pub/Sub", "stream": "Cloud Pub/Sub", "storage": "Cloud Storage",
            "fn": "Cloud Run functions", "container": "Cloud Run", "warehouse": "BigQuery", "llm": "Vertex AI Gemini",
            "vector": "AlloyDB pgvector", "cdn": "Cloud CDN"},
    "agnostic": {"gateway": "Kong", "auth": "Keycloak", "db": "PostgreSQL", "docdb": "MongoDB",
                 "cache": "Redis", "queue": "RabbitMQ", "stream": "Apache Kafka", "storage": "MinIO (S3 API)",
                 "fn": "Knative", "container": "Kubernetes Deployment", "warehouse": "ClickHouse", "llm": "LLM via LiteLLM (Gemini/Claude)",
                 "vector": "pgvector", "cdn": "Cloudflare"},
}

STYLES = {
    "monolith": ("Modular monolith", "One deployable app with clear internal modules and a single database.",
                 ["Fastest to build and operate", "Simple transactions and debugging", "Cheapest at small scale"],
                 ["Scales as one unit", "Module boundaries need discipline"], "Small teams, early products", "low", "low"),
    "microservices": ("Microservices", "Independently deployable services behind an API gateway, each owning its data.",
                      ["Independent scaling and deploys", "Team autonomy", "Fault isolation"],
                      ["Operational overhead", "Distributed transactions and tracing", "Higher baseline cost"],
                      "Multiple teams, high scale", "high", "high"),
    "serverless": ("Serverless", "Managed functions and services that scale to zero; you pay per request.",
                   ["Lowest idle cost", "No servers to manage", "Automatic scaling"],
                   ["Cold starts", "Vendor coupling", "Harder local testing"], "Spiky or unpredictable traffic", "low", "medium"),
    "event_driven": ("Event-driven", "Services communicate through an event stream; work is asynchronous.",
                     ["Loose coupling", "Absorbs traffic spikes", "Easy to add consumers"],
                     ["Eventual consistency", "Harder to trace flows", "Needs schema governance"],
                     "Integrations, workflows, high throughput", "medium", "high"),
}


def _has(answers, qid, *opt_ids) -> bool:
    return any(o in answers.get(qid, {}).get("option_ids", []) for o in opt_ids)


def _cloud(answers) -> str:
    ids = answers.get("cloud", {}).get("option_ids", [])
    return ids[0] if ids and ids[0] in TECH else "agnostic"


def _rank(answers) -> list[str]:
    scale = (answers.get("scale", {}).get("option_ids") or ["m"])[0]
    prio = (answers.get("priority", {}).get("option_ids") or ["speed"])[0]
    score = {"monolith": 0, "microservices": 0, "serverless": 0, "event_driven": 0}
    score["monolith"] += {"s": 3, "m": 2, "l": 0, "xl": -2}.get(scale, 1)
    score["microservices"] += {"s": -2, "m": 0, "l": 2, "xl": 3}.get(scale, 0)
    score["serverless"] += {"s": 2, "m": 2, "l": 1, "xl": 0}.get(scale, 1)
    score["event_driven"] += {"s": -1, "m": 0, "l": 2, "xl": 3}.get(scale, 0)
    bonus = {"speed": "monolith", "cost": "serverless", "scale": "microservices", "compliance": "monolith"}
    score[bonus.get(prio, "monolith")] += 2
    if _has(answers, "channels", "messaging") or _has(answers, "ai", "agents"):
        score["event_driven"] += 1
    return sorted(score, key=lambda k: -score[k])


def _components(style: str, answers: dict, t: dict[str, str]) -> tuple[list[dict], list[dict]]:
    comps: list[dict] = []
    conns: list[dict] = []

    def add(key, name, ntype, tech, layer, desc=""):
        comps.append({"key": key, "name": name, "type": ntype, "technology": tech, "layer": layer, "description": desc})

    def link(a, b, proto=""):
        conns.append({"from": a, "to": b, "protocol": proto})

    # Layer 0: clients
    clients = []
    if _has(answers, "channels", "web") or not answers.get("channels"):
        add("web", "Web App", "client", "React", 0); clients.append("web")
    if _has(answers, "channels", "mobile"):
        add("mobile", "Mobile App", "client", "React Native", 0); clients.append("mobile")
    if _has(answers, "channels", "messaging"):
        add("messaging", "WhatsApp / Telegram / Slack", "external", "Webhooks", 0); clients.append("messaging")
    if _has(answers, "channels", "api") or _has(answers, "users", "partners"):
        add("partners", "Partner Integrations", "external", "REST clients", 0); clients.append("partners")
    if _has(answers, "channels", "voice"):
        add("voice", "Voice / Meeting Bot", "external", "Meeting bot + STT/TTS", 0); clients.append("voice")

    # Layer 1: edge
    add("gateway", "API Gateway", "gateway", t["gateway"], 1)
    add("auth", "Identity & Auth", "security", t["auth"], 1, "Sign-in, tokens, roles")
    for c in clients:
        link(c, "gateway", "HTTPS")
    link("gateway", "auth", "OIDC")

    # Layer 2: compute
    compute = t["fn"] if style == "serverless" else t["container"]
    if style == "monolith":
        add("app", "Application (modular)", "service", f"FastAPI on {t['container']}", 2, "Modules: users, core domain, billing, notifications")
        services = ["app"]
    else:
        add("users-svc", "User Service", "service", compute, 2)
        add("core-svc", "Core Domain Service", "service", compute, 2)
        add("notify-svc", "Notification Service", "service", compute, 2)
        services = ["users-svc", "core-svc", "notify-svc"]
    for s in services:
        link("gateway", s, "REST")

    ai_needed = _has(answers, "ai", "assistant", "agents", "ml")
    if _has(answers, "ai", "assistant", "agents"):
        add("agent", "AI Agent", "agent", "Python agent + tools", 2, "Orchestrates the LLM, retrieval and tools")
        link("gateway", "agent", "REST/WebSocket")
        add("llm", "LLM", "llm", t["llm"], 3)
        link("agent", "llm", "LLM API")
        if _has(answers, "ai", "agents"):
            add("mcp", "MCP Tool Server", "mcp", "MCP", 3, "Exposes business capabilities to agents")
            link("agent", "mcp", "MCP")
            link("mcp", services[-1] if style != "monolith" else "app", "REST")
        if _has(answers, "ai", "assistant"):
            add("vector", "Vector Index", "database", t["vector"], 4, "Embeddings for retrieval")
            link("agent", "vector", "SQL/ANN")
    if _has(answers, "ai", "ml"):
        add("ml", "ML Model Serving", "service", "Model endpoint", 3)
        link(services[-1] if style != "monolith" else "app", "ml", "REST")

    # Messaging layer
    if style == "event_driven":
        add("bus", "Event Stream", "queue", t["stream"], 3, "Domain events between services")
        for s in services[:-1]:
            link(s, "bus", "publish events")
        link("bus", services[-1], "consume")  # the notification service reacts to domain events
    elif style in ("microservices", "serverless") or _has(answers, "channels", "messaging"):
        add("queue", "Job Queue", "queue", t["queue"], 3, "Async jobs and retries")
        link(services[0], "queue", "enqueue")
        link("queue", services[-1], "consume")

    # Layer 4: data
    if style == "monolith":
        add("db", "Primary Database", "database", t["db"], 4)
        link("app", "db", "SQL")
    else:
        add("users-db", "Users DB", "database", t["db"], 4)
        add("core-db", "Core DB", "database", t["docdb"] if _has(answers, "data", "documents") else t["db"], 4)
        link("users-svc", "users-db", "SQL")
        link("core-svc", "core-db", "SQL")
    scale = (answers.get("scale", {}).get("option_ids") or ["m"])[0]
    if scale != "s" or style == "microservices":  # anything beyond a pilot needs a cache to absorb peaks
        add("cache", "Cache", "cache", t["cache"], 3)
        link(services[0] if style == "monolith" else "core-svc", "cache", "RESP")
    if _has(answers, "data", "files"):
        add("files", "Object Storage", "storage", t["storage"], 4)
        link(services[-1] if style == "monolith" else "core-svc", "files", "S3 API")
    if _has(answers, "data", "analytics"):
        add("warehouse", "Analytics Warehouse", "database", t["warehouse"], 4)
        link("bus" if style == "event_driven" else (services[0] if style == "monolith" else "core-svc"), "warehouse", "ETL")
    if ai_needed and _has(answers, "priority", "compliance"):
        add("guard", "AI Guardrails", "security", "Prompt-injection & PII filters", 2)
        link("gateway", "guard", "inline")
    return comps, conns


def _requirements_from_answers(idea: str, answers: dict) -> list[dict[str, Any]]:
    reqs: list[dict[str, Any]] = []
    users = answers.get("users", {}).get("labels") or ["users"]
    if idea:
        reqs.append({"title": idea[:80], "description": idea, "kind": "functional", "priority": "high", "actors": users,
                     "acceptance_criteria": []})
    ch = answers.get("channels", {}).get("labels")
    if ch:
        reqs.append({"title": f"Access via {', '.join(ch)}", "description": f"Users can use the system through: {', '.join(ch)}.",
                     "kind": "functional", "priority": "high", "actors": users,
                     "acceptance_criteria": [f"A user can complete the primary flow via {c}" for c in ch]})
    sc = answers.get("scale", {}).get("labels")
    if sc:
        reqs.append({"title": f"Support {sc[0]}", "description": f"The system must support {sc[0]} in the first year.",
                     "kind": "non_functional", "category": "scalability", "priority": "high",
                     "acceptance_criteria": ["Load test at 2x expected peak without errors", "p95 API latency < 500 ms at peak"]})
    if answers.get("users", {}).get("option_ids"):
        reqs.append({"title": "Authenticated access with roles", "description": "Users must sign in; actions are authorized by role.",
                     "kind": "non_functional", "category": "security", "priority": "high",
                     "acceptance_criteria": ["Unauthenticated requests are rejected", "Role checks enforced on every API"]})
    ai = [l for l in answers.get("ai", {}).get("labels", []) if l != "None"]
    if ai:
        reqs.append({"title": f"AI: {', '.join(ai)}", "description": f"Provide {', '.join(ai).lower()}.", "kind": "functional",
                     "priority": "medium", "actors": users,
                     "acceptance_criteria": ["Answers cite their sources", "Agent tool calls are permission-checked and logged"]})
    if _has(answers, "priority", "compliance"):
        reqs.append({"title": "Personal data protection", "kind": "non_functional", "category": "privacy", "priority": "high",
                     "description": "Personal data is encrypted at rest and in transit, access is audit-logged and data "
                                    "subjects can have their data deleted.",
                     "acceptance_criteria": ["All PII is encrypted with managed keys", "Deletion requests are completed within 30 days"]})
    pr = answers.get("priority", {}).get("labels")
    if pr:
        reqs.append({"title": f"Priority: {pr[0]}", "description": f"Architecture decisions optimize for {pr[0].lower()}.",
                     "kind": "non_functional", "category": "cost" if "cost" in pr[0].lower() else "maintainability",
                     "priority": "medium"})
    return reqs


def _context(idea: str, answers: dict) -> str:
    """Text used to detect the domain: the idea plus the chosen answers (not the options that were not chosen)."""
    return " ".join([idea] + [" ".join(a.get("labels") or []) for a in answers.values()])


def compose_architectures(idea: str, answers: dict, styles: list[str] | None = None) -> dict[str, Any]:
    """Offline composer: up to three styles ranked by the answers, each completed for the detected domain
    (checklist capabilities) and checked/repaired by the deterministic review."""
    cloud = _cloud(answers)
    t = TECH[cloud]
    ranked = styles or _rank(answers)[:3]
    context = _context(idea, answers)
    requirements = _requirements_from_answers(idea, answers)
    requirements += option_review.requirement_additions(context, requirements)
    options = []
    for i, style in enumerate(ranked):
        name, summary, pros, cons, best_for, cost, complexity = STYLES[style]
        comps, conns = _components(style, answers, t)
        option = {"id": style, "name": name, "style": style, "summary": summary, "pros": pros, "cons": cons,
                  "best_for": best_for, "cost": cost, "complexity": complexity, "recommended": i == 0,
                  "components": comps, "connections": conns}
        option_review.review_option(option, requirements, context, cloud=cloud)
        options.append(option)
    return {"type": "architectures", "id": "architectures", "title": f"Architecture options ({cloud.upper() if cloud != 'agnostic' else 'cloud-agnostic'})",
            "options": options, "requirements": requirements}


# --------------------------------------------------------------------------- LLM interviewer
CHAT_PROMPT = "design.turn"  # prompts/design.turn.yaml (versioned; see agents.prompts)
REPAIR_PROMPT = "design.repair"  # prompts/design.repair.yaml: fix one option's problems found by option_review


def _arch_schema() -> dict[str, Any]:
    comp = {"type": "object", "properties": {
        "key": {"type": "string"}, "name": {"type": "string"}, "type": {"type": "string", "enum": list(NODE_TYPES)},
        "technology": {"type": "string"}, "description": {"type": "string"}, "layer": {"type": "integer"}},
        "required": ["key", "name", "type", "technology", "layer"]}
    conn = {"type": "object", "properties": {"from": {"type": "string"}, "to": {"type": "string"}, "protocol": {"type": "string"}},
            "required": ["from", "to"]}
    arch = {"type": "object", "properties": {
        "id": {"type": "string"}, "name": {"type": "string"}, "summary": {"type": "string"},
        "pros": {"type": "array", "items": {"type": "string"}}, "cons": {"type": "array", "items": {"type": "string"}},
        "best_for": {"type": "string"}, "cost": {"type": "string", "enum": ["low", "medium", "high"]},
        "complexity": {"type": "string", "enum": ["low", "medium", "high"]},
        "components": {"type": "array", "items": comp}, "connections": {"type": "array", "items": conn}},
        "required": ["id", "name", "summary", "pros", "cons", "cost", "complexity", "components", "connections"]}
    return arch


def _req_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {
        "title": {"type": "string"}, "description": {"type": "string"},
        "kind": {"type": "string", "enum": ["functional", "non_functional"]}, "category": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "actors": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "kind"]}


def _repair_schema() -> dict[str, Any]:
    return {"type": "object", "properties": {
        "option": _arch_schema(), "fixes": {"type": "array", "items": {"type": "string"}},
        "additional_architectures": {"type": "array", "items": _arch_schema()},
        "requirements": {"type": "array", "items": _req_schema()}},
        "required": ["option", "fixes"]}


def _schema() -> dict[str, Any]:
    opt = {"type": "object", "properties": {
        "id": {"type": "string"}, "label": {"type": "string"}, "description": {"type": "string"},
        "recommended": {"type": "boolean"}}, "required": ["id", "label", "description", "recommended"]}
    arch, req = _arch_schema(), _req_schema()
    return {"type": "object", "properties": {
        "message": {"type": "string"},
        "action": {"type": "string", "enum": ["ask", "propose"]},
        "question": {"type": "object", "properties": {
            "id": {"type": "string"}, "question": {"type": "string"}, "why": {"type": "string"},
            "multi": {"type": "boolean"}, "options": {"type": "array", "items": opt}},
            "required": ["id", "question", "multi", "options"]},
        "architectures": {"type": "array", "items": arch},
        "requirements": {"type": "array", "items": req}},
        "required": ["message", "action"]}


def _project_summary(state: ProjectState) -> str:
    reqs = active_requirements(state)
    if not state.nodes and not reqs:
        return "The project is empty."
    return json.dumps({
        "components": [f"{n.name} ({n.type}, {n.technology})" for n in state.nodes.values()],
        "requirements": [f"{r.id} {r.title}" for r in reqs.values()]})


def llm_turn(turns: list[dict[str, Any]], state: ProjectState, org_id: str | None = None,
             memory: str = "") -> dict[str, Any]:
    from . import agents  # imported lazily so the offline path never depends on provider configuration
    from .cloud_docs import DocsRequest

    asked = sum(1 for t in turns if t.get("role") == "assistant" and (t.get("card") or {}).get("type") == "question")
    project, transcript = _project_summary(state), transcript_text(turns)
    context = _context(_idea(turns), _answers(turns))
    # The idea stays the same for the whole interview, so later turns reuse the cached lookup.
    docs = DocsRequest(query=f"reference architecture {_idea(turns)[:200]}", about=f"{transcript}\n{project}")
    data = agents.run_json(CHAT_PROMPT, _schema(), org_id=org_id, docs=docs, max_tokens=PROPOSAL_MAX_TOKENS, variables={
        "max_q": MAX_QUESTIONS, "types": ", ".join(NODE_TYPES), "project": project,
        "memory": memory, "transcript": transcript, "asked": asked, "checklist": _checklist_hint(context).strip(),
        "must_propose": asked >= MAX_QUESTIONS})

    if data.get("action") == "ask" and data.get("question") and asked < MAX_QUESTIONS:
        q = data["question"]
        options = _clean_options(q.get("options") or [])
        if len(options) >= 2:
            return {"message": data.get("message", ""), "card": {
                "type": "question", "id": slugify(q.get("id") or q["question"]) + f"-{asked + 1}",
                "question": q["question"], "why": q.get("why", ""), "multi": bool(q.get("multi")),
                "options": options, "step": asked + 1, "total": None}}
    archs = [_clean_arch(a, i) for i, a in enumerate(data.get("architectures") or [])]
    archs = [a for a in archs if a["components"]][:MAX_OPTIONS]
    if not archs:  # model did not produce usable options — fall back to the composer
        return {"message": data.get("message") or "Here are architecture options.",
                "card": compose_architectures(_idea(turns), _answers(turns))}
    card = {"type": "architectures", "id": "architectures", "title": "Architecture options",
            "options": archs, "requirements": _clean_requirements(data.get("requirements") or [])}
    return {"message": data.get("message") or "Here are architecture options.",
            "card": review_and_repair(card, context, org_id, idea=_idea(turns), answers=_answers(turns))}


# --------------------------------------------------------------------------- review-and-repair loop (LLM path)
MAX_OPTIONS = 4
PREFERRED_OPTIONS = 3
PROPOSAL_MAX_TOKENS = 16000
REPAIR_MAX_TOKENS = 16000


def _checklist_hint(context: str) -> str:
    domains = checklists.detect_domains(context)
    if not domains:
        return ""
    caps = checklists.expected_capabilities(domains)
    labels = ", ".join(checklists.CAPABILITIES[c]["label"] for c, sev in caps.items() if sev != "info")
    areas = ", ".join(checklists.AREAS[a]["title"].lower() for d in domains for a in checklists.DOMAINS[d]["areas"])
    names = ", ".join(checklists.DOMAINS[d]["label"] for d in domains)
    return (f"Domain checklist ({names}): when proposing, every option needs {labels}; the requirements should "
            f"cover {areas}.\n\n")


def _clean_requirements(reqs: list[dict]) -> list[dict]:
    out, seen = [], set()
    for r in reqs:
        title = (r.get("title") or "").strip()
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        out.append({**r, "title": title, "kind": r.get("kind") if r.get("kind") in ("functional", "non_functional") else "functional",
                    "acceptance_criteria": [str(c) for c in r.get("acceptance_criteria") or []],
                    "actors": [str(a) for a in r.get("actors") or []]})
    return out


def _for_prompt(option: dict) -> dict:
    keep = ("id", "name", "summary", "pros", "cons", "best_for", "cost", "complexity", "components", "connections")
    return {k: option[k] for k in keep if k in option}


def _repair_call(option: dict, issues: list[dict], context: str, requirements: list[dict], others: list[str],
                 extra_options: int, areas: list[str], org_id: str | None) -> dict:
    from . import agents

    problems = "\n".join(f"- [{i['severity']}] {i['message']} Fix: {i['suggestion']}"
                         for i in issues if i["severity"] in ("error", "warning") or i["code"] in
                         ("completeness", "erasure_event_log", "single_instance_datastore")) or "- (none found)"
    asks = []
    if extra_options:
        asks.append(f"Also return {extra_options} additional_architectures: genuinely different styles from these "
                    f"existing options: {', '.join(others)}. Make them complete and correctly wired from the start.")
    if areas:
        listed = "; ".join(f"{checklists.AREAS[a]['title']} ({checklists.AREAS[a]['description']})" for a in areas)
        asks.append(f"Also return requirements (functional, measurable acceptance criteria with numbers) for these "
                    f"functional areas that no requirement covers yet: {listed}.")
    user = (f"Product idea and answers: {context}\n\n"
            f"Requirements: {json.dumps([r.get('title') for r in requirements])}\n\n"
            f"{_checklist_hint(context)}"
            f"Option to repair:\n{json.dumps(_for_prompt(option))}\n\n"
            f"Problems found by ArchFlow's checks:\n{problems}\n\n" + "\n".join(asks))
    return agents.run_json(REPAIR_PROMPT, _repair_schema(), variables={"request": user}, org_id=org_id,
                           max_tokens=REPAIR_MAX_TOKENS, question=f"Repair {option.get('name', 'option')}")


def _style_of(option: dict) -> str:
    text = f"{option.get('id', '')} {option.get('name', '')} {option.get('style', '')}".lower()
    for style, pat in (("monolith", r"monolith"), ("serverless", r"serverless|function"), ("event_driven", r"event"),
                       ("microservices", r"microservice")):
        if re.search(pat, text):
            return style
    return ""


def _distinct(candidate: dict, options: list[dict]) -> bool:
    names = {c["name"].lower() for c in candidate["components"]}
    for o in options:
        if o["name"].strip().lower() == candidate["name"].strip().lower():
            return False
        other = {c["name"].lower() for c in o["components"]}
        if names and len(names & other) / len(names | other) > 0.85:
            return False
    return True


def review_and_repair(card: dict, context: str, org_id: str | None = None, *, idea: str = "",
                      answers: dict | None = None) -> dict:
    """Check every proposed option with the deterministic flow and checklist checks; ask the LLM once per option
    (concurrently) to repair the ones with errors/warnings, keep whichever version is better, then let the rules
    fix what is left. The recommended option's call also asks for more alternatives when fewer than three were
    proposed and for requirements covering uncovered functional areas. Records ``option["review"]``."""
    from . import llm

    options: list[dict] = card["options"]
    reqs: list[dict] = card["requirements"]
    req_text = context + " " + " ".join(f"{r.get('title', '')} {r.get('description', '')}" for r in reqs)
    areas = checklists.missing_areas(req_text, reqs)
    check_reqs = reqs + option_review.requirement_additions(context, reqs)  # what the card will have at the end, roughly
    notes = {i: option_review.normalize_option(o) for i, o in enumerate(options)}
    before = {i: option_review.check_option(o, check_reqs, context) for i, o in enumerate(options)}
    want_more = max(0, PREFERRED_OPTIONS - len(options))
    need = [i for i in range(len(options)) if option_review.blocking(before[i])]
    if (want_more or areas) and 0 not in need:
        need.insert(0, 0)

    results: dict[int, dict] = {}
    if need:
        def call(i: int) -> dict:
            return _repair_call(options[i], before[i], context, reqs, [o["name"] for o in options],
                                want_more if i == 0 else 0, areas if i == 0 else [], org_id)

        with ThreadPoolExecutor(max_workers=min(4, len(need))) as pool:
            futures = {pool.submit(call, i): i for i in need}
            for fut in as_completed(futures):
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except (llm.LLMUnavailable, llm.LLMRefused, ValueError, KeyError, TypeError) as exc:
                    log.warning("architecture repair for option %d failed (%s); using rule-based repair", i, exc)

    for i, option in enumerate(options):
        res, prior, engine = results.get(i), list(notes[i]), "rules"
        if res and isinstance(res.get("option"), dict):
            try:
                cand = _clean_arch(res["option"], i)
            except (ValueError, TypeError):
                cand = None
            if cand and cand["components"]:
                cand.update(id=option["id"], recommended=option.get("recommended", False))
                cand_notes = option_review.normalize_option(cand)
                after = option_review.check_option(cand, check_reqs, context)
                better = (option_review.weight(option_review.blocking(after))
                          < option_review.weight(option_review.blocking(before[i])))
                kept_enough = len(cand["components"]) >= 0.6 * len(option["components"])
                if better and kept_enough:
                    options[i] = option = cand
                    prior = [str(f) for f in res.get("fixes") or []] + cand_notes
                    engine = "llm+rules"
                elif option_review.blocking(before[i]):
                    prior.append("AI repair was not better than the original; kept the original")
        option_review.review_option(option, check_reqs, context, before=before[i], prior_fixes=prior, engine=engine)

    # More alternatives: from the recommended option's repair call, else the offline composer's next-best styles.
    taken_ids = {o["id"] for o in options}
    for k, raw in enumerate((results.get(0) or {}).get("additional_architectures") or []):
        if len(options) >= MAX_OPTIONS or not isinstance(raw, dict):
            break
        try:
            cand = _clean_arch(raw, len(options))
        except (ValueError, TypeError):
            continue
        if not cand["components"] or not _distinct(cand, options):
            continue
        cand["id"] = unique_slug(cand["id"], taken_ids)
        taken_ids.add(cand["id"])
        cand["recommended"] = False
        option_review.review_option(cand, check_reqs, context, prior_fixes=["Alternative added by the architecture review"],
                             engine="llm+rules")
        options.append(cand)
    if len(options) < PREFERRED_OPTIONS:
        _add_composer_options(options, idea, answers or {}, reqs)

    # Requirements breadth: the model's additions first, then checklist stubs for what is still uncovered.
    seen = {r["title"].lower() for r in reqs}
    for r in _clean_requirements((results.get(0) or {}).get("requirements") or []):
        if r["title"].lower() not in seen:
            seen.add(r["title"].lower())
            reqs.append({**r, "source": "review"})
    reqs += option_review.requirement_additions(context, reqs)
    for i, o in enumerate(options):
        o["recommended"] = i == 0
    return card


def _add_composer_options(options: list[dict], idea: str, answers: dict, reqs: list[dict]) -> None:
    """Fill up to the preferred number of options with the offline composer's best styles not yet represented,
    on the same cloud as the proposed options."""
    from .analysis import _large_scale

    styles_present = {_style_of(o) for o in options}
    cloud = option_review.infer_cloud(options[0]) if options else _cloud(answers)
    text = " ".join(f"{r.get('title', '')} {r.get('description', '')} {' '.join(r.get('acceptance_criteria') or [])}" for r in reqs)
    fallback = {**answers, "cloud": {"option_ids": [cloud], "labels": [cloud]},
                "scale": {"option_ids": ["l" if _large_scale(text) else "m"], "labels": []}}
    styles = [s for s in _rank(fallback) if s not in styles_present][:PREFERRED_OPTIONS - len(options)]
    if not styles:
        return
    composed = compose_architectures(idea, fallback, styles=styles)
    taken_ids = {o["id"] for o in options}
    for o in composed["options"]:
        o["id"] = unique_slug(o["id"], taken_ids)
        taken_ids.add(o["id"])
        o["review"]["fixes"].insert(0, "Alternative added by ArchFlow's built-in composer")
        o["review"]["engine"] = "rules"
        options.append(o)


def _clean_options(options: list[dict]) -> list[dict]:
    out, seen = [], set()
    for o in options[:5]:
        oid = slugify(o.get("id") or o.get("label", ""))
        if not o.get("label") or oid in seen:
            continue
        seen.add(oid)
        out.append({"id": oid, "label": o["label"], "description": o.get("description", ""),
                    "recommended": bool(o.get("recommended"))})
    if out and not any(o["recommended"] for o in out):
        out[0]["recommended"] = True
    return out


def _clean_arch(a: dict, i: int) -> dict:
    keys = set()
    comps = []
    for c in a.get("components") or []:
        key = slugify(c.get("key") or c.get("name", ""))
        if not c.get("name") or key in keys:
            continue
        keys.add(key)
        comps.append({"key": key, "name": c["name"], "type": c.get("type") if c.get("type") in NODE_TYPES else "service",
                      "technology": c.get("technology", ""), "description": c.get("description", ""),
                      "layer": max(0, min(5, int(c.get("layer", 2) or 2)))})
    conns = [{"from": slugify(c["from"]), "to": slugify(c["to"]), "protocol": c.get("protocol", "")}
             for c in a.get("connections") or [] if slugify(c.get("from", "")) in keys and slugify(c.get("to", "")) in keys]
    return {"id": slugify(a.get("id") or a.get("name", f"option-{i}")) or f"option-{i}", "name": a.get("name", f"Option {i + 1}"),
            "summary": a.get("summary", ""), "pros": a.get("pros") or [], "cons": a.get("cons") or [],
            "best_for": a.get("best_for", ""), "cost": a.get("cost", "medium"), "complexity": a.get("complexity", "medium"),
            "recommended": i == 0, "components": comps, "connections": conns}


def next_turn(turns: list[dict[str, Any]], state: ProjectState, org_id: str | None = None,
              memory: str = "") -> dict[str, Any]:
    """Return {"message", "card", "engine"} for the next assistant turn. ``memory`` is the project memory block
    (conversations.memory_block); only the LLM interviewer uses it."""
    try:
        from . import llm
        if llm.available(org_id):
            try:
                return {**llm_turn(turns, state, org_id, memory), "engine": "llm"}
            except (llm.LLMUnavailable, llm.LLMRefused, ValueError, KeyError, TypeError) as exc:
                log.warning("LLM interviewer failed (%s); using offline interviewer", exc)
    except ImportError:
        pass
    return {**offline_turn(turns, state), "engine": "offline"}


# --------------------------------------------------------------------------- applying a chosen architecture
def architecture_ops(state: ProjectState, option: dict[str, Any], requirements: list[dict[str, Any]],
                     replace: bool = True, ids_out: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Operations that put a chosen architecture (and its requirements) onto the canvas.

    ``ids_out``, if given, is filled with component key → node id."""
    ops: list[dict[str, Any]] = []
    if replace:
        # Removing a top-level component also removes its internal (LLD) elements.
        ops += [{"op": "remove_node", "id": nid} for nid, n in state.nodes.items()
                if not n.parent_id or n.parent_id not in state.nodes]

    # Requirements: skip titles that already exist; pre-assign IDs so components can link to them.
    existing = {r.title.strip().lower(): r.id for r in active_requirements(state).values()}
    issued = 0  # ids pre-assigned below; past purged ids too (next_requirement_id honours requirement_seq)
    linked: list[tuple[str, dict]] = []  # (requirement id, requirement) for traceability
    for r in requirements:
        title = (r.get("title") or "").strip()
        if not title:
            continue
        if title.lower() in existing:
            linked.append((existing[title.lower()], r))
            continue
        rid = next_requirement_id(state, issued)
        issued = requirement_number(rid)
        existing[title.lower()] = rid
        linked.append((rid, r))
        ops.append({"op": "add_requirement", "requirement": {
            "id": rid, "title": title[:120], "description": r.get("description", ""),
            "kind": r.get("kind") if r.get("kind") in ("functional", "non_functional") else "functional",
            "category": r.get("category", ""), "priority": r.get("priority") if r.get("priority") in ("low", "medium", "high", "critical") else "medium",
            "actors": r.get("actors") or [], "acceptance_criteria": r.get("acceptance_criteria") or [], "status": "draft"}})

    comps = option.get("components") or []
    taken = set() if replace else set(state.nodes)
    ids: dict[str, str] = {}
    by_layer: dict[int, list[dict]] = {}
    for c in comps:
        by_layer.setdefault(int(c.get("layer", 2)), []).append(c)
    for layer, items in sorted(by_layer.items()):
        width = (len(items) - 1) * COL_X
        for i, c in enumerate(items):
            nid = slugify(c["key"])
            while nid in taken:
                nid = f"{nid}-2"
            taken.add(nid)
            ids[c["key"]] = nid
            ops.append({"op": "add_node", "node": {
                "id": nid, "name": c["name"], "type": c.get("type") if c.get("type") in NODE_TYPES else "service",
                "technology": c.get("technology", ""), "description": c.get("description", ""),
                "position": {"x": i * COL_X - width / 2, "y": layer * LAYER_Y},
                "requirement_ids": _link_requirements(c, linked)}})
    if ids_out is not None:
        ids_out.update(ids)
    seen = set()
    for e in option.get("connections") or []:
        a, b = ids.get(e.get("from")), ids.get(e.get("to"))
        if a and b and (a, b) not in seen and a != b:
            seen.add((a, b))
            ops.append({"op": "add_edge", "edge": {"source": a, "target": b, "protocol": e.get("protocol", "")}})
    return ops


def _link_requirements(comp: dict, linked: list[tuple[str, dict]]) -> list[str]:
    """Heuristic traceability: link a component to requirements whose text mentions its role."""
    text = f"{comp.get('name', '')} {comp.get('type', '')} {comp.get('technology', '')}".lower()
    words = {w for w in re.findall(r"[a-z]{4,}", text)}
    role_words = {"client": {"access", "channel", "web", "mobile"}, "security": {"auth", "authenticated", "roles", "security"},
                  "gateway": {"access", "channel"}, "agent": {"ai", "assistant", "agent"}, "llm": {"ai", "assistant"},
                  "database": {"data", "records"}, "cache": {"latency", "scale", "support"}, "queue": {"scale", "support"}}
    words |= role_words.get(comp.get("type", ""), set())
    out = []
    for rid, r in linked:
        rtext = f"{r.get('title', '')} {r.get('description', '')} {r.get('category', '')}".lower()
        rwords = set(re.findall(r"[a-z]{2,}", rtext))
        if words & rwords:
            out.append(rid)
    return out[:4]
