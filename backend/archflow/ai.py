"""AI layer: natural-language commands → structured operations, and questions about the project.

The model edits the canvas through the same tools an MCP agent would use (add_component,
connect_components, ...). Tool calls are collected and applied as one atomic batch through
``ops.apply_ops`` — the AI never produces a picture, only operations on the shared model.

The command is a Pydantic AI agent (``agents``) whose prompt is versioned in ``prompts/canvas.command.yaml``;
its model calls go through the provider-agnostic gateway in ``llm.py`` (LiteLLM Router: Gemini primary,
Claude and OpenAI as fallbacks). If no provider key is configured (or ``ARCHFLOW_AI=offline``),
a small rule-based parser handles common canvas commands so the product still works offline.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from . import agents, cloud_docs, llm
from .models import NODE_TYPES, ProjectState, without_deleted
from .ops import resolve_node

log = logging.getLogger("archflow.ai")

MODE = os.environ.get("ARCHFLOW_AI", "auto")  # auto | claude (or llm: never fall back) | offline
# Reported by /api/health: the primary model of the "reasoning" group (see llm.py for the fallback chain).
MODEL = llm.primary_model("reasoning")
MAX_TOKENS = int(os.environ.get("ARCHFLOW_MAX_TOKENS", "16000"))


@dataclass
class CommandResult:
    ops: list[dict[str, Any]] = field(default_factory=list)
    explanation: str = ""
    questions: list[str] = field(default_factory=list)
    question_options: dict[str, list[str]] = field(default_factory=dict)  # question -> answers the user can tap
    engine: str = "offline"


# --------------------------------------------------------------------------- tools
_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}

_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "name": "add_component",
        "description": "Add an architecture component to the canvas. Refer to it later in the same response by its name.",
        "parameters": {"type": "object", "properties": {
            "name": {**_STR, "description": "Display name, e.g. 'Order Service'."},
            "type": {"type": "string", "enum": list(NODE_TYPES)},
            "technology": {**_STR, "description": "e.g. 'FastAPI', 'PostgreSQL', 'Redis'."},
            "description": _STR,
            "x": {"type": "number"}, "y": {"type": "number"},
            "requirement_ids": {**_STRS, "description": "Requirement IDs this component helps satisfy."},
        }, "required": ["name", "type"]},
    },
    {
        "name": "update_component",
        "description": "Change properties of an existing component (rename, change technology, move, relink requirements).",
        "parameters": {"type": "object", "properties": {
            "component": {**_STR, "description": "Existing component id or name."},
            "name": _STR, "type": {"type": "string", "enum": list(NODE_TYPES)}, "technology": _STR,
            "description": _STR, "x": {"type": "number"}, "y": {"type": "number"}, "requirement_ids": _STRS,
        }, "required": ["component"]},
    },
    {
        "name": "remove_component",
        "description": "Remove a component and all of its connections.",
        "parameters": {"type": "object", "properties": {"component": _STR}, "required": ["component"]},
    },
    {
        "name": "connect_components",
        "description": "Create a directed connection (data/call flow) from source to target.",
        "parameters": {"type": "object", "properties": {
            "source": _STR, "target": _STR,
            "protocol": {**_STR, "description": "e.g. REST, gRPC, SQL, Kafka, MCP, A2A."}, "label": _STR,
        }, "required": ["source", "target"]},
    },
    {
        "name": "disconnect_components",
        "description": "Remove the connection from source to target.",
        "parameters": {"type": "object", "properties": {"source": _STR, "target": _STR}, "required": ["source", "target"]},
    },
    {
        "name": "add_requirement",
        "description": "Add a structured requirement. Use measurable wording for non-functional requirements.",
        "parameters": {"type": "object", "properties": {
            "title": _STR, "description": _STR,
            "kind": {"type": "string", "enum": ["functional", "non_functional"]},
            "category": {**_STR, "description": "For NFRs: performance, scalability, availability, security, privacy, observability, cost, ..."},
            "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "actors": _STRS, "acceptance_criteria": _STRS,
        }, "required": ["title"]},
    },
    {
        "name": "update_requirement",
        "description": "Change fields of an existing requirement.",
        "parameters": {"type": "object", "properties": {
            "requirement": {**_STR, "description": "Requirement id, e.g. REQ-004."},
            "title": _STR, "description": _STR,
            "kind": {"type": "string", "enum": ["functional", "non_functional"]}, "category": _STR,
            "priority": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
            "status": {"type": "string", "enum": ["draft", "review", "approved", "implemented"]},
            "actors": _STRS, "acceptance_criteria": _STRS,
        }, "required": ["requirement"]},
    },
    {
        "name": "remove_requirement",
        "description": "Delete a requirement.",
        "parameters": {"type": "object", "properties": {"requirement": _STR}, "required": ["requirement"]},
    },
    {
        "name": "ask_clarifying_questions",
        "description": "Ask the user questions whose answers are needed to complete the requirements or architecture. "
                       "Give each question 2-4 short answer options, the most likely first; the user taps one.",
        "parameters": {"type": "object", "properties": {"questions": {"type": "array", "items": {
            "type": "object", "properties": {"question": _STR, "options": _STRS}, "required": ["question"]}}},
            "required": ["questions"]},
    },
]

# The system prompts live in archflow/prompts/ (canvas.command, assistant.ask), versioned; see agents.prompts.
COMMAND_PROMPT = "canvas.command"  # v2: clarifying questions with answer options
ASK_PROMPT = "assistant.ask"


def _tool_to_op(name: str, args: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    """Map a model tool call onto an ops.py operation. Returns (op, questions)."""
    pos = {"x": args["x"], "y": args["y"]} if "x" in args and "y" in args else None
    if name == "add_component":
        node = {k: args[k] for k in ("name", "type", "technology", "description", "requirement_ids") if k in args}
        if pos:
            node["position"] = pos
        return {"op": "add_node", "node": node}, []
    if name == "update_component":
        changes = {k: args[k] for k in ("name", "type", "technology", "description", "requirement_ids") if k in args}
        if pos:
            changes["position"] = pos
        return {"op": "update_node", "id": args["component"], "changes": changes}, []
    if name == "remove_component":
        return {"op": "remove_node", "id": args["component"]}, []
    if name == "connect_components":
        return {"op": "add_edge", "edge": {k: args[k] for k in ("source", "target", "protocol", "label") if k in args}}, []
    if name == "disconnect_components":
        return {"op": "remove_edge", "source": args["source"], "target": args["target"]}, []
    if name == "add_requirement":
        return {"op": "add_requirement", "requirement": dict(args)}, []
    if name == "update_requirement":
        changes = {k: v for k, v in args.items() if k != "requirement"}
        return {"op": "update_requirement", "id": args["requirement"], "changes": changes}, []
    if name == "remove_requirement":
        return {"op": "remove_requirement", "id": args["requirement"]}, []
    if name == "ask_clarifying_questions":
        return None, [q for q in (_question(x) for x in args.get("questions") or []) if q]
    return None, []


def _question(x: Any) -> dict[str, Any] | None:
    """A clarifying question with its answer options (models sometimes send a bare string)."""
    if isinstance(x, str):
        return {"question": x.strip(), "options": []} if x.strip() else None
    if isinstance(x, dict) and str(x.get("question") or "").strip():
        opts = [str(o).strip()[:80] for o in x.get("options") or [] if str(o).strip()][:4]
        return {"question": str(x["question"]).strip(), "options": opts}
    return None


def _state_for_prompt(state: ProjectState) -> str:
    state = without_deleted(state)  # the model never sees soft-deleted requirements
    compact = {
        "components": [{"id": n.id, "name": n.name, "type": n.type, "technology": n.technology,
                        "x": round(n.position.x), "y": round(n.position.y), "requirement_ids": n.requirement_ids,
                        **({"description": n.description} if n.description else {}),
                        **({"parent_id": n.parent_id} if n.parent_id else {})} for n in state.nodes.values()],
        "connections": [{"source": e.source, "target": e.target, "protocol": e.protocol, **({"label": e.label} if e.label else {})}
                        for e in state.edges.values()],
        "requirements": [r.model_dump(exclude={"rev", "field_revs", "deleted"}) for r in state.requirements.values()],
    }
    return json.dumps(compact, indent=1)


class AIUnavailable(RuntimeError):
    pass


def _prompt_vars(state: ProjectState, text: str, selection: list[str], context: str = "") -> dict[str, Any]:
    selected = [f"{state.nodes[s].name} ({s})" for s in selection if s in state.nodes]
    return {"state_json": _state_for_prompt(state), "context": context, "selected": ", ".join(selected),
            "request": text}


# Model requests per command: tool rounds plus the closing explanation.
MAX_ROUNDS = 4


@dataclass
class _CommandDeps:
    """What the canvas agent's tools act on: a working copy of the state, and everything applied to it."""
    working: ProjectState
    ops: list[dict[str, Any]] = field(default_factory=list)
    done: list[str] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)  # {question, options}


