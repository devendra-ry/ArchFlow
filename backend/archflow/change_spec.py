"""AI-written implementation specs in the OpenSpec layout (spec-driven development for coding agents).

Vibe coding hands a coding agent (Claude Code, Codex, Cursor, Gemini CLI, Aider, ...) a *change*: a folder that
follows the OpenSpec convention (https://github.com/Fission-AI/openspec), which many agents understand:

    openspec/changes/<change-id>/
        proposal.md                  why, what changes, impact
        specs/<capability>/spec.md   "## ADDED Requirements": SHALL statements + WHEN/THEN scenarios
        design.md                    context, goals, decisions, architecture, data model, API, risks
        tasks.md                     numbered checklist the agent works through

``generate`` asks the LLM to write it from the selected scope and everything ArchFlow knows around it: the traced
requirements, components and their internal (low-level) designs, connections, neighbouring components to stub,
architecture decisions and the project memory. The result is checked: every requirement in scope must be covered
by a spec requirement and a test task, or a spec is derived for it from the model. Without an LLM the whole change
is derived from the model (``engine: "offline"``). Only the file format comes from OpenSpec; no code is used.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from . import agents, llm
from .lld import api_view, children, er_view
from .models import ProjectState, Requirement, without_deleted
from .vibe import TARGETS, Scope, resolve_scope

log = logging.getLogger("archflow.spec")

MAX_TOKENS = 16000
ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,47}$")

PROMPT = "spec.change"  # prompts/spec.change.yaml (versioned; see agents.prompts)

_STRS = {"type": "array", "items": {"type": "string"}}
SCHEMA: dict[str, Any] = {"type": "object", "properties": {
    "change_id": {"type": "string"}, "title": {"type": "string"},
    "proposal": {"type": "object", "properties": {
        "why": {"type": "string"}, "what_changes": _STRS, "impact": _STRS}, "required": ["why", "what_changes"]},
    "specs": {"type": "array", "items": {"type": "object", "properties": {
        "capability": {"type": "string"},
        "requirements": {"type": "array", "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "statement": {"type": "string"}, "sources": _STRS,
            "scenarios": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "when": {"type": "string"}, "then": {"type": "string"}, "and": _STRS},
                "required": ["name", "when", "then"]}}},
            "required": ["name", "statement", "scenarios"]}}},
        "required": ["capability", "requirements"]}},
    "design": {"type": "object", "properties": {
        "context": {"type": "string"}, "goals": _STRS, "non_goals": _STRS,
        "decisions": {"type": "array", "items": {"type": "object", "properties": {
            "decision": {"type": "string"}, "rationale": {"type": "string"}, "alternatives": _STRS},
            "required": ["decision", "rationale"]}},
        "risks": {"type": "array", "items": {"type": "object", "properties": {
            "risk": {"type": "string"}, "mitigation": {"type": "string"}}, "required": ["risk", "mitigation"]}},
        "open_questions": _STRS}, "required": ["context"]},
    "tasks": {"type": "array", "items": {"type": "object", "properties": {
        "section": {"type": "string"}, "items": _STRS}, "required": ["section", "items"]}},
}, "required": ["change_id", "title", "proposal", "specs", "design", "tasks"]}


@dataclass
class ChangeSpec:
    change_id: str
    title: str
    engine: str
    files: dict[str, str] = field(default_factory=dict)  # path under openspec/changes/<id>/ → content
    prompt: str = ""  # agent instructions + every file, for pasting into a chat
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- context
def _component_block(state: ProjectState, nid: str) -> dict[str, Any]:
    n = state.nodes[nid]
    out: dict[str, Any] = {"id": n.id, "name": n.name, "type": n.type, "technology": n.technology,
                           "implements": n.requirement_ids}
    if n.description:
        out["description"] = n.description
    if n.props:
        out["props"] = n.props
    if children(state, nid):
        endpoints = api_view(state, n)
        tables = er_view(state, n).get("tables", [])
        internals = [c.name for c in children(state, nid) if c.type == "component"]
        if internals:
            out["internal_modules"] = internals
        if endpoints:
            out["endpoints"] = [f"{e['method']} {e['path']}" + (f" ({e['description']})" if e.get("description") else "")
                                for e in endpoints]
        if tables:
            out["tables"] = [{"name": t["name"], "columns": [f"{c['name']} {c['type']}".strip() for c in t.get("columns", [])]}
                             for t in tables]
    return out


def build_context(state: ProjectState, project_name: str, scope: Scope, *, memory: str = "",
                  decisions: list[dict[str, Any]] | None = None, target: str = "claude-code", extra: str = "") -> str:
    data = {
        "project": project_name,
        "scope": "whole system" if scope.whole_system else "only the components below; neighbours are integrated/stubbed",
        "components": [_component_block(state, n) for n in scope.node_ids],
        "connections": [{"from": state.nodes[e.source].name, "to": state.nodes[e.target].name,
                         "protocol": e.protocol, **({"label": e.label} if e.label else {})} for e in scope.edges],
        "neighbours_to_stub": [{"name": state.nodes[b].name, "type": state.nodes[b].type,
                                "technology": state.nodes[b].technology} for b in scope.boundary],
        "requirements": [state.requirements[r].model_dump(exclude={"rev", "field_revs", "deleted"}) for r in scope.requirement_ids],
        "architecture_decisions": [{k: d.get(k) for k in ("id", "title", "status", "decision", "consequences")}
                                   for d in (decisions or []) if d.get("status") in ("accepted", "proposed")],
    }
    parts = [f"<design>\n{json.dumps(data, indent=1)}\n</design>"]
    if memory:
        parts.append(memory)
    parts.append(f"Target coding agent: {TARGETS.get(target, TARGETS['generic'])}.")
    if extra.strip():
        parts.append(f"Extra instructions from the team:\n{extra.strip()}")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- normalizing
def slug(text: str, limit: int = 48) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:limit].strip("-")
    return s if s and s[0].isalpha() else f"c-{s}".strip("-")


_MODAL = re.compile(r"\b(can|could|should|will|would|must|shall|may|needs to|has to|is able to|are able to)\b", re.I)


def _shall(statement: str) -> str:
    """A normative statement: keep SHALL/MUST; "X can/should/will do Y" → "X SHALL do Y"; else "The system SHALL …"."""
    s = " ".join((statement or "").split()).rstrip(".")
    if not s:
        return "The system SHALL behave as specified."
    if re.search(r"\b(SHALL|MUST)\b", s):
        return s + "."
    m = _MODAL.search(s)
    if m and m.start() > 0 and len(s[:m.start()].split()) <= 8:  # a short subject before the modal verb
        rest = re.sub(r"^\s+be able to\b", "", s[m.end():])
        return f"{s[:m.start()]}SHALL{rest}."
    return f"The system SHALL {s[0].lower() + s[1:]}."


def _capability_for(r: Requirement) -> str:
    if r.kind == "non_functional":
        return slug(r.category or "quality-attributes") or "quality-attributes"
    return "core-behaviour"


def _offline_requirement(r: Requirement) -> dict[str, Any]:
    actor = r.actors[0] if r.actors else "a user"
    scenarios = [{"name": _clip(c, 70), "when": f"{actor} exercises “{r.title}”", "then": c} for c in r.acceptance_criteria]
    if not scenarios:
        scenarios = [{"name": "Basic behaviour", "when": f"{actor} uses this capability",
                      "then": r.description or f"the system fulfils “{r.title}”"}]
    statement = " ".join((r.description or r.title).split()).rstrip(".")
    if r.kind == "non_functional" and not _MODAL.search(statement) and not re.search(r"\b(SHALL|MUST)\b", statement):
        statement = f"The system SHALL meet: {statement}"  # a target such as "p95 latency under 300 ms"
    return {"name": r.title, "statement": _shall(statement), "sources": [r.id], "scenarios": scenarios}


def _sentence(text: str) -> str:
    """"data-model" or "setup" → "Data model", "Setup"."""
    if " " not in text:
        text = text.replace("-", " ").replace("_", " ")
    return text[:1].upper() + text[1:]


def _clip(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def normalize(data: dict[str, Any], state: ProjectState, scope: Scope) -> tuple[dict[str, Any], list[str]]:
    """Clean the model output and guarantee coverage: every requirement in scope has a spec requirement and a
    test task. Returns (data, notes about what had to be added)."""
    notes: list[str] = []
    change_id = slug(data.get("change_id") or data.get("title") or "implement-design")
    data["change_id"] = change_id if ID_RE.match(change_id) else "implement-design"
    data["title"] = _clip(data.get("title") or "Implement the design", 100)
    specs = []
    for spec in data.get("specs") or []:
        reqs = []
        for req in spec.get("requirements") or []:
            if not req.get("name"):
                continue
            req["statement"] = _shall(req.get("statement", ""))
            req["sources"] = [s for s in req.get("sources") or [] if s in state.requirements]
            req["scenarios"] = [s for s in req.get("scenarios") or [] if s.get("when") and s.get("then")] or \
                [{"name": "Basic behaviour", "when": "the capability is used", "then": req["statement"]}]
            reqs.append(req)
        if reqs:
            specs.append({"capability": slug(spec.get("capability") or "core-behaviour", 40) or "core-behaviour",
                          "requirements": reqs})
    covered = {s for spec in specs for req in spec["requirements"] for s in req["sources"]}
    for rid in scope.requirement_ids:
        if rid not in covered:
            r = state.requirements[rid]
            cap = _capability_for(r)
            target = next((s for s in specs if s["capability"] == cap), None)
            if target is None:
                target = {"capability": cap, "requirements": []}
                specs.append(target)
            target["requirements"].append(_offline_requirement(r))
            notes.append(f"{rid} was not covered by the generated specs; a spec was derived from the requirement.")
    data["specs"] = specs
    tasks = [{"section": _sentence(_clip(t.get("section") or "Tasks", 80)), "items": [i for i in t.get("items") or [] if i.strip()]}
             for t in data.get("tasks") or []]
    tasks = [t for t in tasks if t["items"]]
    all_tasks = " ".join(i for t in tasks for i in t["items"])
    missing = [rid for rid in scope.requirement_ids if rid not in all_tasks]
    if missing:
        tests = next((t for t in tasks if re.search(r"test|verif", t["section"], re.I)), None)
        if tests is None:
            tests = {"section": "Tests and verification", "items": []}
            tasks.append(tests)
        tests["items"] += [f"Test {rid}: {state.requirements[rid].title}; cover every acceptance criterion"
                           for rid in missing]
    data["tasks"] = tasks
    data.setdefault("proposal", {}).setdefault("what_changes", [])
    data.setdefault("design", {}).setdefault("context", "")
    return data, notes


# --------------------------------------------------------------------------- offline
def offline_change(state: ProjectState, project_name: str, scope: Scope,
                   decisions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    names = [state.nodes[n].name for n in scope.node_ids]
    title = f"Implement {project_name}" if scope.whole_system else f"Implement {', '.join(names[:3])}" + \
        (f" and {len(names) - 3} more" if len(names) > 3 else "")
    reqs = [state.requirements[r] for r in scope.requirement_ids]
    specs: dict[str, list[dict[str, Any]]] = {}
    for r in reqs:
        specs.setdefault(_capability_for(r), []).append(_offline_requirement(r))
    tasks = [{"section": "Setup", "items": ["Create or update the project layout, configuration (environment variables) "
                                            "and tooling for the components below"]}]
    for nid in scope.node_ids:
        n = state.nodes[nid]
        items = [f"Implement {n.name}" + (f" with {n.technology}" if n.technology else "")
                 + (f" (satisfies {', '.join(n.requirement_ids)})" if n.requirement_ids else "")]
        block = _component_block(state, nid)
        items += [f"Add endpoint {e}" for e in block.get("endpoints", [])]
        items += [f"Create table {t['name']}" for t in block.get("tables", [])]
        tasks.append({"section": n.name, "items": items})
    if scope.edges:
        tasks.append({"section": "Integration", "items": [
            f"Connect {state.nodes[e.source].name} → {state.nodes[e.target].name}" + (f" over {e.protocol}" if e.protocol else "")
            for e in scope.edges]})
    tasks.append({"section": "Observability", "items": ["Add structured logging and a health-check endpoint to every service"]})
    tasks.append({"section": "Tests and verification",
                  "items": [f"Test {r.id}: {r.title}; cover every acceptance criterion" for r in reqs]})
    return {
        "change_id": slug(title), "title": title,
        "proposal": {"why": f"Build the approved ArchFlow design for {project_name}.",
                     "what_changes": [f"Implement {n}" for n in names],
                     "impact": [f"Requirements: {', '.join(scope.requirement_ids)}"] if reqs else []},
        "specs": [{"capability": c, "requirements": rs} for c, rs in specs.items()],
        "design": {"context": f"{len(names)} component(s) and {len(scope.edges)} connection(s) from the ArchFlow design.",
                   "goals": [f"Satisfy {r.id}: {r.title}" for r in reqs[:8]], "non_goals": [],
                   "decisions": [{"decision": d["title"], "rationale": d.get("decision") or "", "alternatives": []}
                                 for d in decisions or [] if d.get("status") == "accepted"],
                   "risks": [], "open_questions": []},
        "tasks": tasks,
    }


# --------------------------------------------------------------------------- rendering
def _bullets(items: list[str]) -> list[str]:
    return [f"- {i}" for i in items if i and i.strip()]


def _purpose(spec: dict[str, Any]) -> str:
    """One or two sentences on what a capability is for (OpenSpec wants 50+ characters for a new capability)."""
    names = [r["name"] for r in spec["requirements"]][:3]
    words = spec["capability"].replace("-", " ").replace("/", " ")
    noun = words if words.endswith(("behaviour", "behavior")) else f"{words} behaviour"
    return (f"Defines the {noun} of the system"
            + (f", covering {', '.join(names)}" if names else "") + ", as approved in the ArchFlow design.")


def render(data: dict[str, Any], state: ProjectState, scope: Scope) -> dict[str, str]:
    files: dict[str, str] = {}
    p = data["proposal"]
    caps = [s["capability"] for s in data["specs"]]
    proposal = [f"# Change: {data['title']}", "", "## Why", p.get("why", "").strip(), "", "## What Changes",
                *_bullets(p.get("what_changes", [])), "", "## Capabilities", "", "### New Capabilities",
                *[f"- `{s['capability']}`: {s.get('purpose') or _purpose(s)}" for s in data["specs"]], "",
                "### Modified Capabilities", "- None", "", "## Impact", *_bullets(p.get("impact", [])),
                f"- Affected specs: {', '.join(caps) or 'none'}",
                f"- Components: {', '.join(state.nodes[n].name for n in scope.node_ids) or 'none'}"]
    if scope.requirement_ids:
        proposal.append(f"- Requirements: {', '.join(scope.requirement_ids)}")
    files["proposal.md"] = "\n".join(proposal).strip() + "\n"

    for spec in data["specs"]:
        lines = ["## Purpose", spec.get("purpose") or _purpose(spec), "", "## ADDED Requirements", ""]
        for req in spec["requirements"]:
            lines += [f"### Requirement: {req['name']}", req["statement"]]
            if req.get("sources"):
                lines.append(f"_Source: {', '.join(req['sources'])}_")
            lines.append("")
            for sc in req["scenarios"]:
                lines += [f"#### Scenario: {sc.get('name') or 'Behaviour'}", f"- **WHEN** {sc['when']}", f"- **THEN** {sc['then']}"]
                lines += [f"- **AND** {a}" for a in sc.get("and") or [] if a.strip()]
                lines.append("")
        files[f"specs/{spec['capability']}/spec.md"] = "\n".join(lines).strip() + "\n"

    d = data["design"]
    design = [f"# Design: {data['title']}", "", "## Context", d.get("context", "").strip(), ""]
    if d.get("goals") or d.get("non_goals"):
        design += ["## Goals / Non-Goals", *[f"- Goal: {g}" for g in d.get("goals", [])],
                   *[f"- Non-goal: {g}" for g in d.get("non_goals", [])], ""]
    if d.get("decisions"):
        design.append("## Decisions")
        for dec in d["decisions"]:
            design += [f"- **Decision:** {dec['decision']}", f"  - Rationale: {dec.get('rationale', '')}"]
            if dec.get("alternatives"):
                design.append(f"  - Alternatives considered: {'; '.join(dec['alternatives'])}")
        design.append("")
    design.append("## Architecture (from ArchFlow)")
    for nid in scope.node_ids:
        n = state.nodes[nid]
        design.append(f"- **{n.name}** ({n.type}{', ' + n.technology if n.technology else ''})"
                      + (f": {n.description}" if n.description else ""))
    for e in scope.edges:
        design.append(f"- {state.nodes[e.source].name} → {state.nodes[e.target].name}"
                      + (f" via {e.protocol}" if e.protocol else "") + (f" ({e.label})" if e.label else ""))
    if scope.boundary:
        design.append("- Integrate with, do not build (stub in tests): "
                      + ", ".join(state.nodes[b].name for b in scope.boundary))
    design.append("")
    api, tables = [], []
    for nid in scope.node_ids:
        block = _component_block(state, nid)
        api += [f"- {state.nodes[nid].name}: `{e}`" for e in block.get("endpoints", [])]
        tables += [f"- `{t['name']}` ({state.nodes[nid].name}): {', '.join(t['columns'])}" for t in block.get("tables", [])]
    if api:
        design += ["## API", *api, ""]
    if tables:
        design += ["## Data model", *tables, ""]
    if d.get("risks"):
        design += ["## Risks / Trade-offs", *[f"- {r['risk']} → Mitigation: {r['mitigation']}" for r in d["risks"]], ""]
    if d.get("open_questions"):
        design += ["## Open Questions", *_bullets(d["open_questions"]), ""]
    files["design.md"] = "\n".join(design).strip() + "\n"

    tasks = []
    for i, t in enumerate(data["tasks"], 1):
        tasks += [f"## {i}. {t['section']}", *[f"- [ ] {i}.{j} {item}" for j, item in enumerate(t["items"], 1)], ""]
    files["tasks.md"] = "\n".join(tasks).strip() + "\n"
    return files


def agent_prompt(change_id: str, files: dict[str, str], target: str) -> str:
    agent = TARGETS.get(target, TARGETS["generic"])
    order = ["proposal.md", *sorted(f for f in files if f.startswith("specs/")), "design.md", "tasks.md"]
    head = [f"# Implement OpenSpec change `{change_id}` ({agent})", "",
            f"The change below lives (or should be saved) under `openspec/changes/{change_id}/`. It is the approved "
            "design from ArchFlow.", "",
            "How to work:",
            "1. Read the proposal, the specs and the design. Ask before deviating from them.",
            "2. Work through `tasks.md` in order. Check each item off (`- [x]`) as you finish it.",
            "3. Every `#### Scenario` is a test case: write a test for it, named after the requirement and scenario.",
            "4. Finish with a summary that maps each requirement ID to the code and tests that satisfy it.", ""]
    body = []
    for f in order:
        if f in files:
            body += ["---", f"## File: openspec/changes/{change_id}/{f}", "", files[f].strip(), ""]
    return "\n".join(head + body).strip() + "\n"


# --------------------------------------------------------------------------- entry point
def generate(state: ProjectState, project_name: str, node_ids: list[str], requirement_ids: list[str], *,
             target: str = "claude-code", extra: str = "", memory: str = "",
             decisions: list[dict[str, Any]] | None = None, org_id: str | None = None,
             use_llm: bool = True) -> ChangeSpec:
    state = without_deleted(state)
    scope = resolve_scope(state, node_ids, requirement_ids)
    if not scope.node_ids and not scope.requirement_ids:
        raise ValueError("There is nothing to specify yet: add components or requirements first.")
    engine, notes = "offline", []
    data: dict[str, Any] | None = None
    if use_llm and llm.available(org_id):
        context = build_context(state, project_name, scope, memory=memory, decisions=decisions, target=target, extra=extra)
        try:
            data = agents.run_json(PROMPT, SCHEMA, org_id=org_id, variables={"context": context})
            engine = "llm"
        except (llm.LLMUnavailable, llm.LLMRefused) as exc:
            log.warning("spec generation via LLM failed (%s); deriving it from the model", type(exc).__name__)
            notes.append(f"The AI could not write this change ({exc}); it was derived from the design instead.")
    if data is None:
        data = offline_change(state, project_name, scope, decisions)
    data, more = normalize(data, state, scope)
    notes += more
    files = render(data, state, scope)
    return ChangeSpec(change_id=data["change_id"], title=data["title"], engine=engine, files=files,
                      prompt=agent_prompt(data["change_id"], files, target), notes=notes)
