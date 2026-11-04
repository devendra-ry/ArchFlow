"""Repository-analysis service: REST routes (``register_routes``) and the functions the MCP tools share.

Routes (all need a signed-in member of the project's org; analysing, uploading and reconstructing need
developer or above, see authz.py):

=======  ==========================================  =============================================================
POST     /api/projects/{pid}/codegraph/analyze       {repo_url | local_path} → job (503 without the codegraph extra)
GET      /api/projects/{pid}/codegraph/jobs/{id}     job status (queued | running | done | failed | cancelled)
POST     /api/projects/{pid}/codegraph/jobs/{id}/cancel  stop the job before its next phase (any API instance)
POST     /api/projects/{pid}/codegraph/upload        normalized graph JSON from ``archflow-codegraph`` (size-limited)
GET      /api/projects/{pid}/codegraph               analyses (newest first) + the latest summary
GET      /api/projects/{pid}/codegraph/drift         ?branch= designed-vs-code drift issues
POST     /api/projects/{pid}/codegraph/impact        {files: [...], branch?, depth?} → symbols → components → reqs
GET      /api/projects/{pid}/codegraph/{id}          one analysis (``?full=true`` adds the normalized graph)
POST     /api/projects/{pid}/codegraph/{id}/reconstruct  → new branch draft/from-code-<sha>, committed as a version
=======  ==========================================  =============================================================

Only stored, normalized graphs are used after analysis, so upload/reconstruct/drift/impact work without Graphify;
only analysing a repository needs it.
"""

from __future__ import annotations

import asyncio
import copy
import json
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from .. import scale
from ..models import ProjectState
from . import INSTALL_HINT, available, normalize, sources
from . import drift as drift_mod
from . import impact as impact_mod
from . import reconstruct as recon_mod
from .jobs import JobRunner

CLI_HINT = "uv sync --extra codegraph && uv run archflow-codegraph <folder> -o archflow-codegraph.json"


class CodeGraphError(Exception):
    status = 400


class Unavailable(CodeGraphError):
    status = 503


class NotFound(CodeGraphError):
    status = 404


class Invalid(CodeGraphError):
    status = 422


class Conflict(CodeGraphError):
    status = 409


class AnalyzeIn(BaseModel):
    repo_url: str | None = Field(None, max_length=500)
    local_path: str | None = Field(None, max_length=1000)


class ImpactIn(BaseModel):
    files: list[str] = Field(min_length=1, max_length=2000)
    branch: str = "main"
    depth: int = Field(2, ge=1, le=6)