def _apply_tool(name: str):
    def handler(deps: _CommandDeps, **args: Any) -> str:
        from .ops import ConflictError, OpError, apply_ops

        op, questions = _tool_to_op(name, args)
        if questions:
            deps.questions += questions
            return "The questions will be shown to the user with your answer."
        if op is None:
            return f"rejected: unknown tool {name}"
        try:
            deps.working, summary = apply_ops(deps.working, [op])
        except (OpError, ConflictError) as exc:
            return f"rejected: {exc}. Nothing was changed; fix the call and try again."
        deps.ops.append(op)
        deps.done += summary
        return "applied: " + ("; ".join(summary) or "no change")
    return handler


def _tools():
    return [agents.tool_from_schema(t["name"], t["description"], t["parameters"], _apply_tool(t["name"]))
            for t in _TOOL_DEFS]


def llm_command(state: ProjectState, text: str, selection: list[str], org_id: str | None = None,
                context: str = "") -> CommandResult:
    """Turn a request into operations with a Pydantic AI agent. Each tool call is applied to a working copy of
    the state and its result (or the reason it was rejected) goes back to the model, which keeps going until it
    answers in text, at most MAX_ROUNDS model requests. The applied operations are returned as one batch.

    ``context`` is the project memory and recent conversation (conversations.command_context), so a follow-up
    such as "now put a cache in front of it" can refer to earlier turns."""
    result = CommandResult(engine="llm")
    deps = _CommandDeps(working=state)
    try:
        run = agents.run_tools(COMMAND_PROMPT, _tools(), deps, variables=_prompt_vars(state, text, selection, context),
                               org_id=org_id, max_tokens=MAX_TOKENS, request_limit=MAX_ROUNDS)
        result.explanation = run.output
    except llm.LLMRefused:
        result.explanation = "The model declined this request."
    except llm.LLMUnavailable as exc:
        if not deps.ops:
            raise AIUnavailable(str(exc)) from exc
    result.ops = deps.ops
    if not result.explanation and deps.done:
        result.explanation = "Changed: " + "; ".join(deps.done) + "."
    result.questions = dedupe_questions([q["question"] for q in deps.questions])
    options = {q["question"]: q["options"] for q in deps.questions if q["options"]}
    result.question_options = {q: options[q] for q in result.questions if q in options}
    return result


