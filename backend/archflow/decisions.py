"""Architecture decision records (ADRs), alternative evaluation and adopting a chosen draft.

Service functions here are shared by the REST API (``register_routes``, called from ``main.create_app``) and
the remote MCP tools. Decisions live in the store's ``decisions`` table; their activity entries are logged on
the project's ``main`` branch.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from . import approvals
from . import evaluation as evaluation_mod
from . import history, llm
from .models import ProjectState, keep_requirement_seq
from .store import DECISION_STATUSES

LOG_BRANCH = "main"
BRANCH_PATTERN = r"^[A-Za-z0-9._/-]{1,60}$"


# --------------------------------------------------------------------------- request bodies
class Alternative(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    branch: str | None = None
    summary: str = ""
    pros: list[str] = []
    cons: list[str] = []


class DecisionIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    status: str = Field("proposed", pattern="^(proposed|accepted|rejected|superseded)$")
    context: str = ""
    decision: str = ""
    consequences: str = ""
    alternatives: list[Alternative] = []
    chosen: str = ""
    requirement_ids: list[str] = []
    node_ids: list[str] = []
    evaluation: dict[str, Any] | None = None
    actor: str = "anonymous"


class DecisionPatch(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    status: str | None = Field(None, pattern="^(proposed|accepted|rejected|superseded)$")
    context: str | None = None
    decision: str | None = None
    consequences: str | None = None
    alternatives: list[Alternative] | None = None
    chosen: str | None = None
    requirement_ids: list[str] | None = None
    node_ids: list[str] | None = None
    actor: str = "anonymous"


class EvaluateIn(BaseModel):
    branches: list[str] = Field(min_length=2, max_length=4)
    actor: str = "anonymous"
    create_decision: bool = False
    title: str | None = Field(None, max_length=200)
    base_branch: str = "main"


class CompareAskIn(BaseModel):
    branches: list[str] = Field(min_length=2, max_length=4)
    question: str = Field(min_length=1, max_length=2000)
    evaluation: dict[str, Any] | None = None
    base_branch: str = "main"


class AdoptIn(BaseModel):
    target_branch: str = Field("main", pattern=BRANCH_PATTERN)
    actor: str = "anonymous"


# --------------------------------------------------------------------------- services
def _log(store: Any, pid: str, actor: str, line: str) -> None:
    state = store.get_state(pid, LOG_BRANCH)
    store.log_activity(pid, LOG_BRANCH, actor, [line], state.rev if state else 0)


def create_decision(store: Any, pid: str, data: dict[str, Any], actor: str) -> dict[str, Any]:
    data = dict(data)
    title = data.pop("title")
    d = store.create_decision(pid, title, author=actor, **data)
    _log(store, pid, actor, f"created {d['id']} '{d['title']}' ({d['status']})")
    return d


def update_decision(store: Any, pid: str, did: str, fields: dict[str, Any], actor: str) -> dict[str, Any] | None:
    before = store.get_decision(pid, did)
    if before is None:
        return None
    after = store.update_decision(pid, did, fields)
    if after["status"] != before["status"]:
        _log(store, pid, actor, f"{did} status {before['status']} → {after['status']}")
    elif fields:
        changed = [k for k in fields if before.get(k) != after.get(k)]
        if changed:
            _log(store, pid, actor, f"updated {did} ({', '.join(changed)})")
    return after


def _load_alternatives(store: Any, pid: str, branches: list[str],
                       base_branch: str) -> tuple[ProjectState, dict[str, ProjectState]]:
    """(merged requirements, branch → state). Raises ValueError for a bad count, KeyError for a missing branch."""
    uniq = list(dict.fromkeys(b.strip() for b in branches if b.strip()))
    if not 2 <= len(uniq) <= 4:
        raise ValueError("choose 2 to 4 different branches")
    alts: dict[str, ProjectState] = {}
    for b in uniq:
        st = store.get_state(pid, b)
        if st is None:
            raise KeyError(b)
        alts[b] = st
    base = store.get_state(pid, base_branch)
    return evaluation_mod.merged_requirements(base, alts), alts


def evaluate_branches(store: Any, pid: str, branches: list[str], base_branch: str = "main",
                      org_id: str | None = None) -> dict[str, Any]:
    """Load the branches and run the llm → heuristic chain. Raises KeyError for a missing branch."""
    reqs, alts = _load_alternatives(store, pid, branches, base_branch)
    out = evaluation_mod.evaluate(reqs, alts, org_id=org_id)
    out["requirement_ids"] = list(reqs.requirements)
    return out


def decision_from_evaluation(store: Any, pid: str, evaluation: dict[str, Any], actor: str,
                             title: str | None = None) -> dict[str, Any]:
    """A proposed ADR whose alternatives are the evaluated branches and whose choice is the recommendation."""
    branches: list[str] = evaluation["alternatives"]
    labels = evaluation.get("criteria_labels") or evaluation_mod.CRITERIA_LABELS
    alternatives = []
    for b in branches:
        st = store.get_state(pid, b) or ProjectState()
        summ = evaluation_mod.architecture_summary(st)
        techs = sorted({c.get("technology") for c in summ["components"] if c.get("technology")})[:6]
        s = evaluation["scores"].get(b, {})
        det = (evaluation.get("details") or {}).get(b) or {}
        alternatives.append({
            "name": b, "branch": b,
            "summary": det.get("summary") or (
                f"{summ['counts']['components']} components, {summ['counts']['connections']} connections"
                + (f"; {', '.join(techs)}" if techs else "")),
            "pros": det.get("strengths") or [f"{labels.get(c, c)} {v:g}/5" for c, v in s.items() if v >= 4],
            "cons": det.get("risks") or [f"{labels.get(c, c)} {v:g}/5" for c, v in s.items() if v <= 2],
        })
    rec = evaluation["recommended"]
    rec_alt = next(a for a in alternatives if a["name"] == rec)
    n_req = len(evaluation.get("requirement_ids", []))
    context = (f"{len(branches)} candidate architectures ({', '.join(branches)}) were compared against "
               f"{n_req} requirement(s) on {', '.join(labels.get(c, c).lower() for c in evaluation['criteria'])}. "
               f"Evaluation engine: {evaluation['engine']}.")
    decision = f"Adopt the architecture in branch '{rec}'. {evaluation.get('rationale', '')}".strip()
    consequences = "; ".join(
        x for x in [("Strengths: " + ", ".join(rec_alt["pros"])) if rec_alt["pros"] else "",
                    ("Risks to manage: " + ", ".join(rec_alt["cons"])) if rec_alt["cons"] else ""] if x
    ) or "No standout strengths or weaknesses on the scored criteria."
    if evaluation.get("flip_conditions"):
        consequences += f". Revisit if: {evaluation['flip_conditions']}"
    stored_eval = {k: v for k, v in evaluation.items() if k != "raw"}
    return create_decision(store, pid, {
        "title": title or f"Choose an architecture: {' vs '.join(branches)}",
        "status": "proposed", "context": context, "decision": decision, "consequences": consequences,
        "alternatives": alternatives, "chosen": rec, "requirement_ids": evaluation.get("requirement_ids", []),
        "node_ids": [], "evaluation": stored_eval,
    }, actor)


def chosen_branch(store: Any, pid: str, decision: dict[str, Any]) -> str | None:
    """The branch behind the decision's chosen alternative (by alternative name or branch), if it exists."""
    chosen = (decision.get("chosen") or "").strip()
    if not chosen:
        return None
    for alt in decision.get("alternatives") or []:
        if chosen in (alt.get("name"), alt.get("branch")) and alt.get("branch"):
            if store.get_state(pid, alt["branch"]) is not None:
                return alt["branch"]
    return chosen if store.get_state(pid, chosen) is not None else None


