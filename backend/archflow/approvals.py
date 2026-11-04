"""Version approvals: a saved version is reviewed before it counts as the agreed architecture.

* A saved version starts as **draft**. Someone who can edit asks for a review: **in review**.
* Reviewers, architects, admins and owners approve it or request changes, with an optional comment (requesting
  changes needs one). With the org's required number of approvals from different people it is **approved**; a
  request for changes makes it **changes requested** and drops the approvals so far. A new review request starts
  a fresh round. Developers request reviews but do not approve (``authz.can_approve``).
* The version's author cannot approve it, unless they are the organization's only owner.
* Org policy (``org_approval_policy``): required approvals (default 1) and whether a draft must be approved before
  a decision adopts it. When required, ``check_adopt`` refuses (409) unless the draft's latest version is approved
  and the draft has no changes since.

Every action is a row in ``approvals`` (actor, time, version, comment), a line in the version's branch activity
log, and in-app notifications: reviewers hear about a request; whoever requested the review hears the outcome.
The status is derived from the rows, so changing the required number applies to versions already in review.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .authz import APPROVER_ROLES, can_approve

STATUSES = ("draft", "in_review", "approved", "changes_requested")
LABELS = {"draft": "a draft", "in_review": "in review", "approved": "approved", "changes_requested": "changes requested"}
MAX_COMMENT = 2000
MAX_REQUIRED = 10


class ReviewIn(BaseModel):
    comment: str = Field("", max_length=MAX_COMMENT)


class PolicyIn(BaseModel):
    required_approvals: int = Field(1, ge=1, le=MAX_REQUIRED)
    require_for_adopt: bool = False


# --------------------------------------------------------------------------- status
def derive(rows: list[dict[str, Any]], required: int) -> dict[str, Any]:
    """The review status from a version's actions (oldest first) and the number of approvals required."""
    status, approvals, requester = "draft", [], None
    for r in rows:
        if r["action"] == "request":
            status, approvals, requester = "in_review", [], {"id": r["actor_id"], "name": r["actor_name"]}
        elif r["action"] == "approve" and status == "in_review":
            approvals.append({"id": r["actor_id"], "name": r["actor_name"], "at": r["created_at"]})
        elif r["action"] == "request_changes":
            status, approvals = "changes_requested", []
    if status == "in_review" and len(approvals) >= required:
        status = "approved"
    return {"status": status, "approvals": approvals, "required": required, "requested_by": requester}


def version_status(store: Any, org_id: str, pid: str, number: int) -> dict[str, Any]:
    """A version's review status with its full history."""
    rows = store.list_approvals(pid, number)
    required = store.get_approval_policy(org_id)["required_approvals"]
    return {"version": number, **derive(rows, required), "history": rows}