def dedupe_questions(questions: list[str]) -> list[str]:
    """Drop repeated questions, including rewordings from a later round: at least 35% of their longer words, and
    at least four of them, in common (unrelated questions share well under 10%)."""
    def words(q: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) > 3}

    def same(a: set[str], b: set[str]) -> bool:
        common = a & b
        return bool(a and b) and len(common) >= 4 and len(common) / len(a | b) >= 0.35 or (bool(a) and a == b)

    kept: list[tuple[str, set[str]]] = []
    for q in questions:
        w = words(q)
        if q.strip() and not any(same(w, k) for _, k in kept):
            kept.append((q.strip(), w))
    return [q for q, _ in kept]




def claude_ask(context: str, question: str, org_id: str | None = None, knowledge: str = "") -> str:
    """Answer a question about the project (name kept for compatibility; uses the "reasoning" group). ``knowledge``
    is the matching knowledge-base passages (knowledge.context_block)."""
    try:
        return agents.run_text(ASK_PROMPT, variables={"project_context": "", "memory": "", "summary": "",
                                                      "knowledge": knowledge, "context": context,
                                                      "question": question},
                               org_id=org_id, max_tokens=MAX_TOKENS,
                               docs=cloud_docs.DocsRequest(query=question, about=context))
    except llm.LLMRefused:
        return "The model declined to answer this question."
    except llm.LLMUnavailable as exc:
        raise AIUnavailable(str(exc)) from exc


def run_command(state: ProjectState, text: str, selection: list[str] | None = None,
                org_id: str | None = None, context: str = "") -> CommandResult:
    selection = selection or []
    if MODE != "offline":
        try:
            return llm_command(state, text, selection, org_id, context)
        except AIUnavailable as exc:
            if MODE in ("claude", "llm"):
                raise
            log.info("LLM unavailable (%s); using offline command parser", exc)
    return offline_command(state, text, selection)