def with_bumped_revs(new: ProjectState, current: ProjectState) -> ProjectState:
    """Copy of ``new`` whose revisions all follow ``current`` (keeps conflict detection valid), as restore does."""
    out = keep_requirement_seq(new.model_copy(deep=True), current)
    out.rev = current.rev + 1
    for coll in (out.nodes, out.edges, out.requirements):
        for item in coll.values():
            item.rev = out.rev
            item.field_revs = {f: out.rev for f in item.field_revs}
    return out


# --------------------------------------------------------------------------- routes
def register_routes(app: FastAPI, store: Any, *, authz: Any,
                    sync_to_live_doc: Callable[..., Awaitable[None]],
                    broadcast: Callable[[str, str, dict[str, Any]], Awaitable[None]]) -> None:
    """Routes are authorized by ``authz`` (authz.Authz): reading needs any role; reviewers may change only a
    decision's status; everything else needs developer or above. ``actor`` fields in bodies are ignored."""

    def decision_or_404(pid: str, did: str) -> dict[str, Any]:
        d = store.get_decision(pid, did)
        if not d:
            raise HTTPException(404, "decision not found")
        return d

    @app.get("/api/projects/{pid}/decisions")
    def list_decisions(pid: str, a=Depends(authz.project("read"))):
        return store.list_decisions(pid)

    @app.post("/api/projects/{pid}/decisions", status_code=201)
    def post_decision(pid: str, body: DecisionIn, a=Depends(authz.project("edit"))):
        data = body.model_dump(exclude={"actor"})
        return create_decision(store, pid, data, a.actor)

    @app.get("/api/projects/{pid}/decisions/{did}")
    def get_decision(pid: str, did: str, a=Depends(authz.project("read"))):
        return decision_or_404(pid, did)

    @app.patch("/api/projects/{pid}/decisions/{did}")
    def patch_decision(pid: str, did: str, body: DecisionPatch, a=Depends(authz.project("review"))):
        decision_or_404(pid, did)
        fields = body.model_dump(exclude={"actor"}, exclude_unset=True)
        fields = {k: v for k, v in fields.items() if v is not None}
        if set(fields) - {"status"} and not a.can("edit"):
            raise HTTPException(403, f"Your role ({a.role}) can only change a decision's status.")
        try:
            return update_decision(store, pid, did, fields, a.actor)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/projects/{pid}/evaluate")
    async def evaluate(pid: str, body: EvaluateIn, a=Depends(authz.project("edit"))):
        try:
            result = await asyncio.to_thread(evaluate_branches, store, pid, body.branches, body.base_branch,
                                             a.org_id)
        except KeyError as exc:
            raise HTTPException(404, f"branch '{exc.args[0]}' not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if body.create_decision:
            result["decision"] = decision_from_evaluation(store, pid, result, a.actor, body.title)
        return result

    @app.post("/api/projects/{pid}/evaluate/ask")
    async def evaluate_ask(pid: str, body: CompareAskIn, a=Depends(authz.project("read"))):
        """A follow-up question about a comparison ("what if cost matters most?"), answered by the LLM."""
        try:
            reqs, alts = _load_alternatives(store, pid, body.branches, body.base_branch)
        except KeyError as exc:
            raise HTTPException(404, f"branch '{exc.args[0]}' not found") from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not llm.available(a.org_id):
            raise HTTPException(503, "No AI model is configured. Add a provider key in AI settings to ask about "
                                     "a comparison.")
        try:
            return await asyncio.to_thread(evaluation_mod.ask, reqs, alts, body.evaluation, body.question, a.org_id)
        except llm.LLMRefused:
            return {"answer": "The model declined to answer this question.", "follow_ups": []}
        except llm.LLMUnavailable as exc:
            raise HTTPException(503, f"The AI model is unavailable: {exc}") from exc

    @app.post("/api/projects/{pid}/decisions/{did}/adopt")
    async def adopt(pid: str, did: str, body: AdoptIn, a=Depends(authz.project("edit"))):
        actor = a.actor
        d = decision_or_404(pid, did)
        if d["status"] not in ("proposed", "accepted"):
            raise HTTPException(409, f"{did} is {d['status']}; only proposed or accepted decisions can be adopted")
        source = chosen_branch(store, pid, d)
        if not source:
            raise HTTPException(422, f"{did} has no chosen alternative backed by an existing branch")
        target = body.target_branch
        if source == target:
            raise HTTPException(422, f"the chosen branch is already '{target}'")
        approvals.check_adopt(store, a.org_id, pid, source)  # 409 unless approved, when the org requires it
        chosen_state = store.get_state(pid, source)
        current = store.get_state(pid, target)
        if current is None:
            raise HTTPException(404, f"branch '{target}' not found")
        safety = store.commit(pid, target, f"Before adopting {source} ({did})", actor, current, a.user["id"])
        with store.transaction(pid, target) as current:
            if current is None:
                raise HTTPException(404, f"branch '{target}' not found")
            adopted = with_bumped_revs(chosen_state, current)
            store.save_state(pid, target, adopted)
            summary = [f"adopted {source} ({did})"]
            store.log_activity(pid, target, actor, summary, adopted.rev)
            history.record(store, pid, target, actor, current, adopted)
        await sync_to_live_doc(pid, target, current, adopted, actor)
        version = store.commit(pid, target, f"Adopted {source} ({did})", actor, adopted, a.user["id"])
        decision = update_decision(store, pid, did, {"status": "accepted"}, actor)
        await broadcast(pid, target, {"type": "state", "state": adopted.model_dump(), "actor": actor,
                                      "summary": summary})
        await broadcast(pid, target, {"type": "versions", "actor": actor})
        return {"version": version["number"], "safety_version": safety["number"], "branch": target,
                "source_branch": source, "decision": decision}


__all__ = ["DECISION_STATUSES", "register_routes", "create_decision", "update_decision", "evaluate_branches",
           "decision_from_evaluation", "chosen_branch"]
