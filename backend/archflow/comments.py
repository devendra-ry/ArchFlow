"""Comments: threaded discussion on components, decisions and whole projects, with @mentions.

* A **thread** is a root comment (``parent_id`` empty) on a target: a canvas component (``node``), an
  architecture decision (``decision``, project-wide) or the project itself (``project``). Replies point at the
  root. Threads can be resolved and reopened; resolving is per thread, not per reply.
* **Mentions** are user ids the client picked from the org's members while typing ``@``. The server keeps only
  ids of people in the project's org, and each mentioned person gets a ``mention`` notification. Everyone else
  who already wrote in the thread gets a ``reply`` notification.
* Changes are broadcast to the branch's realtime room (``{"type": "comments"}``) so open workspaces refresh.

Permissions: reading needs any role; writing, replying and resolving need reviewer or above (viewers only read).
Editing a comment's text is for its author; deleting is for its author or an admin; resolving is for the thread's
author or anyone who can edit the project.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from .db.types import now

MAX_BODY = 5000


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=MAX_BODY)
    target_type: str = Field("project", pattern="^(project|node|decision)$")
    target_id: str = Field("", max_length=120)
    parent_id: str = Field("", max_length=40)
    mentions: list[str] = Field(default_factory=list, max_length=50)


class CommentPatch(BaseModel):
    body: str | None = Field(None, min_length=1, max_length=MAX_BODY)
    mentions: list[str] | None = Field(None, max_length=50)
    resolved: bool | None = None


class ReadIn(BaseModel):
    ids: list[int] | None = None  # None: all


def register_routes(app: FastAPI, store: Any, *, authz: Any,
                    broadcast: Callable[[str, str, dict[str, Any]], Awaitable[None]]) -> None:
    read, review = authz.project("read"), authz.project("review")

    def comment_or_404(pid: str, cid: str) -> dict[str, Any]:
        c = store.get_comment(pid, cid)
        if not c:
            raise HTTPException(404, "comment not found")
        return c

    def members_only(org_id: str, ids: list[str]) -> list[str]:
        members = {m["user_id"] for m in store.list_members(org_id)}
        return [i for i in dict.fromkeys(ids) if i in members]

    def notify(comment: dict[str, Any], mentions: list[str], actor_id: str, actor_name: str) -> None:
        mentioned = [u for u in mentions if u != actor_id]
        store.add_notifications(mentioned, comment=comment, kind="mention", actor_name=actor_name)
        if comment["parent_id"]:
            others = [u for u in store.thread_participants(comment["project_id"], comment["parent_id"])
                      if u != actor_id and u not in mentioned]
            store.add_notifications(others, comment=comment, kind="reply", actor_name=actor_name)

    async def changed(c: dict[str, Any], actor: str) -> None:
        await broadcast(c["project_id"], c["branch"], {"type": "comments", "actor": actor})

    @app.get("/api/projects/{pid}/comments")
    def list_comments(pid: str, branch: str = "main", a=Depends(read)):
        return store.list_comments(pid, branch)

    @app.post("/api/projects/{pid}/comments", status_code=201)
    async def post_comment(pid: str, body: CommentIn, branch: str = "main", a=Depends(review)):
        target_type, target_id = body.target_type, body.target_id.strip()
        if body.parent_id:
            root = comment_or_404(pid, body.parent_id)
            if root["parent_id"]:
                raise HTTPException(422, "Reply to the thread, not to a reply.")
            target_type, target_id, branch = root["target_type"], root["target_id"], root["branch"]
        elif target_type != "project" and not target_id:
            raise HTTPException(422, f"target_id is required for a {target_type} comment")
        if target_type == "decision" and not store.get_decision(pid, target_id):
            raise HTTPException(404, "decision not found")
        mentions = members_only(a.org_id, body.mentions)
        c = store.add_comment(pid, branch, author_id=a.user["id"], author_name=a.actor, body=body.body.strip(),
                              target_type=target_type, target_id=target_id if target_type != "project" else "",
                              parent_id=body.parent_id, mentions=mentions)
        notify(c, mentions, a.user["id"], a.actor)
        await changed(c, a.actor)
        return c

    @app.patch("/api/projects/{pid}/comments/{cid}")
    async def patch_comment(pid: str, cid: str, body: CommentPatch, a=Depends(review)):
        c = comment_or_404(pid, cid)
        mine = c["author_id"] == a.user["id"]
        fields: dict[str, Any] = {}
        if body.body is not None or body.mentions is not None:
            if not mine:
                raise HTTPException(403, "Only the author can edit a comment.")
            if body.body is not None:
                fields["body"] = body.body.strip()
            new_mentions: list[str] = []
            if body.mentions is not None:
                fields["mentions"] = members_only(a.org_id, body.mentions)
                new_mentions = [u for u in fields["mentions"] if u not in c["mentions"]]
            fields["edited_at"] = now()
        if body.resolved is not None:
            if c["parent_id"]:
                raise HTTPException(422, "Resolve the thread, not a reply.")
            if not (mine or a.can("edit")):
                raise HTTPException(403, "Only the thread's author or an editor can resolve it.")
            fields["resolved"] = body.resolved
            fields["resolved_by"] = a.actor if body.resolved else ""
        out = store.update_comment(pid, cid, fields)
        if fields.get("mentions") is not None and new_mentions:
            store.add_notifications([u for u in new_mentions if u != a.user["id"]], comment=out, kind="mention",
                                    actor_name=a.actor)
        await changed(out, a.actor)
        return out

    @app.delete("/api/projects/{pid}/comments/{cid}", status_code=204)
    async def delete_comment(pid: str, cid: str, a=Depends(review)):
        c = comment_or_404(pid, cid)
        if c["author_id"] != a.user["id"] and not a.can("admin"):
            raise HTTPException(403, "Only the author or an admin can delete a comment.")
        store.delete_comment(pid, cid)
        await changed(c, a.actor)

    # ---- your notifications (mentions and replies), across every project ----
    @app.get("/api/notifications")
    def list_notifications(limit: int = Query(30, ge=1, le=100), user: dict = Depends(authz.current_user)):
        # Only projects in orgs the user still belongs to (filtered in the query, so the page is full and the
        # unread count matches what the user can see).
        return {"items": store.list_notifications(user["id"], limit), "unread": store.count_unread_notifications(user["id"])}

    @app.post("/api/notifications/read")
    def read_notifications(body: ReadIn, user: dict = Depends(authz.current_user)):
        store.mark_notifications_read(user["id"], body.ids)
        return {"unread": store.count_unread_notifications(user["id"])}