# --------------------------------------------------------------------------- offline parser
KEYWORD_TYPES: list[tuple[str, str, str]] = [
    # (regex, node type, default technology)
    (r"redis", "cache", "Redis"), (r"memcache", "cache", "Memcached"), (r"\bcache\b", "cache", "Redis"),
    (r"postgres", "database", "PostgreSQL"), (r"mysql", "database", "MySQL"), (r"dynamo", "database", "DynamoDB"),
    (r"mongo", "database", "MongoDB"), (r"\b(database|db)\b", "database", ""),
    (r"kafka", "queue", "Kafka"), (r"rabbit", "queue", "RabbitMQ"), (r"\bsqs\b", "queue", "SQS"),
    (r"queue|event bus|pub ?sub", "queue", ""),
    (r"\bs3\b|bucket|blob|storage", "storage", ""),
    (r"gateway", "gateway", ""), (r"\bmcp\b", "mcp", "MCP"), (r"agent", "agent", ""),
    (r"\bllm\b|claude|gpt|gemini|model", "llm", ""),
    (r"auth|identity|keycloak|cognito|oauth", "security", ""),
    (r"web|mobile|frontend|\bui\b|client|browser|\bapp\b", "client", ""),
    (r"stripe|twilio|whatsapp|external|third[- ]party|zendesk|jira|github", "external", ""),
]


def infer_type(name: str) -> tuple[str, str]:
    low = name.lower()
    for pattern, ntype, tech in KEYWORD_TYPES:
        if re.search(pattern, low):
            return ntype, tech
    return "service", ""


def _clean(name: str) -> str:
    name = re.sub(r"^(a|an|the|new)\s+", "", name.strip().strip(".\"'"), flags=re.I).strip()
    return name[:1].upper() + name[1:] if name.islower() else name


def _find(state: ProjectState, ref: str):
    ref = _clean(ref)
    node = resolve_node(state, ref)
    if node:
        return node
    low = ref.lower()
    matches = [n for n in state.nodes.values() if low in n.name.lower() or (n.technology and low in n.technology.lower())]
    return matches[0] if len(matches) == 1 else None


def offline_command(state: ProjectState, text: str, selection: list[str]) -> CommandResult:
    res = CommandResult(engine="offline")
    notes: list[str] = []
    for raw in re.split(r"[;\n]+|\.\s+|\bthen\b", text):
        cmd = raw.strip().rstrip(".")
        if not cmd:
            continue
        if not _offline_one(state, cmd, selection, res, notes):
            notes.append(f"Didn't understand “{cmd}”.")
    res.explanation = " ".join(notes)
    if not res.ops and not notes:
        res.explanation = "Nothing to do."
    if any(n.startswith("Didn't") for n in notes):
        res.explanation += (" Offline mode understands: add X [between A and B | connected to A], remove X, "
                            "connect A to B [via P], disconnect A from B, rename A to B, change A to B, "
                            "move A below/above/left of/right of B. Configure GEMINI_API_KEY (or ANTHROPIC_API_KEY / OPENAI_API_KEY) for full AI.")
    return res


