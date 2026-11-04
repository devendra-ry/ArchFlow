"""ArchFlow HTTP + WebSocket API.

Run:  uvicorn archflow.main:app --reload --port 8000

Every route requires a signed-in session (cookie, see users.py) and checks the caller's role in the project's
org (see authz.py), except: /api/health, /api/catalog, /api/templates, /api/prompt-targets, /api/mcp-info (static
metadata), /api/auth/signup|login|logout, GET /api/invites/{token} (token-gated), /api/internal/* (shared secret
with the collab server) and the MCP/OAuth endpoints (bearer tokens / OAuth flow).
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from . import (accounts, ai, approvals, openspec_service, diagram_export, auth, catalog, chat, collab_sync, comments, conversations,
               decisions, lld, llm, history, intake, observability, review, secrets_box, suggestions, usage, users, exports, diagrams, jira,
               costs, integrations, knowledge, meetings, scale, compliance, mfa, project_roles, sso)
from .analysis import full_analysis, trace_requirement
from .codegraph.service import CodeGraphService
from .codegraph.service import register_routes as register_codegraph_routes
from .merge import register_routes as register_merge_routes
from .authz import ROLES, Access, Authz, CSRFMiddleware, can, cap_scopes
from .diff import diff_states, diff_to_text
from .mcp_remote import Services, build_remote_mcp
from .models import ProjectState
from .ops import ConflictError, OpError, apply_ops, resolve_node, unique_slug
from .store import Store, default_store
from .templates import TEMPLATES, build_template
from .vibe import TARGETS, build_prompt

# Request bodies keep their legacy ``actor`` / ``author`` / ``user_name`` fields so old clients still validate,
# but they are ignored: activity is always attributed to the signed-in user.


# --------------------------------------------------------------------------- request bodies
class ProjectIn(BaseModel):
    name: str
    description: str = ""
    template: str = "blank"
    org_id: str = "org-default"


class OpsIn(BaseModel):
    ops: list[dict[str, Any]]
    actor: str = ""  # ignored


class CommitIn(BaseModel):
    message: str
    author: str = ""  # ignored


class RestoreIn(BaseModel):
    version: int
    actor: str = ""  # ignored


class BranchIn(BaseModel):
    name: str = Field(max_length=60, pattern=r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
    from_version: int | None = None
    from_branch: str = "main"


class PromptIn(BaseModel):
    node_ids: list[str] = []
    requirement_ids: list[str] = []
    target: str = "claude-code"
    extra: str = ""



class CommandIn(BaseModel):
    text: str
    selection: list[str] = []
    actor: str = ""  # ignored


class ProjectionIn(BaseModel):
    state: dict[str, Any]
    actors: list[str] = []


class ConvertIn(BaseModel):
    target: str = Field(pattern="^(aws|azure|gcp|oss)$")
    actor: str = ""  # ignored


class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    user_name: str = ""  # ignored: tokens are created for the signed-in user
    access: str = Field("write", pattern="^(read|write)$")
    expires_in_days: int | None = Field(None, ge=1, le=3650)


class ChatIn(BaseModel):
    turns: list[dict[str, Any]]
    # With a design conversation the server keeps the transcript: ``turns`` then only needs the new user turn.
    conversation_id: str | None = None
    actor: str = ""  # ignored


class ChatApplyIn(BaseModel):
    option: dict[str, Any]
    requirements: list[dict[str, Any]] = []
    mode: str = Field("replace", pattern="^(replace|merge|branch)$")
    branch_name: str | None = Field(None, max_length=60, pattern=r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
    conversation_id: str | None = None
    actor: str = ""  # ignored


class AskIn(BaseModel):
    question: str


class LLDIn(BaseModel):
    actor: str = ""  # ignored
    replace: bool = False


COLOR_RE = re.compile(r"^#[0-9a-fA-F]{3,8}$")
WRITE_ROLES = {r for r in ROLES if can(r, "edit")}


# --------------------------------------------------------------------------- realtime hub
class Hub:
    """Fan-out of state changes and presence for each (project, branch) room. With ARCHFLOW_REDIS_URL the
    ``bus`` (scale.RoomBus) carries messages and presence to the other API instances."""

    def __init__(self, bus: scale.RoomBus | None = None) -> None:
        self.rooms: dict[str, dict[WebSocket, dict[str, Any]]] = {}
        self.bus = bus or scale.RoomBus()

    @staticmethod
    def key(pid: str, branch: str) -> str:
        return f"{pid}:{branch}"

    async def join(self, room: str, ws: WebSocket, user: dict[str, Any]) -> None:
        self.rooms.setdefault(room, {})[ws] = user
        await self.bus.present(room, id(ws), user)
        await self.broadcast_presence(room)

    async def leave(self, room: str, ws: WebSocket) -> None:
        self.rooms.get(room, {}).pop(ws, None)
        await self.bus.absent(room, id(ws))
        await self.broadcast(room, {"type": "left", "client_id": id(ws)})
        await self.broadcast_presence(room)

    async def broadcast(self, room: str, msg: dict[str, Any], exclude: WebSocket | None = None) -> None:
        await self.deliver(room, msg, exclude)
        await self.bus.publish(room, msg)  # other instances (no-op without Redis)

    async def deliver(self, room: str, msg: dict[str, Any], exclude: WebSocket | None = None) -> None:
        """Send to this instance's sockets in the room."""
        data = json.dumps(msg, default=str)
        for ws in list(self.rooms.get(room, {})):
            if ws is exclude:
                continue
            try:
                await ws.send_text(data)
            except Exception:
                self.rooms.get(room, {}).pop(ws, None)

    async def broadcast_presence(self, room: str) -> None:
        users_ = [{**u, "client_id": id(ws)} for ws, u in self.rooms.get(room, {}).items()]
        users_ = await self.bus.presence(room, users_)
        await self.broadcast(room, {"type": "presence", "users": users_})


