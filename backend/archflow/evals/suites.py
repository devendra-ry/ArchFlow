"""Eval suites: how each kind of case is run and checked.

A case is a dict from a dataset YAML. Shared keys:
    id: unique within the suite
    project: {template: <name>, ops: [...]}   the project state (ops as in ops.apply_ops)
    expect: {...}                              suite-specific checks, below
    ragas: [faithfulness | answer_accuracy | technically_sound | grounded_in_project, ...]   optional
The intake suite also takes ``document: {filename, text}``: the uploaded file's name and (text) content; the meetings
suite takes ``transcript: {filename, text}``, the knowledge suite ``documents: [{filename, text}]`` and a ``question``,
and the costs suite ``assumptions`` (workload overrides); the compliance suite takes ``frameworks`` (ids) and
``memory`` (project memory texts); the generation suite takes ``kind``, ``target``, an optional
``cloud`` (terraform) and ``name`` (the project name).

The ``adversarial`` suite runs another suite's agent (``target: ask | command | design | review | ...``) on
red-team input (prompt injection in requirements or pasted documents, requests for the system prompt, attempts
to wipe the canvas, PII that must not be echoed) and scores it with that suite's checks.

Each runner returns ``Outcome(response, checks, contexts, question, reference)``; ``checks`` are
``(name, passed, detail)`` and the case score is the share that passed.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import (ai, chat, compliance, costs, evaluation, intake, knowledge, lld, meetings, merge, review, suggestions, exports,
               diagrams, generation, jira)
from ..analysis import full_analysis
from ..models import LLD_TYPES, ProjectState, without_deleted
from ..ops import apply_ops, resolve_node
from ..templates import build_template

Check = tuple[str, bool, str]


@dataclass
class Outcome:
    response: str
    checks: list[Check] = field(default_factory=list)
    question: str = ""
    contexts: list[str] = field(default_factory=list)
    reference: str = ""
    raw: Any = None


def build(spec: dict[str, Any] | None) -> ProjectState:
    spec = spec or {}
    state = build_template(spec.get("template", "blank"))
    if spec.get("ops"):
        state, _ = apply_ops(state, spec["ops"])
    return state


def _text_checks(text: str, expect: dict[str, Any]) -> list[Check]:
    low = text.lower()
    out: list[Check] = []
    for term in expect.get("mentions_all") or []:
        out.append((f"mentions '{term}'", term.lower() in low, ""))
    if expect.get("mentions_any"):
        terms = expect["mentions_any"]
        hit = [t for t in terms if t.lower() in low]
        out.append((f"mentions one of {terms}", bool(hit), ", ".join(hit)))
    for term in expect.get("not_mentions") or []:
        out.append((f"does not mention '{term}'", term.lower() not in low, ""))
    for pattern in expect.get("forbid") or []:
        m = re.search(pattern, text, re.I)
        out.append((f"avoids /{pattern}/", m is None, m.group(0) if m else ""))
    if expect.get("max_words"):
        n = len(text.split())
        out.append((f"at most {expect['max_words']} words", n <= expect["max_words"], f"{n} words"))
    return out


def project_context(state: ProjectState) -> str:
    """The project block the Ask assistant answers from (as main.project_context, without history)."""
    return (f"<project>\n<model>\n{ai._state_for_prompt(state)}\n</model>\n"
            f"<analysis>\n{json.dumps(full_analysis(state)['health'])}\n</analysis>\n</project>")


# --------------------------------------------------------------------------- suites
def run_compare(case: dict[str, Any], org_id: str | None) -> Outcome:
    alts = {name: build(spec) for name, spec in case["alternatives"].items()}
    reqs = evaluation.merged_requirements(build(case.get("project")), alts)
    ev = evaluation.evaluate(reqs, alts, org_id=org_id)
    exp = case.get("expect") or {}
    checks: list[Check] = [("engine is llm", ev["engine"] == "llm", ev["engine"] + "; " + "; ".join(ev["notes"]))]
    if exp.get("recommended"):
        checks.append((f"recommends {exp['recommended']}", ev["recommended"] == exp["recommended"], ev["recommended"]))
    for better, worse, crit in exp.get("scores_higher") or []:
        a, b = ev["scores"].get(better, {}).get(crit), ev["scores"].get(worse, {}).get(crit)
        checks.append((f"{crit}: {better} > {worse}", a is not None and b is not None and a > b, f"{a} vs {b}"))
    if exp.get("reasons", True):
        missing = [f"{b}.{c}" for b in ev["alternatives"] for c in ev["criteria"]
                   if not (ev.get("details", {}).get(b, {}).get("reasons") or {}).get(c)]
        checks.append(("every score has a reason", not missing, ", ".join(missing[:5])))
    checks.append(("has follow-up questions", bool(ev.get("follow_up_questions")), ""))
    checks += _text_checks(ev["rationale"], exp)
    return Outcome(response=ev["rationale"], checks=checks, question="Which architecture fits the requirements best?",
                   contexts=[json.dumps(evaluation.build_state(reqs, alts))], raw=ev)


def run_ask(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    ctx = project_context(state)
    answer = ai.claude_ask(ctx, case["question"], org_id)
    exp = case.get("expect") or {}
    return Outcome(response=answer, checks=[("answered", bool(answer.strip()), "")] + _text_checks(answer, exp),
                   question=case["question"], contexts=[ctx], reference=case.get("reference", ""))


def run_command(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    res = ai.llm_command(state, case["request"], case.get("selection") or [], org_id)
    exp = case.get("expect") or {}
    checks: list[Check] = []
    try:
        after, summary = apply_ops(state, res.ops)
        checks.append(("ops apply cleanly", True, f"{len(res.ops)} op(s)"))
    except Exception as exc:  # noqa: BLE001
        return Outcome(response=res.explanation, checks=[("ops apply cleanly", False, str(exc))], raw=res.ops)
    added = [after.nodes[i] for i in set(after.nodes) - set(state.nodes)]
    removed = [state.nodes[i].name for i in set(state.nodes) - set(after.nodes)]
    for t in exp.get("adds_types") or []:
        checks.append((f"adds a {t}", any(n.type == t for n in added), ", ".join(f"{n.name}({n.type})" for n in added)))
    if exp.get("adds_any"):
        names = " ".join(f"{n.name} {n.technology}" for n in added).lower()
        checks.append((f"adds one of {exp['adds_any']}", any(x.lower() in names for x in exp["adds_any"]), names))
    if exp.get("removes_nothing"):
        checks.append(("removes nothing", not removed, ", ".join(removed)))
    gone = {n.lower() for n in removed}
    for name in exp.get("removes") or []:
        checks.append((f"removes {name}", name.lower() in gone, ", ".join(removed)))
    if "max_removed" in exp:
        checks.append((f"removes at most {exp['max_removed']}", len(removed) <= exp["max_removed"], ", ".join(removed)))
    for name in exp.get("keeps") or []:
        checks.append((f"keeps {name}", name.lower() not in gone, ""))
    for old, new in exp.get("renames") or []:
        node = resolve_node(state, old)
        now = after.nodes.get(node.id) if node else None
        checks.append((f"renames {old} -> {new}", bool(now) and now.name.lower() == new.lower(),
                       now.name if now else "removed"))
    if exp.get("ops_not_mentions"):  # PII or prompt text that must not be written to the canvas
        dumped = json.dumps(res.ops).lower()
        for term in exp["ops_not_mentions"]:
            checks.append((f"canvas does not get '{term}'", term.lower() not in dumped, ""))
    def ids(ref: str) -> set[str]:
        """Components matching ``ref``: exact name, else a name or technology containing it ("Redis")."""
        ref = ref.lower()
        exact = {n.id for n in after.nodes.values() if n.name.lower() == ref}
        return exact or {n.id for n in after.nodes.values() if ref in n.name.lower() or ref in n.technology.lower()}

    for src, dst in exp.get("connects") or []:
        s, d = ids(src), ids(dst)
        ok = any(e.source in s and e.target in d for e in after.edges.values())
        checks.append((f"connects {src} -> {dst}", ok, ""))
    if "max_ops" in exp:
        checks.append((f"at most {exp['max_ops']} ops", len(res.ops) <= exp["max_ops"], str(len(res.ops))))
    checks.append(("explains the change", bool(res.explanation.strip()), ""))
    checks += _text_checks(res.explanation, exp)
    return Outcome(response=res.explanation + "\n" + "\n".join(summary), checks=checks, question=case["request"],
                   contexts=[ai._state_for_prompt(state)], raw=res.ops)


def run_design(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    turns = case.get("turns") or [{"role": "user", "content": case["idea"]}]
    out = chat.llm_turn(turns, state, org_id)
    card = out.get("card") or {}
    exp = case.get("expect") or {}
    checks: list[Check] = []
    if exp.get("action"):
        kind = {"question": "ask", "architectures": "propose"}.get(card.get("type"), card.get("type"))
        checks.append((f"action {exp['action']}", kind == exp["action"], str(kind)))
    if card.get("type") == "question":
        checks.append(("question has 2+ options", len(card.get("options") or []) >= 2, ""))
        text = card.get("question", "") + " " + " ".join(o["label"] for o in card.get("options") or [])
    else:
        options = card.get("options") or []
        checks.append((f"at least {exp.get('min_options', 2)} options", len(options) >= exp.get("min_options", 2),
                       str(len(options))))
        for t in exp.get("component_types_all") or []:
            checks.append((f"every option has a {t}", bool(options) and all(
                any(c["type"] == t for c in o["components"]) for o in options), ""))
        text = " ".join(f"{o['name']} {o['summary']} " + " ".join(c["name"] + " " + c["technology"]
                                                                  for c in o["components"]) for o in options)
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question=turns[-1]["content"], raw=out)


def run_suggestions(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    items = suggestions.suggest(state, case.get("mode", "edit"), selection=case.get("selection"),
                                last_question=case.get("last_question", ""), last_answer=case.get("last_answer", ""),
                                org_id=org_id)
    exp = case.get("expect") or {}
    names = [n.name.lower() for n in state.nodes.values() if n.type != "note"] + [r.lower() for r in state.requirements]
    checks: list[Check] = [("3-4 suggestions", 3 <= len(items) <= 4, str(len(items)))]
    if names:
        grounded = [s["label"] for s in items if any(n in (s["label"] + " " + s["prompt"]).lower() for n in names)]
        share = len(grounded) / len(items) if items else 0
        checks.append(("most name a real component or requirement", share >= exp.get("grounded_share", 0.75),
                       f"{share:.0%}"))
    text = "\n".join(f"- {s['label']}: {s['prompt']}" for s in items)
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question=f"Suggest next steps ({case.get('mode', 'edit')})",
                   contexts=[json.dumps(suggestions.project_summary(state))], raw=items)


def run_review(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    exp = case.get("expect") or {}
    if exp.get("rejected"):  # e.g. an empty canvas: refused before any model call
        try:
            review.review(state, org_id=org_id)
            ok, detail = False, "reviewed anyway"
        except ValueError as exc:
            ok, detail = True, str(exc)
        return Outcome(response=detail, checks=[("rejected", ok, detail)], question="Review this architecture")
    out = review.review(state, org_id=org_id, focuses=case.get("focuses"))
    findings, report = out["findings"], out["specialists"]
    failed = [f"{k}: {v['error']}" for k, v in report.items() if v["status"] != "ok"]
    checks: list[Check] = [("every specialist answered", not failed, "; ".join(failed))]
    n = exp.get("min_findings", 1)
    checks.append((f"at least {n} findings", len(findings) >= n, str(len(findings))))
    if "max_findings" in exp:
        checks.append((f"at most {exp['max_findings']} findings", len(findings) <= exp["max_findings"],
                       str(len(findings))))
    checks.append(("every finding has a fix", bool(findings) and all(f["fix"] for f in findings), ""))
    named = sum(1 for f in findings if f["components"]) / len(findings) if findings else 0
    checks.append(("most findings name a component", named >= exp.get("grounded_share", 0.5), f"{named:.0%}"))
    if exp.get("top_severity"):
        top = findings[0]["severity"] if findings else "none"
        checks.append((f"most severe is {'/'.join(exp['top_severity'])}", top in exp["top_severity"], top))
    flagged = {c.lower() for f in findings for c in f["components"]}
    for name in exp.get("flags") or []:
        checks.append((f"flags {name}", name.lower() in flagged, ", ".join(sorted(flagged))))
    if exp.get("flags_any"):
        hit = [c for c in exp["flags_any"] if c.lower() in flagged]
        checks.append((f"flags one of {exp['flags_any']}", bool(hit), ", ".join(hit)))
    text = "\n".join([v["summary"] for v in report.values() if v.get("summary")] +
                     [f"[{f['severity']}] {f['title']}: {f['detail']} Fix: {f['fix']}" for f in findings])
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question="Review this architecture",
                   contexts=[review._project(state)], raw=out)


def run_lld(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    node = resolve_node(state, case["component"])
    if not node:
        raise KeyError(f"no component '{case['component']}' in the project")
    design, engine = lld.generate_design(state, node, org_id)
    els = design["elements"]
    exp = case.get("expect") or {}
    checks: list[Check] = [("engine is llm", engine == "llm", engine)]
    try:
        apply_ops(state, lld.design_ops(state, node, design))
        checks.append(("design applies cleanly", True, f"{len(els)} element(s)"))
    except Exception as exc:  # noqa: BLE001
        checks.append(("design applies cleanly", False, str(exc)))
    for t in exp.get("element_types_all") or []:
        checks.append((f"has a {t}", any(e["type"] == t for e in els), ""))
    for t in LLD_TYPES:  # endpoints_any, tables_any, events_any, components_any
        if exp.get(f"{t}s_any"):
            names = " ".join(e["name"] for e in els if e["type"] == t).lower()
            checks.append((f"a {t} names one of {exp[f'{t}s_any']}",
                           any(x.lower() in names for x in exp[f"{t}s_any"]), names[:200]))
    tables = [e for e in els if e["type"] == "table"]
    if tables:
        no_pk = [e["name"] for e in tables
                 if not any(c["key"] == "PK" for c in lld.parse_columns(e["props"].get("columns", "")))]
        checks.append(("every table has a primary key", not no_pk, ", ".join(no_pk)))
    for rid in exp.get("links_requirements") or []:
        checks.append((f"links {rid}", any(rid in e["requirement_ids"] for e in els), ""))
    if "max_elements" in exp:
        checks.append((f"at most {exp['max_elements']} elements", len(els) <= exp["max_elements"], str(len(els))))
    text = "\n".join(f"{e['type']} {e['name']}: {e['description']} {e['props'].get('columns', '')}".strip()
                     for e in els)
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question=f"Design the internals of {node.name}",
                   contexts=[lld._llm_prompt(state, node)], raw=design)


def run_adversarial(case: dict[str, Any], org_id: str | None) -> Outcome:
    """Red-team input for another suite's agent (``target``), scored with that suite's checks."""
    target = case.get("target", "")
    if target not in SUITES or target == "adversarial":
        raise ValueError(f"adversarial case needs a target suite, not '{target}'")
    return SUITES[target][0](case, org_id)


def run_merge(case: dict[str, Any], org_id: str | None) -> Outcome:
    """``project`` is the merge base; ``ours`` and ``theirs`` are ops applied to it on each branch."""
    base = build(case.get("project"))
    ours, _ = apply_ops(base, (case.get("ours") or {}).get("ops") or [])
    theirs, _ = apply_ops(base, (case.get("theirs") or {}).get("ops") or [])
    conflicts = merge.merge_states(base, ours, theirs).conflicts
    items = merge.suggest(conflicts, ours, theirs, "source", "target", org_id)
    by_id = {s["id"]: s for s in items}
    exp = case.get("expect") or {}
    checks: list[Check] = [("has conflicts", bool(conflicts), str(len(conflicts))),
                           ("every conflict resolved", set(by_id) == {c["id"] for c in conflicts},
                            f"{len(by_id)}/{len(conflicts)}"),
                           ("every resolution explained", all(s["rationale"] for s in items), "")]
    for key, choice in (exp.get("choices") or {}).items():  # "<name>.<field>" (field "" for a deletion)
        c = next((c for c in conflicts if f"{c['name']}.{c['field']}" == key), None)
        got = by_id.get(c["id"], {}).get("choice") if c else None
        checks.append((f"{key}: {choice}", got == choice, str(got)))
    text = "\n".join(f"- {s['id']}: {s['choice']} ({s['rationale']})" for s in items)
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question="Resolve the merge conflicts",
                   contexts=[json.dumps(conflicts, default=str)], raw=items)


def run_intake(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    d = case["document"]
    doc = intake.read_document(d.get("filename", "spec.md"), d["text"].encode("utf-8"))
    out = intake.propose(doc, state, org_id, use_ai=True)
    reqs = out["requirements"]
    exp = case.get("expect") or {}
    checks: list[Check] = [("engine is llm", out["engine"] == "llm", out["engine"] + "; " + "; ".join(out["notes"]))]
    if "min_requirements" in exp:
        checks.append((f"at least {exp['min_requirements']} requirements", len(reqs) >= exp["min_requirements"],
                       str(len(reqs))))
    if "max_requirements" in exp:
        checks.append((f"at most {exp['max_requirements']} requirements", len(reqs) <= exp["max_requirements"],
                       str(len(reqs))))
    nfr = {r["category"] for r in reqs if r["kind"] == "non_functional"}
    for cat in exp.get("nfr_categories") or []:
        checks.append((f"has a {cat} NFR", cat in nfr, ", ".join(sorted(nfr))))
    for rid in exp.get("duplicates") or []:
        checks.append((f"flags a duplicate of {rid}", any(d["id"] == rid for r in reqs for d in r["duplicates"]), ""))
    for rid in exp.get("conflicts") or []:
        checks.append((f"flags a conflict with {rid}", any(c["id"] == rid for r in reqs for c in r["conflicts"]), ""))
    for key in ("assumptions", "constraints", "open_questions"):
        if exp.get(f"has_{key}"):
            checks.append((f"has {key.replace('_', ' ')}", bool(out[key]), str(len(out[key]))))
    if exp.get("sections"):
        checks.append(("every requirement names its section", all(r["section"] for r in reqs), ""))
    text = "\n".join(f"- [{r['kind']}/{r['priority']}] {r['title']}: {r['description']}" for r in reqs)
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question=f"Extract the requirements from {doc.name}",
                   contexts=[d["text"]], raw=out)


def run_guide(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    b = exports.Bundle(project={"name": case.get("name", "Eval project")}, branch="main", state=state,
                       memory=[{"category": m.get("category", "fact"), "text": m["text"]} for m in case.get("memory") or []],
                       decisions=case.get("decisions") or [])
    ctx = exports.guide_context(b)
    guidance = exports.agent_guidance(ctx, exports.facts_text(ctx), org_id)
    exp = case.get("expect") or {}
    items = [x for _, xs in guidance.items() for x in xs]
    names = [n.name.lower() for n in state.nodes.values() if n.type != "note"] + [r.lower() for r in state.requirements] \
        + [n.technology.lower() for n in state.nodes.values() if n.technology]
    grounded = [x for x in items if any(n in x.lower() for n in names)]
    share = len(grounded) / len(items) if items else 0
    checks: list[Check] = [("every section written", all(guidance.get(k) for k, _ in exports.GUIDANCE_SECTIONS), ""),
                           ("most items name a real component, technology or requirement",
                            share >= exp.get("grounded_share", 0.8), f"{share:.0%}")]
    text = exports._guidance_md(guidance)
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question=f"Write the coding-agent guide for {b.name}",
                   contexts=[exports._skeleton_md(ctx)], raw=guidance)


def run_diagrams(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    found, engine = diagrams.flows(state, org_id, ai=True)
    exp = case.get("expect") or {}
    edges = {(e.source, e.target) for e in state.edges.values()}
    entries = {n.id for n in state.nodes.values() if n.type == "client"} or {p[0] for p in diagrams.candidate_paths(state)}
    checks: list[Check] = [
        ("engine is llm", engine == "llm", engine),
        (f"at least {exp.get('min_flows', 1)} flow(s)", len(found) >= exp.get("min_flows", 1), str(len(found))),
        ("every flow follows connections", all(all((a, b) in edges for a, b in zip(f.path, f.path[1:])) for f in found), ""),
        ("every flow starts at an entry point", all(f.path[0] in entries for f in found), ""),
    ]
    if found:
        share = sum(1 for f in found if f.requirement_ids) / len(found)
        checks.append(("flows name their requirements", share >= exp.get("requirement_share", 0.5), f"{share:.0%}"))
    text = "\n".join(f"- {f.name} ({', '.join(f.requirement_ids)}): " + " -> ".join(state.nodes[x].name for x in f.path)
                     for f in found)
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question="Name the critical flows",
                   contexts=[diagrams._flows_prompt(state, diagrams.candidate_paths(state))], raw=text)


def run_jira(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    m = jira.Model(state)
    inputs = [jira.story_input(m, r) for r in m.functional]
    raw = jira.ai_stories(inputs, "Eval project", org_id=org_id)
    exp = case.get("expect") or {}
    checks: list[Check] = [("one story per functional requirement", set(raw) == {i["requirement_id"] for i in inputs},
                            ", ".join(sorted(raw)))]
    stories = {rid: jira.clean_story(raw[rid], jira.offline_story(i)) for i in inputs if (rid := i["requirement_id"]) in raw}
    gwt = all(s["acceptance_criteria"] and len(raw[rid].get("acceptance_criteria") or []) == len(s["acceptance_criteria"])
              for rid, s in stories.items())
    checks.append(("acceptance criteria are complete Given/When/Then", gwt, ""))
    checks.append(("story points are Fibonacci 1-13", all(raw[r].get("story_points") in jira.FIB for r in stories),
                   ", ".join(f"{r}={raw[r].get('story_points')}" for r in stories)))
    for rid, low in (exp.get("min_points") or {}).items():
        checks.append((f"{rid} at least {low} points", stories.get(rid, {}).get("story_points", 0) >= low, ""))
    for rid, high in (exp.get("max_points") or {}).items():
        checks.append((f"{rid} at most {high} points", stories.get(rid, {}).get("story_points", 99) <= high, ""))
    text = "\n".join(f"{rid} {s['summary']} ({s['story_points']} pts: {s['points_basis']})\n{s['user_story']}\n"
                     + "\n".join(f"- {jira.gwt_text(c)}" for c in s["acceptance_criteria"]) for rid, s in stories.items())
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question="Write Jira stories for the functional requirements",
                   contexts=[json.dumps(inputs)], raw=stories)


def run_meetings(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    tr = case["transcript"]
    t = meetings.read_transcript(tr.get("filename", "meeting.vtt"), tr["text"].encode("utf-8"))
    out = meetings.propose(t, state, org_id, use_ai=True)
    exp = case.get("expect") or {}
    reqs, decisions, actions = out["requirements"], out["decisions"], out["action_items"]
    checks: list[Check] = [("engine is llm", out["engine"] == "llm", out["engine"] + "; " + "; ".join(out["notes"]))]
    for key, items, label in (("requirements", reqs, "requirements"), ("decisions", decisions, "decisions")):
        if f"min_{key}" in exp:
            checks.append((f"at least {exp[f'min_{key}']} {label}", len(items) >= exp[f"min_{key}"], str(len(items))))
        if f"max_{key}" in exp:
            checks.append((f"at most {exp[f'max_{key}']} {label}", len(items) <= exp[f"max_{key}"], str(len(items))))
    for owner in exp.get("action_owners") or []:
        checks.append((f"an action item for {owner}", any(owner.lower() in a["owner"].lower() for a in actions),
                       ", ".join(a["owner"] for a in actions)))
    if exp.get("has_open_questions"):
        checks.append(("has open questions", bool(out["open_questions"]), str(len(out["open_questions"]))))
    if exp.get("speakers"):
        checks.append(("every requirement names its speaker", all(r["speaker"] for r in reqs), ""))
    for term, want in exp.get("agreement") or []:  # [term in the requirement, agreed | proposed | disputed]
        r = next((r for r in reqs if term.lower() in (r["title"] + " " + r["description"]).lower()), None)
        checks.append((f"'{term}' is {want}", bool(r) and r["agreement"] == want, r["agreement"] if r else "missing"))
    text = meetings.to_text(out)
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question=f"Extract requirements and decisions from {t.name}",
                   contexts=[tr["text"]], raw=out)


def run_knowledge(case: dict[str, Any], org_id: str | None) -> Outcome:
    chunks: list[dict[str, Any]] = []
    for d in case["documents"]:
        doc = intake.read_document(d.get("filename", "doc.md"), d["text"].encode("utf-8"))
        for c in knowledge.chunk_sections(doc.sections):
            chunks.append({**c, "document": doc.name, "document_id": doc.name, "type": doc.type, "embedding": None})
    results, mode = knowledge.rank(case["question"], chunks, None, knowledge.TOP_K)
    for i, r in enumerate(results):
        r["ref"] = f"K{i + 1}"
    out = knowledge.answer_from(case["question"], results, mode, org_id, use_ai=True)
    exp = case.get("expect") or {}
    checks: list[Check] = [("engine is llm", out["engine"] == "llm", out["engine"])]
    if "answerable" in exp:
        checks.append((f"answerable is {exp['answerable']}", out["answerable"] == exp["answerable"],
                       str(out["answerable"])))
    cited = {c["document"] for c in out["citations"]}
    for name in exp.get("cites") or []:
        checks.append((f"cites {name}", name in cited, ", ".join(sorted(cited))))
    if exp.get("quotes_verbatim"):
        texts = {r["ref"]: " ".join(r["text"].split()).lower() for r in results}
        ok = all(" ".join(c["quote"].split()).lower() in texts.get(c["ref"], "") for c in out["citations"] if c["quote"])
        checks.append(("every quote is verbatim from its passage", ok, ""))
    checks += _text_checks(out["answer"], exp)
    return Outcome(response=out["answer"], checks=checks, question=case["question"],
                   contexts=[r["text"] for r in results], raw=out)


def run_costs(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    est = costs.estimate(state, [], costs._overrides(case.get("assumptions") or {}))
    recs, _questions, engine = costs.recommend(est, state, "Eval project", org_id, use_ai=True)
    exp = case.get("expect") or {}
    names = {ln["name"] for ln in est["lines"]}
    checks: list[Check] = [("engine is llm", engine == "llm", engine),
                           (f"at least {exp.get('min_recommendations', 1)} recommendation(s)",
                            len(recs) >= exp.get("min_recommendations", 1), str(len(recs))),
                           ("every recommendation names real components",
                            all(r["components"] and set(r["components"]) <= names for r in recs), "")]
    if exp.get("targets_any"):
        hit = [c for c in exp["targets_any"] if any(c in r["components"] for r in recs)]
        checks.append((f"targets one of {exp['targets_any']}", bool(hit), ", ".join(hit)))
    if exp.get("within_budget_first") and (est.get("budget") or {}).get("status") == "over":
        checks.append(("the first recommendation saves the most",
                       bool(recs) and recs[0]["saving_monthly"] == max(r["saving_monthly"] for r in recs), ""))
    text = "\n".join(f"- {r['title']} (${r['saving_monthly']:,.0f}/month; {', '.join(r['components'])}): "
                     f"{r['detail']} Trade-off: {r['tradeoff']}" for r in recs)
    checks += _text_checks(text, exp)
    return Outcome(response=text, checks=checks, question="Recommend cost savings for this architecture",
                   contexts=[json.dumps([{k: ln[k] for k in ("name", "monthly", "basis")} for ln in est["lines"]])],
                   raw=recs)


def run_compliance(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    memory = [{"id": f"mem-{i}", "text": t} for i, t in enumerate(case.get("memory") or [])]
    rep = compliance.report(state, frameworks=case.get("frameworks") or ["soc2"], memory=memory)
    out = compliance.remediate(rep, state, "Eval project", org_id, memory=memory, use_ai=True)
    exp = case.get("expect") or {}
    open_refs = {f"{fw['name']} {c['id']}" for fw in rep["frameworks"] for c in fw["controls"]
                 if c["effective"] in ("fail", "needs_evidence")}
    names = {nid: n.name for nid, n in state.nodes.items()}
    targeted = {names[c] for s in out["steps"] for c in s["components"] if c in names}
    checks: list[Check] = [("engine is llm", out["engine"] == "llm", out["engine"]),
                           (f"at least {exp.get('min_steps', 1)} step(s)", len(out["steps"]) >= exp.get("min_steps", 1),
                            str(len(out["steps"]))),
                           ("every step closes an open control",
                            all(s["controls"] and set(s["controls"]) <= open_refs for s in out["steps"]), "")]
    if "min_requirements" in exp:
        checks.append((f"at least {exp['min_requirements']} proposed requirement(s)",
                       len(out["requirements"]) >= exp["min_requirements"], str(len(out["requirements"]))))
    if exp.get("targets_any"):
        hit = [c for c in exp["targets_any"] if c in targeted]
        checks.append((f"targets one of {exp['targets_any']}", bool(hit), ", ".join(hit)))
    if exp.get("controls_any"):
        covered = {c for s in out["steps"] for c in s["controls"]}
        hit = [c for c in exp["controls_any"] if c in covered]
        checks.append((f"addresses one of {exp['controls_any']}", bool(hit), ", ".join(hit)))
    text = "\n".join([out["summary"]]
                     + [f"- {s['title']} ({', '.join(s['controls'])}; {', '.join(names.get(c, c) for c in s['components'])}): "
                        f"{s['detail']}" for s in out["steps"]]
                     + [f"- NFR: {r['title']}: {r['description']} {' '.join(r['acceptance_criteria'])}"
                        for r in out["requirements"]])
    checks += _text_checks(text, exp)
    findings = [{"control": f"{fw['name']} {c['id']}", "status": c["effective"], "found": c["summary"]}
                for fw in rep["frameworks"] for c in fw["controls"] if c["effective"] in ("fail", "needs_evidence")]
    return Outcome(response=text, checks=checks, question="How should this architecture close its compliance gaps?",
                   contexts=[json.dumps(findings)], raw=out)


def run_generation(case: dict[str, Any], org_id: str | None) -> Outcome:
    state = build(case.get("project"))
    d = generation.Design(name=case.get("name") or "Eval project", branch="main", state=without_deleted(state))
    res = generation.generate(d, case["kind"], case["target"], use_ai=True, org_id=org_id, cloud=case.get("cloud", ""))
    exp = case.get("expect") or {}
    text = "\n\n".join(f"--- {f.path}\n{f.content}" for f in res.files)
    bad = [c for c in res.checks if not c["ok"]]
    checks: list[Check] = [("engine is ai", res.engine == "ai", res.engine + "; " + "; ".join(res.notes)),
                           ("files are well-formed", not bad, "; ".join(f"{c['path']}: {c['detail']}" for c in bad[:3]))]
    if exp.get("min_files"):
        checks.append((f"at least {exp['min_files']} file(s)", len(res.files) >= exp["min_files"], str(len(res.files))))
    for rid in exp.get("covers_requirements") or []:
        checks.append((f"covers {rid}", rid in text, ""))
    if exp.get("paths_any"):
        paths = [f.path for f in res.files]
        hit = [p for p in exp["paths_any"] if any(p in x for x in paths)]
        checks.append((f"has a file like {exp['paths_any']}", bool(hit), ", ".join(paths[:8])))
    checks += _text_checks(text, exp)
    return Outcome(response=text[:20000], checks=checks, question=f"Generate {case['kind']} ({case['target']})",
                   contexts=[generation.design_summary(d)], raw=res.dump())


SUITES: dict[str, tuple[Callable[[dict[str, Any], str | None], Outcome], list[str]]] = {
    # suite: (runner, prompt ids it exercises)
    "compare": (run_compare, ["compare.evaluate"]),
    "ask": (run_ask, ["assistant.ask"]),
    "command": (run_command, ["canvas.command"]),
    "design": (run_design, ["design.turn", "design.repair"]),
    "suggestions": (run_suggestions, ["assistant.suggestions"]),
    "review": (run_review, ["review.specialist"]),
    "lld": (run_lld, ["lld.design"]),
    "merge": (run_merge, ["merge.resolve"]),
    "intake": (run_intake, ["intake.extract"]),
    "adversarial": (run_adversarial, ["assistant.ask", "canvas.command", "design.turn", "review.specialist"]),
    "guide": (run_guide, ["exports.agent_guide"]),
    "diagrams": (run_diagrams, ["diagrams.sequence"]),
    "jira": (run_jira, ["jira.stories"]),
    "meetings": (run_meetings, ["meetings.extract"]),
    "knowledge": (run_knowledge, ["kb.answer"]),
    "costs": (run_costs, ["costs.optimize"]),
    "compliance": (run_compliance, ["compliance.review"]),
    "generation": (run_generation, ["generate.iac", "generate.tests", "generate.docs"]),
}
