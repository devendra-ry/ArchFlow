"""Per-project role overrides: give a member a different role on one project.

An org admin, or anyone who is admin of the project (through their org role or an override), manages them. The
override replaces the member's org role on that project, higher or lower: viewer, reviewer, editor (the developer
role) or admin. Rules: org owners are never overridden; you cannot grant a role above your own role on the project;
only org admins can change the project role of an org admin. Overrides end when the member leaves the org.
authz.check_project applies them (see authz.effective_role).
"""

from __future__ import annotations

from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field

from .authz import PROJECT_ROLES, RANK, Access, can, effective_role

ALIASES = {"editor": "developer"}


class ProjectRoleIn(BaseModel):
    role: str = Field(pattern="^(viewer|reviewer|editor|developer|admin)$")


def roles_payload(store, a: Access) -> dict[str, Any]:
    overrides = {r["user_id"]: r for r in store.list_project_roles(a.project["id"])}
    members = []
    for m in store.list_members(a.org_id):
        o = overrides.get(m["user_id"])
        members.append({"user_id": m["user_id"], "name": m["name"], "email": m["email"], "org_role": m["role"],
                        "project_role": o["role"] if o else None,
                        "effective_role": effective_role(m["role"], o["role"] if o else None)})
    return {"members": members, "roles": list(PROJECT_ROLES), "can_manage": can(a.role, "admin")}


def register_routes(app: FastAPI, store: Any, authz: Any) -> None:
    read, admin = authz.project("read"), authz.project("admin")

    def target(a: Access, user_id: str) -> dict[str, Any]:
        m = store.get_member(a.org_id, user_id)
        if not m:
            raise HTTPException(404, "member not found")
        if m["role"] == "owner":
            raise HTTPException(409, "Owners always have full access to every project.")
        if RANK[m["role"]] >= RANK["admin"] and not can(a.org_role, "admin"):
            raise HTTPException(403, "Only an organization admin can change an admin's project role.")
        return m

    @app.get("/api/projects/{pid}/roles")
    def list_roles(a: Access = Depends(read)):
        return roles_payload(store, a)

    @app.put("/api/projects/{pid}/roles/{user_id}")
    def set_role(user_id: str, body: ProjectRoleIn, a: Access = Depends(admin)):
        role = ALIASES.get(body.role, body.role)
        target(a, user_id)
        if RANK[role] > RANK[a.role]:
            raise HTTPException(403, f"You cannot grant a role above your own ({a.role}).")
        store.set_project_role(a.project["id"], user_id, role, a.user["id"])
        return roles_payload(store, a)

    @app.delete("/api/projects/{pid}/roles/{user_id}")
    def clear_role(user_id: str, a: Access = Depends(admin)):
        target(a, user_id)
        store.delete_project_role(a.project["id"], user_id)
        return roles_payload(store, a)