# --------------------------------------------------------------------------- app
def create_app(store: Store | None = None) -> FastAPI:
    store = store or default_store()
    hub = Hub()
    authz = Authz(store)
    # LLM calls made for an org use that org's own provider keys when it has configured some (BYOK).
    llm.set_org_key_resolver(lambda org_id: secrets_box.org_ai_keys(store, org_id))
    llm.set_org_config_resolver(store.get_ai_config)  # models chosen in AI settings
    # Every LLM call is recorded (tokens, estimated cost) and checked against the org's monthly budget.
    budgets = usage.install(store)
    scale.install()  # shared rate limits when ARCHFLOW_REDIS_URL is set; in-process otherwise

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The remote MCP endpoint's session manager must run for the lifetime of the app.
        hub.bus.start(hub.deliver)  # cross-instance fan-out (only with ARCHFLOW_REDIS_URL)
        try:
            async with app.state.mcp.session_manager.run():
                yield
        finally:
            await hub.bus.stop()

    app = FastAPI(title="ArchFlow", version="0.1.0", lifespan=lifespan)
    origins = users.allowed_origins()
    app.add_middleware(CSRFMiddleware, allowed=origins)
    app.add_middleware(usage.UsageScopeMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])
    app.state.store = store
    app.state.hub = hub
    app.state.authz = authz

    read, edit, admin = (authz.project(x) for x in ("read", "edit", "admin"))

    def project_or_404(pid: str) -> dict[str, Any]:
        project = store.get_project(pid)
        if not project:
            raise HTTPException(404, "project not found")
        return project

    def state_or_404(pid: str, branch: str) -> ProjectState:
        project_or_404(pid)
        state = store.get_state(pid, branch)
        if state is None:
            raise HTTPException(404, f"branch '{branch}' not found")
        return state

    async def sync_to_live_doc(pid: str, branch: str, old: ProjectState, new: ProjectState, actor: str) -> None:
        """Push a server-side change into the live collaborative document (field-level, merges with humans)."""
        if not await collab_sync.push_patch(pid, branch, collab_sync.state_patch(old, new), actor):
            # Collab server unreachable: drop the persisted document so it is rebuilt from this state.
            store.delete_ydoc(pid, branch)

    async def commit_ops(pid: str, branch: str, ops: list[dict[str, Any]], actor: str) -> tuple[ProjectState, list[str]]:
        """The one place state changes: apply atomically, persist, log, broadcast."""
        project_or_404(pid)
        with store.transaction(pid, branch) as state:  # row lock: safe across server processes
            if state is None:
                raise HTTPException(404, f"branch '{branch}' not found")
            new_state, summary = apply_ops(state, ops)
            if new_state is not state:
                store.save_state(pid, branch, new_state)
                store.log_activity(pid, branch, actor, summary, new_state.rev)
                history.record(store, pid, branch, actor, state, new_state)
        if new_state is not state:
            await sync_to_live_doc(pid, branch, state, new_state, actor)
        if summary or new_state.rev != state.rev:
            await hub.broadcast(Hub.key(pid, branch), {
                "type": "state", "state": new_state.model_dump(), "actor": actor, "summary": summary})
        return new_state, summary

    # ---- errors ----
    @app.exception_handler(ConflictError)
    async def _conflict(_, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": "conflict", "conflicts": exc.conflicts})

    @app.exception_handler(OpError)
    async def _op_error(_, exc: OpError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    # ---- meta (public: static metadata only) ----
    @app.get("/api/health")
    def api_health():
        """Liveness: the process answers (no dependency checks, so it is cheap to poll)."""
        return {"ok": True, "ai_mode": ai.MODE, "model": ai.MODEL, "llm": llm.status(),
                "export": {"interactive": diagram_export.available()}, "redis": scale.status(hub.bus)}

    @app.get("/api/ready")
    def api_ready():
        """Readiness for load balancers and Kubernetes: the database answers and the schema is current."""
        from .db import migrate

        try:
            revision = migrate.current(store.engine)
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            return JSONResponse(status_code=503, content={"ready": False, "database": f"{type(exc).__name__}"})
        head = migrate.head()
        ok = revision == head
        # Redis is reported, not required: without it each instance falls back to in-process state.
        return JSONResponse(status_code=200 if ok else 503,
                            content={"ready": ok, "database": store.dialect, "schema": revision, "expected": head,
                                     "redis": scale.status(hub.bus, ping=True)})

    @app.get("/api/catalog")
    def get_catalog():
        return catalog.catalog_payload()

    @app.get("/api/templates")
    def list_templates():
        return [{"key": k, "name": v[0]} for k, v in TEMPLATES.items()]

    # ---- accounts, orgs, members, invites, org AI settings ----
    accounts.register_routes(app, store, authz)
    # ---- two-step sign-in, OIDC single sign-on, per-project roles (mfa.py, sso.py, project_roles.py) ----
    mfa.register_routes(app, store, authz)
    sso.register_routes(app, store, authz)
    project_roles.register_routes(app, store, authz)
    usage.register_routes(app, store, authz, budgets)
    observability.install(store)
    observability.register_routes(app, store, authz)

    # ---- projects ----
    @app.get("/api/projects")
    def list_projects(org_id: str | None = None, user: dict = Depends(authz.current_user)):
        if org_id:
            authz.check_org(user, org_id, "read")
            return store.list_projects(org_id)
        out = [p for o in store.list_user_orgs(user["id"]) if not authz.policy_block(user, o["id"], o["role"])
               for p in store.list_projects(o["id"])]
        return sorted(out, key=lambda p: p["created_at"], reverse=True)

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectIn, user: dict = Depends(authz.current_user)):
        a = authz.check_org(user, body.org_id, "edit")
        project = store.create_project(body.name, body.description, build_template(body.template), org_id=a.org_id)
        state = store.get_state(project["id"], "main")
        if state.nodes or state.requirements:
            store.commit(project["id"], "main", f"Created from template '{body.template}'", a.actor, state)
        return project

    @app.delete("/api/projects/{pid}", status_code=204)
    def delete_project(pid: str, a: Access = Depends(admin)):
        store.delete_project(pid)

    @app.get("/api/projects/{pid}")
    def get_project(pid: str, branch: str = "main", a: Access = Depends(read)):
        state = state_or_404(pid, branch)
        return {"project": a.project, "branch": branch, "branches": store.list_branches(pid),
                "head_version": store.head_version(pid, branch), "state": state.model_dump(),
                "role": a.role, "ai_available": llm.available(a.org_id)}

    # ---- operations ----
    @app.post("/api/projects/{pid}/ops")
    async def post_ops(pid: str, body: OpsIn, branch: str = "main", a: Access = Depends(edit)):
        if not a.can("admin") and any(op.get("op") == "purge_requirement" for op in body.ops):
            raise HTTPException(403, "Only admins can remove a requirement permanently.")
        state, summary = await commit_ops(pid, branch, body.ops, a.actor)
        return {"state": state.model_dump(), "summary": summary}

    @app.get("/api/projects/{pid}/activity")
    def activity(pid: str, branch: str = "main", limit: int = 100, a: Access = Depends(read)):
        return store.list_activity(pid, branch, limit)

    # ---- versions & branches ----
    async def commit_version(pid: str, branch: str, message: str, author: str) -> dict[str, Any]:
        state = state_or_404(pid, branch)
        version = store.commit(pid, branch, message, author, state)
        await hub.broadcast(Hub.key(pid, branch), {"type": "versions", "actor": author})
        integrations.notify(store, project_or_404(pid).get("org_id"), pid, "version",
                            f"{author} saved v{version['number']} on {branch}", message, branch=branch)
        return version

    # ---- repository analysis (code graph → reconstruction, drift, impact); see codegraph/service.py ----
    codegraph = CodeGraphService(store, commit_ops, commit_version)
    app.state.codegraph = codegraph
    register_codegraph_routes(app, codegraph, authz=authz)
    scale.register_routes(app, authz=authz)  # job status / cancel, answered by any instance

    @app.post("/api/projects/{pid}/commit", status_code=201)
    async def commit(pid: str, body: CommitIn, branch: str = "main", a: Access = Depends(edit)):
        return await commit_version(pid, branch, body.message, a.actor)

    @app.get("/api/projects/{pid}/versions")
    def versions(pid: str, a: Access = Depends(read)):
        return approvals.with_status(store, a.org_id, pid, store.list_versions(pid))

    def resolve_ref(pid: str, branch: str, ref: str) -> ProjectState:
        """'working' = branch working state; 'N' or 'vN' = version N; 'branch:<name>' = another branch."""
        if ref == "working":
            return state_or_404(pid, branch)
        if ref.startswith("branch:"):
            return state_or_404(pid, ref.split(":", 1)[1])
        m = re.fullmatch(r"v?(\d+)", ref)
        if not m:
            raise HTTPException(400, f"bad version reference '{ref}'")
        v = store.get_version(pid, int(m.group(1)))
        if not v:
            raise HTTPException(404, f"version {ref} not found")
        return v["state"]

    @app.get("/api/projects/{pid}/diff")
    def diff(pid: str, from_: str = Query(alias="from"), to: str = "working", branch: str = "main",
             a: Access = Depends(read)):
        old, new = resolve_ref(pid, branch, from_), resolve_ref(pid, branch, to)
        d = diff_states(old, new)
        return {**d, "text": diff_to_text(d), "from_state": old.model_dump()}

    @app.post("/api/projects/{pid}/restore")
    async def restore(pid: str, body: RestoreIn, branch: str = "main", a: Access = Depends(edit)):
        v = store.get_version(pid, body.version)
        if not v:
            raise HTTPException(404, "version not found")
        before = store.get_state(pid, branch)
        if before is None:
            raise HTTPException(404, f"branch '{branch}' not found")
        safety = store.commit(pid, branch, f"Before restoring v{body.version}", a.actor, before)  # undo point
        with store.transaction(pid, branch) as current:
            if current is None:
                raise HTTPException(404, f"branch '{branch}' not found")
            restored = v["state"].model_copy(deep=True)
            restored.rev = current.rev + 1  # keep revisions monotonic so conflict detection stays valid
            for coll in (restored.nodes, restored.edges, restored.requirements):
                for item in coll.values():
                    item.rev = restored.rev
                    item.field_revs = {f: restored.rev for f in item.field_revs}
            store.save_state(pid, branch, restored)
            store.log_activity(pid, branch, a.actor, [f"restored v{body.version}"], restored.rev)
            history.record(store, pid, branch, f"{a.actor} (restored v{body.version})", current, restored)
        await sync_to_live_doc(pid, branch, current, restored, a.actor)
        await hub.broadcast(Hub.key(pid, branch), {"type": "state", "state": restored.model_dump(),
                                                   "actor": a.actor, "summary": [f"restored v{body.version}"]})
        await hub.broadcast(Hub.key(pid, branch), {"type": "versions", "actor": a.actor})
        return {"state": restored.model_dump(), "safety_version": safety["number"]}

    @app.post("/api/projects/{pid}/branches", status_code=201)
    def create_branch(pid: str, body: BranchIn, a: Access = Depends(edit)):
        if store.get_state(pid, body.name) is not None:
            raise HTTPException(409, "branch already exists")
        if body.from_version is not None:
            v = store.get_version(pid, body.from_version)
            if not v:
                raise HTTPException(404, "version not found")
            state, base = v["state"], body.from_version
        else:
            state, base = state_or_404(pid, body.from_branch), store.head_version(pid, body.from_branch)
        store.create_branch(pid, body.name, state, base)
        return {"name": body.name, "base_version": base}

    # ---- analysis ----
    @app.get("/api/projects/{pid}/analysis")
    def analysis(pid: str, branch: str = "main", a: Access = Depends(read)):
        return full_analysis(state_or_404(pid, branch), codegraph.code_state(pid))

    @app.get("/api/projects/{pid}/trace/{req_id}")
    def trace(pid: str, req_id: str, branch: str = "main", a: Access = Depends(read)):
        state = state_or_404(pid, branch)
        if req_id not in state.requirements or state.requirements[req_id].deleted:
            raise HTTPException(404, "requirement not found")
        return trace_requirement(state, req_id)

    # ---- HLD → LLD drill-down ----
    def node_or_404(state: ProjectState, node_id: str):
        node = resolve_node(state, node_id)
        if not node:
            raise HTTPException(404, "component not found")
        return node

    @app.post("/api/projects/{pid}/nodes/{node_id}/lld")
    async def generate_lld(pid: str, node_id: str, body: LLDIn, branch: str = "main", a: Access = Depends(edit)):
        """Generate the component's internal (low-level) diagram; applied as ordinary ops, so it syncs live."""
        state = state_or_404(pid, branch)
        node = node_or_404(state, node_id)
        try:
            ops, info = await asyncio.to_thread(lld.build, state, node.id, body.replace, a.org_id)
        except lld.LLDError as exc:
            raise HTTPException(409, str(exc)) from exc
        new_state, summary = await commit_ops(pid, branch, ops, f"{a.actor} (low-level design)")
        return {**info, "summary": summary, "rev": new_state.rev}

    @app.get("/api/projects/{pid}/nodes/{node_id}/views")
    def node_views(pid: str, node_id: str, branch: str = "main", a: Access = Depends(read)):
        """Read-only views derived from the model: API list, ER data, Mermaid sequence and ER diagrams."""
        state = state_or_404(pid, branch)
        return lld.views(state, node_or_404(state, node_id))

    # ---- interactive diagram export (ArchFlow renderer, see diagram_export.py) ----
    def export_response(html: str, name: str) -> HTMLResponse:
        filename = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")[:80] or "diagram"
        return HTMLResponse(html, headers={"Content-Disposition": f'attachment; filename="{filename}.html"',
                                           "Cache-Control": "no-store"})

    async def run_export(fn, *args):
        try:
            return await asyncio.to_thread(fn, *args)
        except diagram_export.ExportUnavailable as exc:
            raise HTTPException(503, str(exc)) from exc
        except diagram_export.ExportError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/projects/{pid}/export/diagram")
    async def export_diagram(pid: str, branch: str = "main", level: str = "", a: Access = Depends(read)):
        """The diagram level (system, or a component's internals) as a standalone interactive HTML file."""
        state = state_or_404(pid, branch)
        title = a.project["name"] + (f" · {state.nodes[level].name} internals" if level in state.nodes else "")
        if level and level not in state.nodes:
            raise HTTPException(404, "component not found")
        html, _layout = await run_export(diagram_export.export_architecture, state, f"{title} ({branch})", level)
        return export_response(html, f"{a.project['name']}-{branch}" + (f"-{level}" if level else ""))

    @app.get("/api/projects/{pid}/nodes/{node_id}/export/sequence")
    async def export_sequence(pid: str, node_id: str, branch: str = "main", a: Access = Depends(read)):
        """A component's main request flow as a standalone interactive sequence diagram."""
        state = state_or_404(pid, branch)
        node = node_or_404(state, node_id)
        flow = lld.sequence_steps(state, node, lld.api_view(state, node))
        html = await run_export(diagram_export.export_sequence, flow, f"{node.name}: request flow")
        return export_response(html, f"{node.name}-request-flow")

    # ---- diagrams as text: C4, sequence, ER, deployment, data flow (Mermaid / PlantUML, see diagrams.py) ----
    @app.get("/api/projects/{pid}/diagrams")
    async def list_diagrams(pid: str, branch: str = "main", ai: bool = False, a: Access = Depends(read)):
        """The diagrams this branch's model yields, with alt text (sources via the route below). ``ai=true`` lets the
        diagrams.sequence agent name the critical flows (when a model is configured)."""
        state = state_or_404(pid, branch)
        found = await asyncio.to_thread(diagrams.all_diagrams, state, a.project["name"], a.org_id, ai)
        return {"branch": branch, "diagrams": [diagrams.summary(d) for d in found]}

    @app.get("/api/projects/{pid}/diagrams/{diagram_id}")
    async def get_diagram(pid: str, diagram_id: str, branch: str = "main", ai: bool = False,
                          fmt: str = Query("", alias="format", pattern="^(|mermaid|plantuml)$"),
                          a: Access = Depends(read)):
        """One diagram as Mermaid and PlantUML text plus alt text; ``format=mermaid|plantuml`` returns that source
        alone as a text file."""
        state = state_or_404(pid, branch)
        d = await asyncio.to_thread(diagrams.get, state, a.project["name"], diagram_id, a.org_id, ai)
        if not d:
            raise HTTPException(404, "diagram not found")
        if fmt:
            ext = "mmd" if fmt == "mermaid" else "puml"
            filename = re.sub(r"[^A-Za-z0-9._-]+", "-", f"{a.project['name']}-{branch}-{diagram_id}").strip("-")[:80]
            return PlainTextResponse(d[fmt], headers={"Content-Disposition": f'attachment; filename="{filename}.{ext}"'})
        return d

    # ---- vibe coding ----
    @app.get("/api/prompt-targets")
    def prompt_targets():
        return [{"key": k, "name": v} for k, v in TARGETS.items()]

    @app.post("/api/projects/{pid}/prompt")
    def prompt(pid: str, body: PromptIn, branch: str = "main", a: Access = Depends(read)):
        state = state_or_404(pid, branch)
        return {"prompt": build_prompt(state, a.project["name"], body.node_ids, body.requirement_ids, body.target,
                                       body.extra)}

    # ---- AI ----
    async def run_ai_command(pid: str, branch: str, text: str, selection: list[str], actor: str,
                             context: str | None = None) -> dict[str, Any]:
        """Apply a natural-language edit. ``context`` is the conversation context (conversations.command_context);
        without one the model still gets the project memory."""
        state = state_or_404(pid, branch)
        org_id = project_or_404(pid).get("org_id")
        if context is None:
            context = conversations.command_context(store, pid, None)
        room = Hub.key(pid, branch)
        await hub.broadcast(room, {"type": "ai_status", "status": "thinking", "actor": actor, "text": text})
        try:
            result = await asyncio.to_thread(ai.run_command, state, text, selection, org_id, context)
        except ai.AIUnavailable as exc:
            await hub.broadcast(room, {"type": "ai_status", "status": "idle"})
            raise HTTPException(503, str(exc)) from exc
        except Exception:
            await hub.broadcast(room, {"type": "ai_status", "status": "idle"})
            raise
        try:
            new_state, summary = await commit_ops(pid, branch, result.ops, f"AI ({actor})")
        except (OpError, ConflictError) as exc:
            await hub.broadcast(room, {"type": "ai_status", "status": "idle"})
            return {"applied": False, "error": str(exc), "explanation": result.explanation,
                    "questions": result.questions, "question_options": result.question_options, "engine": result.engine, "ops": result.ops}
        await hub.broadcast(room, {"type": "ai_status", "status": "idle"})
        return {"applied": True, "summary": summary, "explanation": result.explanation,
                "questions": result.questions, "question_options": result.question_options, "engine": result.engine, "state": new_state.model_dump()}

    @app.post("/api/projects/{pid}/ai/command")
    async def ai_command(pid: str, body: CommandIn, branch: str = "main", a: Access = Depends(edit)):
        return await run_ai_command(pid, branch, body.text, body.selection, a.actor)

    def project_context(pid: str, branch: str) -> str:
        """The project block the assistant answers from: model, health, versions and recent activity."""
        project = project_or_404(pid)
        state = state_or_404(pid, branch)
        history = store.list_versions(pid)[:30]
        acts = store.list_activity(pid, branch, 60)
        return (f"<project name=\"{project['name']}\" branch=\"{branch}\">\n"
                f"<model>\n{ai._state_for_prompt(state)}\n</model>\n"
                f"<analysis>\n{json.dumps(full_analysis(state)['health'])}\n</analysis>\n"
                f"<versions>\n{json.dumps(history, default=str)}\n</versions>\n"
                f"<activity>\n{json.dumps(acts, default=str)}\n</activity>\n</project>")

    @app.post("/api/projects/{pid}/ai/ask")
    async def ai_ask(pid: str, body: AskIn, branch: str = "main", a: Access = Depends(read)):
        """One-off question (no conversation); the threaded, streamed version is /conversations/{cid}/ask."""
        context = project_context(pid, branch)
        memory = conversations.memory_block(store, pid)
        if memory:
            context += "\n" + memory
        kb = await asyncio.to_thread(knowledge.context_block, store, pid, body.question, a.org_id)
        try:
            extra = {"knowledge": kb} if kb else {}
            answer = await asyncio.to_thread(lambda: ai.claude_ask(context, body.question, a.org_id, **extra))
        except ai.AIUnavailable as exc:
            raise HTTPException(503, f"{exc}. Set GEMINI_API_KEY (or ANTHROPIC_API_KEY / OPENAI_API_KEY), or add "
                                     "your organization's key in AI settings, to use the project assistant.") from exc
        return {"answer": answer}

    # ---- cloud conversion ----
    @app.post("/api/projects/{pid}/convert")
    async def convert(pid: str, body: ConvertIn, branch: str = "main", a: Access = Depends(edit)):
        ops, skipped = catalog.convert_ops(state_or_404(pid, branch), body.target)
        _, summary = await commit_ops(pid, branch, ops, f"{a.actor} (convert to {catalog.VENDORS[body.target]})")
        return {"changed": len(ops), "summary": summary, "skipped": skipped}

    # ---- guided design chat ----
    def design_conversation(pid: str, cid: str) -> dict[str, Any]:
        conv = store.get_conversation(cid)
        if not conv or conv["project_id"] != pid:
            raise HTTPException(404, "conversation not found")
        if conv["kind"] != "design":
            raise HTTPException(409, "That conversation is not a design chat.")
        return conv

    @app.post("/api/projects/{pid}/chat")
    async def chat_turn(pid: str, body: ChatIn, branch: str = "main", a: Access = Depends(edit)):
        state = state_or_404(pid, branch)
        memory = conversations.memory_block(store, pid)
        if not body.conversation_id:  # legacy: the client sends the whole transcript and keeps it
            return await asyncio.to_thread(chat.next_turn, body.turns[-60:], state, a.org_id, memory)
        conv = design_conversation(pid, body.conversation_id)
        new = body.turns[-1] if body.turns else None
        if not new or new.get("role") != "user":
            raise HTTPException(422, "Send the new user turn as the last item of 'turns'.")
        turns = conversations.design_turns(store, conv["id"])
        asked = turns[-1].get("card") if turns and turns[-1]["role"] == "assistant" else None
        user_msg = conversations.add_design_turn(store, conv["id"], new, a.actor)
        if not conv["title"] and not new.get("answer"):
            store.update_conversation(conv["id"], {"title": conversations.title_from(str(new.get("content") or ""))})
        if new.get("answer") and asked and asked.get("type") == "question":
            ids = new["answer"].get("option_ids") or []
            labels = [o["label"] for o in asked.get("options", []) if o.get("id") in ids]
            if new["answer"].get("other"):
                labels.append(str(new["answer"]["other"]))
            if conversations.learn_design_answer(store, pid, conv["id"], asked, labels, a.actor):
                await hub.broadcast(Hub.key(pid, branch), {"type": "memory", "actor": a.actor})
                memory = conversations.memory_block(store, pid)
        result = await asyncio.to_thread(chat.next_turn, (turns + [new])[-60:], state, a.org_id, memory)
        msg = conversations.add_design_turn(
            store, conv["id"], {"role": "assistant", "content": result["message"], "card": result["card"],
                                "engine": result["engine"]}, a.actor)
        await hub.broadcast(Hub.key(pid, branch), {"type": "conversation", "id": conv["id"], "actor": a.actor})
        return {**result, "user_message": user_msg, "assistant_message": msg}

    @app.post("/api/projects/{pid}/chat/apply")
    async def chat_apply(pid: str, body: ChatApplyIn, branch: str = "main", a: Access = Depends(edit)):
        conv = design_conversation(pid, body.conversation_id) if body.conversation_id else None
        target = branch
        if body.mode == "branch":
            # A derived name is cut at a word boundary and made unique (draft/x, draft/x-2…); an explicit name
            # that already exists is a conflict.
            target = body.branch_name or unique_slug(body.option.get("name") or "option",
                                                     lambda b: store.get_state(pid, b) is not None, prefix="draft/")
            if store.get_state(pid, target) is not None:
                raise HTTPException(409, f"branch '{target}' already exists")
            store.create_branch(pid, target, state_or_404(pid, branch), store.head_version(pid, branch))
        ops = chat.architecture_ops(state_or_404(pid, target), body.option, body.requirements,
                                    replace=body.mode != "merge")
        new_state, summary = await commit_ops(pid, target, ops, f"{a.actor} (design chat)")
        version = store.commit(pid, target, f"Applied architecture option: {body.option.get('name', 'option')}",
                               a.actor, new_state)
        await hub.broadcast(Hub.key(pid, target), {"type": "versions", "actor": a.actor})
        if conv:  # record the choice; a design chat follows the draft branch it created
            name = body.option.get("name", "option")
            conversations.add_design_turn(store, conv["id"], {"role": "user", "content": f"Use {name}"}, a.actor, "note")
            conversations.add_design_turn(store, conv["id"], {
                "role": "assistant",
                "content": (f"Created draft “{target}” with {name} (v{version['number']}). Switching to it…"
                            if body.mode == "branch" else f"Applied {name} to {target} and saved it as v{version['number']}."),
                "applied": {"option": name, "branch": target, "version": version["number"]}}, a.actor)
            if target != conv["branch"]:
                store.update_conversation(conv["id"], {"branch": target})
        return {"branch": target, "version": version["number"], "summary": summary, "state": new_state.model_dump(),
                **({"conversation": store.get_conversation(conv["id"])} if conv else {})}

    # ---- architecture decisions (ADRs), alternative evaluation, adopting a draft ----
    async def broadcast_room(pid: str, branch: str, msg: dict[str, Any]) -> None:
        await hub.broadcast(Hub.key(pid, branch), msg)

    decisions.register_routes(app, store, authz=authz, sync_to_live_doc=sync_to_live_doc, broadcast=broadcast_room)
    comments.register_routes(app, store, authz=authz, broadcast=broadcast_room)
    approvals.register_routes(app, store, authz=authz, broadcast=broadcast_room)
    suggestions.register_routes(app, store, authz=authz, state_or_404=state_or_404)
    review.register_routes(app, store, authz=authz, state_or_404=state_or_404)
    # branch merge: three-way diff, conflict resolution (optionally AI-assisted), applied as ops (merge.py)
    register_merge_routes(app, store, authz=authz, commit_ops=commit_ops, broadcast=broadcast_room)
    history.register_routes(app, store, authz=authz, state_or_404=state_or_404, commit_ops=commit_ops)
    def notify(org_id: str | None, pid: str, event: str, title: str, text: str, branch: str = "main") -> None:
        integrations.notify(store, org_id, pid, event, title, text, branch=branch)

    intake.register_routes(app, store, authz=authz, state_or_404=state_or_404, commit_ops=commit_ops,
                           broadcast=broadcast_room, notify=notify)
    # ---- meeting transcripts and recordings → requirements, decisions, action items (meetings.py) ----
    meetings.register_routes(app, store, authz=authz, state_or_404=state_or_404, commit_ops=commit_ops,
                             broadcast=broadcast_room, notify=notify)
    # ---- project knowledge base: documents, hybrid search, cited answers (knowledge.py) ----
    knowledge.register_routes(app, store, authz=authz, broadcast=broadcast_room)
    # ---- cost estimate of the architecture (costs.py) ----
    costs.register_routes(app, store, authz=authz, state_or_404=state_or_404)
    # ---- compliance frameworks: SOC 2, HIPAA, GDPR, PCI DSS, ISO 27001 (compliance.py) ----
    compliance.register_routes(app, store, authz=authz, state_or_404=state_or_404)
    # ---- GitHub Issues, Confluence, Slack and Teams (integrations.py) ----
    integrations.register_routes(app, store, authz=authz, state_or_404=state_or_404, broadcast=broadcast_room)
    # ---- spec exports (Markdown, print/PDF, JSON, YAML) and coding-agent guides (exports.py) ----
    exports.register_routes(app, store, authz=authz, state_or_404=state_or_404)
    jira.register_routes(app, store, authz=authz, state_or_404=state_or_404, broadcast=broadcast_room, commit_ops=commit_ops)

    # ---- OpenSpec: spec-driven changes stored per project (openspec_service.py) ----
    def openspec_context(pid: str, branch: str) -> dict[str, Any]:
        return {"state": state_or_404(pid, branch), "name": project_or_404(pid)["name"],
                "memory": conversations.memory_block(store, pid), "decisions": store.list_decisions(pid)}

    openspec_service.register_routes(app, store, authz=authz, project_context=openspec_context)

    # ---- conversations (threaded, streamed AI chat) and project memory ----
    conversations.register_routes(app, store, authz=authz, project_context=project_context,
                                  run_command=run_ai_command, broadcast=broadcast_room)

    # ---- real-time collaboration ticket (for the Hocuspocus collab server) ----
    @app.get("/api/projects/{pid}/collab-ticket")
    def collab_ticket(pid: str, branch: str = "main", color: str = "", a: Access = Depends(read)):
        """A short-lived signed ticket the browser presents to the collab server. Viewers and reviewers get a
        read-only connection (the collab server rejects their document updates)."""
        state_or_404(pid, branch)
        doc = collab_sync.doc_name(pid, branch)
        read_only = not a.can("edit")
        try:
            ticket = collab_sync.sign_ticket({"uid": a.user["id"], "name": a.user["name"][:40],
                                              "color": color if COLOR_RE.match(color) else "#888888",
                                              "doc": doc, "ro": read_only})
        except RuntimeError as exc:
            raise HTTPException(503, f"Real-time collaboration is not configured: {exc}") from exc
        return {"ticket": ticket, "doc": doc, "read_only": read_only, "expires_in": collab_sync.TICKET_TTL}

    # ---- internal: collab server ↔ API (shared-secret authenticated) ----
    def internal_auth(token: str | None) -> None:
        if not collab_sync.INTERNAL_TOKEN:
            raise HTTPException(503, "ARCHFLOW_INTERNAL_TOKEN is not configured")
        if not token or not secrets.compare_digest(token, collab_sync.INTERNAL_TOKEN):
            raise HTTPException(401, "invalid internal token")

    @app.get("/api/internal/state/{name:path}")
    def get_internal_state(name: str, x_internal_token: str | None = Header(None)):
        """The branch's structured state, used by the collab server to seed a new live document."""
        internal_auth(x_internal_token)
        pid, branch = collab_sync.parse_doc_name(name)
        return {"state": state_or_404(pid, branch).model_dump()}

    @app.get("/api/internal/ydocs/{name:path}")
    def get_ydoc(name: str, x_internal_token: str | None = Header(None)):
        internal_auth(x_internal_token)
        pid, branch = collab_sync.parse_doc_name(name)
        data = store.get_ydoc(pid, branch)
        if data is None:
            raise HTTPException(404, "no stored document")
        return Response(content=data, media_type="application/octet-stream")

    @app.put("/api/internal/ydocs/{name:path}", status_code=204)
    async def put_ydoc(name: str, request: Request, x_internal_token: str | None = Header(None)):
        internal_auth(x_internal_token)
        pid, branch = collab_sync.parse_doc_name(name)
        state_or_404(pid, branch)
        store.put_ydoc(pid, branch, await request.body())

    @app.put("/api/internal/projection/{name:path}")
    async def put_projection(name: str, body: ProjectionIn, x_internal_token: str | None = Header(None)):
        """The live document's current content; becomes the branch state used by AI, MCP and analysis."""
        internal_auth(x_internal_token)
        pid, branch = collab_sync.parse_doc_name(name)
        incoming, dropped = collab_sync.sanitize(body.state)
        with store.transaction(pid, branch) as current:
            if current is None:
                raise HTTPException(404, "branch not found")
            merged, summary = collab_sync.merge_projection(current, incoming)
            if summary:
                store.save_state(pid, branch, merged)
                actor = ", ".join(sorted(set(body.actors))) or "collaborator"
                store.log_activity(pid, branch, actor, summary[:30], merged.rev)
                history.record(store, pid, branch, actor, current, merged)
        if summary:
            await hub.broadcast(Hub.key(pid, branch), {"type": "activity", "actor": body.actors})
        return {"changed": bool(summary), "rev": merged.rev, "dropped": dropped}

    # ---- realtime (server events: AI status, versions, activity) ----
    @app.websocket("/ws/{pid}/{branch:path}")
    async def ws_endpoint(ws: WebSocket, pid: str, branch: str, color: str = "#888"):
        origin = ws.headers.get("origin")
        if origin and users.origin_of(origin) not in origins:
            await ws.close(code=4403)  # cross-site WebSocket hijacking guard
            return
        user = authz.user_from_token(ws.cookies.get(users.SESSION_COOKIE))
        if not user:
            await ws.close(code=4401)
            return
        try:
            authz.check_project(user, pid, "read")
        except HTTPException:
            await ws.close(code=4404)
            return
        if store.get_state(pid, branch) is None:
            await ws.close(code=4404)
            return
        await ws.accept()
        name, color = user["name"][:40], (color if COLOR_RE.match(color) else "#888")
        room = Hub.key(pid, branch)
        await ws.send_text(json.dumps({"type": "hello", "client_id": id(ws)}))
        await hub.join(room, ws, {"name": name, "color": color})
        try:
            while True:
                msg = json.loads(await ws.receive_text())
                if msg.get("type") in ("cursor", "selection"):
                    # Ephemeral awareness data: relay only, never persisted.
                    await hub.broadcast(room, {**msg, "client_id": id(ws), "name": name, "color": color}, exclude=ws)
        except (WebSocketDisconnect, json.JSONDecodeError):
            pass
        finally:
            await hub.leave(room, ws)

    # ---- remote MCP server (Streamable HTTP + OAuth 2.1) ----
    mcp = build_remote_mcp(Services(store=store, commit_ops=commit_ops, commit_version=commit_version,
                                    ai_command=run_ai_command, codegraph=codegraph, broadcast=broadcast_room))
    provider: auth.ArchFlowOAuthProvider = mcp._archflow_provider
    app.state.mcp = mcp

    def safe_next(url: str | None) -> str | None:
        """Only same-site consent URLs are valid post-login destinations (no open redirect)."""
        return url if url and re.fullmatch(r"/oauth/consent\?request=[A-Za-z0-9_\-]+", url) else None

    def to_login(request_id: str) -> RedirectResponse:
        nxt = f"/oauth/consent?request={request_id}"
        return RedirectResponse(f"/oauth/login?next={quote(nxt, safe='')}", status_code=303)

    @app.get("/oauth/login", response_class=HTMLResponse)
    def oauth_login_form(request: Request, next: str = ""):
        nxt = safe_next(next)
        if nxt and authz.session_user(request):
            return RedirectResponse(nxt, status_code=303)
        return HTMLResponse(auth.login_page(nxt or "", users.frontend_url()))

    @app.post("/oauth/login")
    async def oauth_login_submit(request: Request):
        form = await request.form()
        email = users.normalize_email(str(form.get("email", "")))[:320]
        password = str(form.get("password", ""))[:users.MAX_PASSWORD]
        nxt = safe_next(str(form.get("next", "")))
        mfa_token = str(form.get("mfa_token", ""))[:200]
        if mfa_token:  # second step: the code from the authenticator app (or a recovery code)
            challenge = mfa.peek_challenge(store, mfa_token)
            if not challenge:
                return HTMLResponse(auth.login_page(nxt or "", users.frontend_url(), "This sign-in has expired. Sign in again."), 401)
            try:
                ok = mfa.check_code(store, challenge["user_id"], str(form.get("code", ""))[:40])
            except HTTPException as exc:
                return HTMLResponse(auth.login_page(nxt or "", users.frontend_url(), str(exc.detail), mfa_token=mfa_token), 429)
            if not ok or not mfa.finish_challenge(store, mfa_token):
                return HTMLResponse(auth.login_page(nxt or "", users.frontend_url(), "That code is not right.", mfa_token=mfa_token), 401)
            user_id, method = challenge["user_id"], challenge["auth_method"]
        else:
            user_id, method = None, "password"
        key = users.limiter_key(email, accounts.client_ip(request))
        if users.login_limiter.check(key):
            return HTMLResponse(auth.login_page(nxt or "", users.frontend_url(),
                                                "Too many sign-in attempts. Try again later.", email), 429)
        if user_id is None:
            rec = store.get_user_by_email(email, with_hash=True)
            if not users.verify_password(password, rec["password_hash"] if rec else None):
                users.login_limiter.failure(key)
                return HTMLResponse(auth.login_page(nxt or "", users.frontend_url(), "Invalid email or password.", email), 401)
            users.login_limiter.success(key)
            user_id = rec["id"]
            if store.mfa_enabled(user_id):
                return HTMLResponse(auth.login_page(nxt or "", users.frontend_url(),
                                                    mfa_token=mfa.start_challenge(store, user_id, "password")))
        store.touch_login(user_id)
        resp: Response = (RedirectResponse(nxt, status_code=303) if nxt else
                          HTMLResponse("<p>Signed in. You can close this window and return to your app.</p>"))
        accounts.set_session_cookie(resp, users.create_session(store, user_id, request.headers.get("user-agent", ""), method))
        return resp

    @app.get("/oauth/consent", response_class=HTMLResponse)
    def consent_form(request: Request, request_id: str = Query(alias="request")):
        req = provider.pending_request(request_id)
        if not req:
            return HTMLResponse("<p>This authorization request has expired. Start the connection again from your app.</p>", 400)
        user = authz.session_user(request)
        if not user:
            return to_login(request_id)
        orgs = [o for o in store.list_user_orgs(user["id"]) if not authz.policy_block(user, o["id"], o["role"])]
        return HTMLResponse(auth.consent_page(req, request_id, orgs, user, WRITE_ROLES))

    @app.post("/oauth/consent")
    async def consent_submit(req: Request):
        form = await req.form()
        request_id = str(form.get("request", ""))
        org_id = str(form.get("org_id", ""))
        approve = form.get("decision") == "approve"
        user = authz.session_user(req)
        scopes: list[str] = []
        if approve:
            if not user:
                return to_login(request_id)
            role = store.get_role(org_id, user["id"])
            if role is None or authz.policy_block(user, org_id, role):  # the org's SSO / MFA rules apply here too
                pending = provider.pending_request(request_id)
                if not pending:
                    return HTMLResponse("<p>This authorization request has expired.</p>", 400)
                return HTMLResponse(auth.consent_page(pending, request_id, store.list_user_orgs(user["id"]), user,
                                                      WRITE_ROLES, "Choose one of your organizations."), 400)
            requested = [auth.SCOPE_READ, auth.SCOPE_WRITE] if form.get("access") == "write" else [auth.SCOPE_READ]
            scopes = cap_scopes(role, requested)  # viewers and reviewers can only grant read access
        try:
            url = provider.complete_consent(request_id, approve, org_id, user, scopes)
        except ValueError as exc:
            return HTMLResponse(f"<p>{exc}</p>", 400)
        return RedirectResponse(url, status_code=302)

    # ---- personal access tokens and connected apps (org admins) ----
    @app.get("/api/orgs/{org_id}/tokens")
    def list_tokens(org_id: str, a: Access = Depends(authz.org("admin"))):
        clients: dict[str, str] = {}

        def client_name(cid: str | None) -> str:
            if not cid:
                return ""
            if cid not in clients:
                c = store.get_oauth_client(cid) or {}
                clients[cid] = c.get("client_name") or cid
            return clients[cid]

        out = []
        for t in store.list_tokens(org_id):
            if t["kind"] == "access":
                continue  # short-lived; represented by the app's refresh token
            out.append({**t, "client_name": client_name(t["client_id"]),
                        "access": "write" if auth.SCOPE_WRITE in t["scopes"] else "read"})
        return out

    @app.post("/api/orgs/{org_id}/tokens", status_code=201)
    def create_token(org_id: str, body: TokenIn, a: Access = Depends(authz.org("admin"))):
        requested = [auth.SCOPE_READ, auth.SCOPE_WRITE] if body.access == "write" else [auth.SCOPE_READ]
        scopes = cap_scopes(a.role, requested)
        if not scopes:
            raise HTTPException(403, "Your role cannot create tokens")
        secret, record = auth.create_pat(store, org_id, a.user, body.name, scopes, body.expires_in_days)
        return {"token": secret, **record}

    @app.delete("/api/orgs/{org_id}/tokens/{token_id}", status_code=204)
    def revoke_token(org_id: str, token_id: str, a: Access = Depends(authz.org("admin"))):
        t = store.get_token_by_id(token_id)
        if not t or t["org_id"] != org_id or not store.revoke_token_id(token_id):
            raise HTTPException(404, "token not found")
        if t["kind"] == "refresh":  # disconnecting an app also ends its current sessions
            for x in store.list_tokens(org_id, "access"):
                if x["client_id"] == t["client_id"] and x["user_id"] == t["user_id"]:
                    store.revoke_token_id(x["id"])

    @app.get("/api/mcp-info")
    def mcp_info():
        return {"url": auth.MCP_URL, "issuer": auth.PUBLIC_URL, "scopes": auth.ALL_SCOPES}

    # Mounted last so every API route above takes precedence. Serves /mcp, the OAuth endpoints
    # (/authorize, /token, /register, /revoke) and the discovery documents under /.well-known/.
    app.mount("/", mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True,
                                            transport_security=mcp._archflow_security))
    return app


app = create_app()