def _offline_one(state: ProjectState, cmd: str, selection: list[str], res: CommandResult, notes: list[str]) -> bool:
    m = re.match(r"(?:add|insert|put)\s+(.+?)\s+between\s+(.+?)\s+and\s+(.+)$", cmd, re.I)
    if m:
        new, a, b = _clean(m.group(1)), _find(state, m.group(2)), _find(state, m.group(3))
        if not a or not b:
            notes.append(f"Couldn't find '{m.group(2) if not a else m.group(3)}'.")
            return True
        ntype, tech = infer_type(new)
        pos = {"x": (a.position.x + b.position.x) / 2 + 40, "y": (a.position.y + b.position.y) / 2}
        if any(e.source == a.id and e.target == b.id for e in state.edges.values()):
            res.ops.append({"op": "remove_edge", "source": a.id, "target": b.id})
        res.ops += [
            {"op": "add_node", "node": {"name": new, "type": ntype, "technology": tech, "position": pos,
                                        "parent_id": a.parent_id}},
            {"op": "add_edge", "edge": {"source": a.id, "target": new}},
            {"op": "add_edge", "edge": {"source": new, "target": b.id}},
        ]
        notes.append(f"Inserted {new} between {a.name} and {b.name}.")
        return True

    m = re.match(r"(?:add|create)\s+(.+?)(?:\s+(?:connected to|connecting to|linked to|for|to|behind|in front of)\s+(.+))?$", cmd, re.I)
    if m:
        new = _clean(m.group(1))
        anchor = _find(state, m.group(2)) if m.group(2) else (state.nodes.get(selection[0]) if selection else None)
        ntype, tech = infer_type(new)
        node: dict[str, Any] = {"name": new, "type": ntype, "technology": tech}
        if anchor:
            node["position"] = {"x": anchor.position.x + 320, "y": anchor.position.y + 40}
            node["parent_id"] = anchor.parent_id  # same diagram level as the anchor
        res.ops.append({"op": "add_node", "node": node})
        if anchor:
            res.ops.append({"op": "add_edge", "edge": {"source": anchor.id, "target": new}})
        notes.append(f"Added {new}" + (f" connected to {anchor.name}." if anchor else "."))
        return True

    m = re.match(r"(?:remove|delete|drop)\s+(.+)$", cmd, re.I)
    if m:
        node = _find(state, m.group(1))
        if not node:
            notes.append(f"Couldn't find '{m.group(1)}'.")
            return True
        res.ops.append({"op": "remove_node", "id": node.id})
        notes.append(f"Removed {node.name}.")
        return True

    m = re.match(r"(?:connect|link)\s+(.+?)\s+(?:to|with|and)\s+(.+?)(?:\s+(?:via|using|over)\s+(.+))?$", cmd, re.I)
    if m:
        a, b = _find(state, m.group(1)), _find(state, m.group(2))
        if not a or not b:
            notes.append(f"Couldn't find '{m.group(1) if not a else m.group(2)}'.")
            return True
        res.ops.append({"op": "add_edge", "edge": {"source": a.id, "target": b.id, "protocol": (m.group(3) or "").strip()}})
        notes.append(f"Connected {a.name} → {b.name}.")
        return True

    m = re.match(r"(?:disconnect|unlink)\s+(.+?)\s+(?:from|and)\s+(.+)$", cmd, re.I)
    if m:
        a, b = _find(state, m.group(1)), _find(state, m.group(2))
        if not a or not b:
            notes.append("Couldn't find one of those components.")
            return True
        for s, t in ((a, b), (b, a)):
            if any(e.source == s.id and e.target == t.id for e in state.edges.values()):
                res.ops.append({"op": "remove_edge", "source": s.id, "target": t.id})
        notes.append(f"Disconnected {a.name} and {b.name}.")
        return True

    m = re.match(r"rename\s+(.+?)\s+to\s+(.+)$", cmd, re.I)
    if m:
        node = _find(state, m.group(1))
        if not node:
            notes.append(f"Couldn't find '{m.group(1)}'.")
            return True
        res.ops.append({"op": "update_node", "id": node.id, "changes": {"name": _clean(m.group(2))}})
        notes.append(f"Renamed {node.name}.")
        return True

    m = re.match(r"(?:change|replace|switch|convert)\s+(.+?)\s+(?:to|with|into)\s+(.+)$", cmd, re.I)
    if m:
        node = _find(state, m.group(1))
        if not node:
            notes.append(f"Couldn't find '{m.group(1)}'.")
            return True
        new = _clean(m.group(2))
        ntype, tech = infer_type(new)
        changes: dict[str, Any] = {"technology": tech or new}
        if node.technology and node.technology.lower() in node.name.lower():
            changes["name"] = node.name.replace(node.technology, tech or new)
        if ntype != "service" and ntype != node.type:
            changes["type"] = ntype
        res.ops.append({"op": "update_node", "id": node.id, "changes": changes})
        notes.append(f"Changed {node.name} to {tech or new}.")
        return True

    m = re.match(r"move\s+(.+?)\s+(below|under|above|over|left of|right of|next to)\s+(.+)$", cmd, re.I)
    if m:
        a, b = _find(state, m.group(1)), _find(state, m.group(3))
        if not a or not b:
            notes.append("Couldn't find one of those components.")
            return True
        dx, dy = {"below": (0, 160), "under": (0, 160), "above": (0, -160), "over": (0, -160),
                  "left of": (-320, 0), "right of": (320, 0), "next to": (320, 0)}[m.group(2).lower()]
        res.ops.append({"op": "update_node", "id": a.id,
                        "changes": {"position": {"x": b.position.x + dx, "y": b.position.y + dy}}})
        notes.append(f"Moved {a.name} {m.group(2)} {b.name}.")
        return True
    return False