def with_status(store: Any, org_id: str, pid: str, versions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Version listings with each version's review status (``review``: status, approvals, required)."""
    required = store.get_approval_policy(org_id)["required_approvals"]
    by_version: dict[int, list[dict[str, Any]]] = {}
    for r in store.list_approvals(pid):
        by_version.setdefault(r["version"], []).append(r)
    out = []
    for v in versions:
        d = derive(by_version.get(v["number"], []), required)
        out.append({**v, "review": {"status": d["status"], "approvals": len(d["approvals"]), "required": required}})
    return out


def branch_status(store: Any, org_id: str, pid: str, branch: str) -> dict[str, Any]:
    """The review status of a branch: that of its latest saved version, and whether it changed since."""
    state = store.get_state(pid, branch)
    if state is None:
        raise HTTPException(404, f"branch '{branch}' not found")
    head = store.head_version(pid, branch)
    if head is None:
        return {"branch": branch, "version": None, "status": "draft", "approvals": [], "required":
                store.get_approval_policy(org_id)["required_approvals"], "requested_by": None, "history": [],
                "unsaved_changes": True}
    v = store.get_version(pid, head)
    return {"branch": branch, **version_status(store, org_id, pid, head),
            "unsaved_changes": v["state"].model_dump() != state.model_dump()}


def check_adopt(store: Any, org_id: str, pid: str, source: str) -> None:
    """Raise 409 if the org requires approval before adopting and ``source`` is not approved as it stands."""
    policy = store.get_approval_policy(org_id)
    if not policy["require_for_adopt"]:
        return
    s = branch_status(store, org_id, pid, source)
    n = policy["required_approvals"]
    need = f"{n} approval{'s' if n != 1 else ''}"
    if s["version"] is None:
        raise HTTPException(409, f"Approval is required before adopting: save a version of '{source}' and get "
                                 f"{need} first.")
    if s["status"] != "approved":
        raise HTTPException(409, f"Approval is required before adopting: '{source}' v{s['version']} is "
                                 f"{LABELS[s['status']]} ({len(s['approvals'])}/{n} approvals).")
    if s["unsaved_changes"]:
        raise HTTPException(409, f"Approval is required before adopting: '{source}' has changed since v{s['version']} "
                                 f"was approved. Save a version and get it approved again.")


# --------------------------------------------------------------------------- routes
def register_routes(app: FastAPI, store: Any, *, authz: Any,
                    broadcast: Callable[[str, str, dict[str, Any]], Awaitable[None]]) -> None:
    """Reading needs any role; requesting a review needs developer or above; approving and requesting changes
    need an approver role (reviewer, architect, admin, owner). The org policy is read by members, set by admins."""
    read, edit, review = authz.project("read"), authz.project("edit"), authz.project("review")

    def version_or_404(pid: str, number: int) -> dict[str, Any]:
        v = store.get_version(pid, number, with_state=False)
        if not v:
            raise HTTPException(404, "version not found")
        return v

    def approvers(org_id: str) -> list[str]:
        return [m["user_id"] for m in store.list_members(org_id) if m["role"] in APPROVER_ROLES]

    async def act(pid: str, number: int, action: str, comment: str, a: Any) -> dict[str, Any]:
        v = version_or_404(pid, number)
        comment = comment.strip()
        me = a.user["id"]
        with store.transaction(pid, v["branch"]) as state:  # serializes concurrent reviews of this branch
            before = version_status(store, a.org_id, pid, number)
            status = before["status"]
            if action == "request":
                if status in ("in_review", "approved"):
                    raise HTTPException(409, f"v{number} is already {LABELS[status]}.")
            else:
                if not can_approve(a.role):
                    raise HTTPException(403, f"Your role ({a.role}) cannot review versions; reviewers, architects, "
                                             "admins and owners can.")
                if status != "in_review":
                    raise HTTPException(409, f"v{number} is {LABELS[status]}; only a version in review can be "
                                             + ("approved." if action == "approve" else "sent back for changes."))
            if action == "approve":
                if v["author"] == a.user["name"] and not (a.role == "owner" and store.count_role(a.org_id, "owner") == 1):
                    raise HTTPException(403, "You saved this version, so someone else has to approve it.")
                if any(x["id"] == me for x in before["approvals"]):
                    raise HTTPException(409, f"You already approved v{number}.")
            if action == "request_changes" and not comment:
                raise HTTPException(422, "Say what needs to change.")
            store.add_approval(pid, number, v["branch"], action, actor_id=me, actor_name=a.actor, comment=comment)
            after = version_status(store, a.org_id, pid, number)
            line = {"request": f"requested review of v{number}",
                    "approve": f"approved v{number}" + ("" if after["status"] == "approved"
                                                        else f" ({len(after['approvals'])}/{after['required']})"),
                    "request_changes": f"requested changes to v{number}"}[action]
            store.log_activity(pid, v["branch"], a.actor, [line + (f": {comment[:120]}" if comment else "")],
                               state.rev if state else 0)
        if action == "request":
            to, kind = [u for u in approvers(a.org_id) if u != me], "review_requested"
        else:
            requester = (before["requested_by"] or {}).get("id")
            to = [requester] if requester and requester != me else []
            kind = "changes_requested" if action == "request_changes" else "approved"
        store.add_review_notifications(to, pid=pid, branch=v["branch"], version=number, kind=kind,
                                       actor_name=a.actor, preview=comment or v["message"])
        from . import integrations

        what = {"review_requested": "asked for a review of", "changes_requested": "requested changes to",
                "approved": "approved"}[kind]
        integrations.notify(store, a.org_id, pid, "approval", f"{a.actor} {what} v{number}",
                            comment or v["message"], branch=v["branch"])
        await broadcast(pid, v["branch"], {"type": "versions", "actor": a.actor})
        return after

    @app.get("/api/projects/{pid}/versions/{number}/review")
    def get_version_review(pid: str, number: int, a=Depends(read)):
        version_or_404(pid, number)
        return version_status(store, a.org_id, pid, number)

    @app.get("/api/projects/{pid}/review")
    def get_branch_review(pid: str, branch: str = "main", a=Depends(read)):
        return branch_status(store, a.org_id, pid, branch)

    @app.post("/api/projects/{pid}/versions/{number}/request-review")
    async def request_review(pid: str, number: int, body: ReviewIn, a=Depends(edit)):
        return await act(pid, number, "request", body.comment, a)

    @app.post("/api/projects/{pid}/versions/{number}/approve")
    async def approve(pid: str, number: int, body: ReviewIn, a=Depends(review)):
        return await act(pid, number, "approve", body.comment, a)

    @app.post("/api/projects/{pid}/versions/{number}/request-changes")
    async def request_changes(pid: str, number: int, body: ReviewIn, a=Depends(review)):
        return await act(pid, number, "request_changes", body.comment, a)

    # ---- org policy ----
    @app.get("/api/orgs/{org_id}/approval-policy")
    def get_policy(org_id: str, a=Depends(authz.org("read"))):
        return store.get_approval_policy(org_id)

    @app.put("/api/orgs/{org_id}/approval-policy")
    def set_policy(org_id: str, body: PolicyIn, a=Depends(authz.org("admin"))):
        return store.set_approval_policy(org_id, body.required_approvals, body.require_for_adopt, a.actor)


__all__ = ["STATUSES", "derive", "version_status", "with_status", "branch_status", "check_adopt", "register_routes"]