class ReconstructIn(BaseModel):
    refine: bool = True
    include_dev_tools: bool = False  # Adminer, Mailpit, Playwright, ... as components (default: listed only)
    branch_name: str | None = Field(None, max_length=60, pattern=r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")


class CodeGraphService:
    """Holds the job runner and a small cache of decoded graphs and their reconstructions (keyed by graph id;
    stored graphs are immutable, so entries never go stale)."""

    CACHE_SIZE = 4

    def __init__(self, store: Any, commit_ops: Callable[..., Awaitable[Any]],
                 commit_version: Callable[..., Awaitable[dict[str, Any]]], runner: JobRunner | None = None):
        self.store = store
        self.commit_ops = commit_ops
        self.commit_version = commit_version
        self._runner = runner
        self._cache: OrderedDict[str, tuple[dict, dict, ProjectState]] = OrderedDict()
        self._lock = threading.Lock()

    @property
    def runner(self) -> JobRunner:
        if self._runner is None:
            self._runner = JobRunner(self.store)
        return self._runner

    # ---- reading ---------------------------------------------------------------------------------------
    def record(self, pid: str, gid: str | None = None) -> dict[str, Any]:
        rec = self.store.get_code_graph(pid, gid) if gid else self.store.latest_code_graph(pid)
        if not rec:
            raise NotFound("code graph not found" if gid else "no code analysis yet: analyse or upload a repository first")
        return rec

    def loaded(self, pid: str, gid: str | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], ProjectState]:
        """(record, graph, reconstruction, reconstruction as state) — cached per graph id."""
        rec = self.record(pid, gid)
        with self._lock:
            hit = self._cache.get(rec["id"])
            if hit:
                self._cache.move_to_end(rec["id"])
                return (rec, *hit)
        full = self.store.get_code_graph(pid, rec["id"], with_graph=True)
        graph = normalize.decompress(full.get("graph")) or {}
        recon = recon_mod.reconstruct(graph, repo_url=rec.get("repo_url"), commit_sha=rec.get("commit_sha"),
                                      code_graph_id=rec["id"])
        state = recon_mod.to_state(recon)
        with self._lock:
            self._cache[rec["id"]] = (graph, recon, state)
            while len(self._cache) > self.CACHE_SIZE:
                self._cache.popitem(last=False)
        return rec, graph, recon, state

    def code_state(self, pid: str) -> ProjectState | None:
        """The latest reconstruction for analysis/drift, or None when the project has no code graph."""
        if not self.store.latest_code_graph(pid):
            return None
        try:
            return self.loaded(pid)[3]
        except Exception:  # a corrupt graph must not break the analysis endpoint
            return None

    def overview(self, pid: str) -> dict[str, Any]:
        graphs = self.store.list_code_graphs(pid)
        return {"available": available(), "local_repos": sources.local_repos_allowed(),
                "latest": graphs[0] if graphs else None,
                "graphs": [{k: v for k, v in g.items() if k != "summary"} for g in graphs],
                "cli": CLI_HINT, "install_hint": None if available() else INSTALL_HINT}

    # ---- creating --------------------------------------------------------------------------------------
    def analyze(self, pid: str, actor: str, repo_url: str | None = None, local_path: str | None = None) -> dict[str, Any]:
        if not available():
            raise Unavailable(INSTALL_HINT)
        if bool(repo_url) == bool(local_path):
            raise Invalid("give exactly one of repo_url or local_path")
        try:
            return self.runner.submit(pid, actor, repo_url=repo_url, local_path=local_path)
        except sources.SourceError as exc:
            raise Invalid(str(exc)) from exc

    def _jobs(self) -> Any:
        # Without a local runner the job may still be running on another instance (shared store with Redis).
        return self._runner.jobs if self._runner is not None else scale.job_store()

    def job(self, pid: str, job_id: str) -> dict[str, Any]:
        job = self._jobs().get(job_id)
        if not job or job.get("project_id") != pid:
            raise NotFound("job not found")
        return JobRunner.public(job)

    def cancel_job(self, pid: str, job_id: str) -> dict[str, Any]:
        self.job(pid, job_id)
        return JobRunner.public(self._jobs().cancel(job_id) or {})

    def upload(self, pid: str, actor: str, raw: bytes, repo_url: str | None = None,
               commit_sha: str | None = None) -> dict[str, Any]:
        if len(raw) > normalize.max_upload_bytes():
            raise Invalid(f"graph is larger than {normalize.max_upload_bytes() // (1024 * 1024)} MB")
        try:
            graph = normalize.validate(json.loads(raw.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise Invalid("upload must be the JSON file written by archflow-codegraph") from exc
        except ValueError as exc:
            raise Invalid(str(exc)) from exc
        if repo_url and not re.fullmatch(r"https://[A-Za-z0-9.\-]+(?::443)?/[A-Za-z0-9._~/\-]+", repo_url):
            raise Invalid("repo_url must be an https URL")
        if commit_sha and not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha):
            raise Invalid("commit_sha must be a hex commit id")
        return self.store.add_code_graph(pid, source="upload", repo_url=repo_url or None, commit_sha=commit_sha or None,
                                         created_by=actor, status="ready", summary=normalize.summarize(graph),
                                         graph=normalize.compress(graph))

    # ---- reconstruction --------------------------------------------------------------------------------
    def _branch_name(self, pid: str, rec: dict[str, Any], wanted: str | None) -> str:
        if wanted:
            if self.store.get_state(pid, wanted) is not None:
                raise Conflict(f"branch '{wanted}' already exists")
            return wanted
        suffix = (rec.get("commit_sha") or "")[:7] or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        base = f"draft/from-code-{suffix}"
        name, i = base, 2
        while self.store.get_state(pid, name) is not None:
            name, i = f"{base}-{i}", i + 1
        return name

    async def reconstruct_branch(self, pid: str, actor: str, gid: str | None = None, *, org_id: str | None = None,
                                 refine: bool = True, branch_name: str | None = None,
                                 include_dev_tools: bool = False) -> dict[str, Any]:
        rec, graph, recon, code_state = await asyncio.to_thread(self.loaded, pid, gid)
        if include_dev_tools:  # not cached: the default reconstruction is the one drift and impact use
            recon = await asyncio.to_thread(recon_mod.reconstruct, graph, repo_url=rec.get("repo_url"),
                                            commit_sha=rec.get("commit_sha"), code_graph_id=rec["id"],
                                            include_dev_tools=True)
        recon = copy.deepcopy(recon)
        engine = await asyncio.to_thread(recon_mod.refine, recon, graph, org_id) if refine else "offline"
        main = self.store.get_state(pid, "main") or ProjectState()
        # Requirements are kept; components matched to a designed one inherit its requirement links.
        matches = drift_mod.match(main, code_state)
        inherit: dict[str, list[str]] = {}
        for did, (cid, _) in matches.items():
            inherit.setdefault(cid, [])
            for r in main.nodes[did].requirement_ids:
                if r not in inherit[cid]:
                    inherit[cid].append(r)
        branch = self._branch_name(pid, rec, branch_name)
        self.store.create_branch(pid, branch, main, self.store.head_version(pid, "main"))
        pub = recon_mod.public(recon)
        ops: list[dict[str, Any]] = [{"op": "remove_node", "id": n.id} for n in main.nodes.values()
                                     if not n.parent_id or n.parent_id not in main.nodes]
        for n in pub["nodes"]:
            ops.append({"op": "add_node", "node": {**n, "requirement_ids": inherit.get(n["id"], [])}})
        ops += [{"op": "add_edge", "edge": e} for e in pub["edges"]]
        new_state, summary = await self.commit_ops(pid, branch, ops, f"{actor} (reconstructed from code)")
        where = rec.get("repo_url") or rec.get("summary", {}).get("root_name") or "uploaded graph"
        version = await self.commit_version(pid, branch, f"Reconstructed from {where}@{(rec.get('commit_sha') or 'unknown')[:12]}",
                                            actor)
        return {"branch": branch, "version": version["number"], "code_graph_id": rec["id"], "engine": engine,
                "components": len(pub["nodes"]), "connections": len(pub["edges"]),
                "dev_tools": pub["dev_tools"], "excluded": pub["excluded"],
                "requirements_linked": sum(1 for v in inherit.values() if v), "rev": new_state.rev}

    # ---- drift / impact --------------------------------------------------------------------------------
    def drift(self, pid: str, branch: str = "main") -> dict[str, Any]:
        designed = self.store.get_state(pid, branch)
        if designed is None:
            raise NotFound(f"branch '{branch}' not found")
        rec, _, _, code_state = self.loaded(pid)
        out = drift_mod.drift(designed, code_state)
        return {"branch": branch, "code_graph_id": rec["id"], "repo_url": rec.get("repo_url"),
                "commit_sha": rec.get("commit_sha"), **out}

    def impact(self, pid: str, files: list[str], branch: str = "main", depth: int = 2) -> dict[str, Any]:
        designed = self.store.get_state(pid, branch)
        if designed is None:
            raise NotFound(f"branch '{branch}' not found")
        rec, graph, recon, code_state = self.loaded(pid)
        out = impact_mod.impact(graph, recon, code_state, designed, files, depth)
        return {"branch": branch, "code_graph_id": rec["id"], **out}


# --------------------------------------------------------------------------- routes
def register_routes(app: FastAPI, svc: CodeGraphService, *, authz: Any) -> None:
    read, edit = authz.project("read"), authz.project("edit")

    def run(fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        try:
            return fn(*args, **kw)
        except CodeGraphError as exc:
            raise HTTPException(exc.status, str(exc)) from exc

    @app.get("/api/projects/{pid}/codegraph")
    def codegraph_overview(pid: str, a=Depends(read)):
        return svc.overview(pid)

    @app.post("/api/projects/{pid}/codegraph/analyze", status_code=202)
    def codegraph_analyze(pid: str, body: AnalyzeIn, a=Depends(edit)):
        return run(svc.analyze, pid, a.actor, body.repo_url, body.local_path)

    @app.get("/api/projects/{pid}/codegraph/jobs/{job_id}")
    def codegraph_job(pid: str, job_id: str, a=Depends(read)):
        return run(svc.job, pid, job_id)

    @app.post("/api/projects/{pid}/codegraph/jobs/{job_id}/cancel")
    def codegraph_job_cancel(pid: str, job_id: str, a=Depends(edit)):
        return run(svc.cancel_job, pid, job_id)

    @app.post("/api/projects/{pid}/codegraph/upload", status_code=201)
    async def codegraph_upload(pid: str, request: Request, repo_url: str | None = None, commit_sha: str | None = None,
                               a=Depends(edit)):
        limit = normalize.max_upload_bytes()
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise HTTPException(413, f"graph is larger than {limit // (1024 * 1024)} MB")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > limit:
                raise HTTPException(413, f"graph is larger than {limit // (1024 * 1024)} MB")
            chunks.append(chunk)
        return await asyncio.to_thread(run, svc.upload, pid, a.actor, b"".join(chunks), repo_url, commit_sha)

    @app.get("/api/projects/{pid}/codegraph/drift")
    async def codegraph_drift(pid: str, branch: str = "main", a=Depends(read)):
        return await asyncio.to_thread(run, svc.drift, pid, branch)

    @app.post("/api/projects/{pid}/codegraph/impact")
    async def codegraph_impact(pid: str, body: ImpactIn, a=Depends(read)):
        return await asyncio.to_thread(run, svc.impact, pid, body.files, body.branch, body.depth)

    @app.get("/api/projects/{pid}/codegraph/{gid}")
    def codegraph_get(pid: str, gid: str, full: bool = False, a=Depends(read)):
        rec = run(svc.record, pid, gid)
        if full:
            rec = {**rec, "graph": run(svc.loaded, pid, gid)[1]}
        return rec

    @app.post("/api/projects/{pid}/codegraph/{gid}/reconstruct", status_code=201)
    async def codegraph_reconstruct(pid: str, gid: str, body: ReconstructIn | None = None, a=Depends(edit)):
        body = body or ReconstructIn()
        try:
            return await svc.reconstruct_branch(pid, a.actor, gid, org_id=a.org_id, refine=body.refine,
                                                branch_name=body.branch_name,
                                                include_dev_tools=body.include_dev_tools)
        except CodeGraphError as exc:
            raise HTTPException(exc.status, str(exc)) from exc
