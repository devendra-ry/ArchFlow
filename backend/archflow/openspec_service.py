"""OpenSpec inside ArchFlow: spec-driven changes written by the LLM, driven by the real OpenSpec CLI, stored in the DB.

OpenSpec (https://github.com/Fission-AI/openspec, MIT) is used as an npm dependency (``backend/openspec``,
``npm ci --omit=dev``); its code is not copied. Every OpenSpec file of a project (``openspec/config.yaml``, the
living specs in ``openspec/specs/``, changes in ``openspec/changes/<id>/`` and ``openspec/changes/archive/``) is
stored in the ``openspec_files`` table; ``openspec_changes`` keeps each change's status and last validation.

A CLI call runs in a temporary workspace: the project's files are written out, ``openspec …`` runs there, and what
it changed is written back to the DB (``Workspace``). Telemetry and update checks are switched off.

Writing a change follows OpenSpec's own workflow (schema "spec-driven"):

1. ``openspec new change <id>``; ``openspec/config.yaml`` carries the project context (ArchFlow's project memory)
   and rules, which OpenSpec passes into every artifact's instructions.
2. Until ``openspec status`` reports every artifact done, take the next ready one (proposal → specs + design →
   tasks), fetch ``openspec instructions <artifact> --json`` (the instruction and template OpenSpec gives coding
   agents) and have the LLM write it from the ArchFlow design, the existing living specs and the artifacts it
   depends on.
3. ``openspec validate <id> --strict --json``; reported issues go back to the LLM to fix (MAX_REPAIRS rounds).

Without an LLM the change is derived from the model (change_spec.offline) and still validated by OpenSpec.
Without Node/OpenSpec the derived change is stored unvalidated (engine "archflow").
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from . import agents, change_spec, llm
from .models import ProjectState, without_deleted
from .vibe import resolve_scope

log = logging.getLogger("archflow.openspec")

TOOL_DIR = Path(__file__).resolve().parent.parent / "openspec"
CLI = TOOL_DIR / "node_modules" / "@fission-ai" / "openspec" / "bin" / "openspec.js"
TIMEOUT = int(os.environ.get("ARCHFLOW_OPENSPEC_TIMEOUT", "90"))
MAX_REPAIRS = 2
MAX_FILE_CHARS = 200_000
ENV = {"OPENSPEC_TELEMETRY": "0", "DO_NOT_TRACK": "1", "OPENSPEC_NO_UPDATE_CHECK": "1", "CI": "1", "NO_COLOR": "1",
       "FORCE_COLOR": "0"}
CHANGE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")
SAFE_PATH_RE = re.compile(r"^openspec/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+$")


class OpenSpecError(RuntimeError):
    pass


def node_binary() -> str | None:
    return shutil.which(os.environ.get("ARCHFLOW_NODE", "node"))


def available() -> bool:
    return CLI.is_file() and node_binary() is not None


def safe_path(path: str) -> str:
    """Validate a stored path: under openspec/, no traversal, plain segments only."""
    p = (path or "").replace("\\", "/").strip()
    if not SAFE_PATH_RE.match(p) or any(seg in ("", ".", "..") for seg in p.split("/")):
        raise ValueError(f"invalid OpenSpec path: {path!r}")
    return p


# --------------------------------------------------------------------------- workspace
class Workspace:
    """The project's OpenSpec tree materialized in a temporary directory, for running the CLI."""

    def __init__(self, store: Any, pid: str):
        self.store, self.pid = store, pid
        self._tmp = tempfile.TemporaryDirectory(prefix="archflow-openspec-")
        self.root = Path(self._tmp.name)
        self.before: dict[str, str] = {}

    def __enter__(self) -> "Workspace":
        for f in self.store.list_openspec_files(self.pid):
            target = self.root / safe_path(f["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f["content"], encoding="utf-8")
            self.before[f["path"]] = f["content"]
        if not (self.root / "openspec" / "config.yaml").is_file():
            self.run("init", ".", "--tools", "none", json_out=False)
        return self

    def __exit__(self, *exc: Any) -> None:
        self._tmp.cleanup()

    def run(self, *args: str, json_out: bool = True, ok_codes: tuple[int, ...] = (0,)) -> Any:
        node = node_binary()
        if not CLI.is_file() or node is None:
            raise OpenSpecError("OpenSpec is not installed on the API server (cd backend/openspec && npm ci --omit=dev).")
        cmd = [node, str(CLI), *args] + (["--json"] if json_out else [])
        try:
            proc = subprocess.run(cmd, cwd=self.root, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=TIMEOUT, env={**os.environ, **ENV})
        except subprocess.TimeoutExpired as exc:
            raise OpenSpecError(f"openspec {args[0]} took longer than {TIMEOUT}s") from exc
        if json_out:
            try:
                data = json.loads(proc.stdout) if proc.stdout.strip() else None
            except json.JSONDecodeError:
                data = None
            if data is not None and (proc.returncode in ok_codes or args[0] == "validate"):
                return data
        if proc.returncode not in ok_codes:
            raise OpenSpecError((proc.stderr or proc.stdout).strip()[:1500] or f"openspec {args[0]} failed")
        return proc.stdout

    def path(self, rel: str) -> Path:
        return self.root / safe_path(rel)

    def write(self, rel: str, content: str) -> None:
        target = self.path(rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    def read(self, rel: str) -> str:
        p = self.path(rel)
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def snapshot(self) -> dict[str, str]:
        out = {}
        base = self.root / "openspec"
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.name != ".gitkeep" and p.stat().st_size <= MAX_FILE_CHARS:
                out[p.relative_to(self.root).as_posix()] = p.read_text(encoding="utf-8", errors="replace")
        return out

    def save(self, actor: str) -> dict[str, str]:
        """Write what changed in the workspace back to the DB; returns the new snapshot."""
        after = self.snapshot()
        changed = {k: v for k, v in after.items() if self.before.get(k) != v}
        removed = [k for k in self.before if k not in after]
        if changed or removed:
            self.store.write_openspec_files(self.pid, changed, actor, delete=removed)
        self.before = after
        return after

    def change_files(self, change_id: str) -> dict[str, str]:
        prefix = f"openspec/changes/{change_id}/"
        return {k[len(prefix):]: v for k, v in self.snapshot().items() if k.startswith(prefix) and not k.endswith(".openspec.yaml")}


# --------------------------------------------------------------------------- prompts
# Prompts: prompts/openspec.name.yaml, openspec.artifact.yaml, openspec.repair.yaml (versioned).
_FILES_SCHEMA = {"type": "object", "properties": {"files": {"type": "array", "items": {
    "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
    "required": ["path", "content"]}}}, "required": ["files"]}
_NAME_SCHEMA = {"type": "object", "properties": {"change_id": {"type": "string"}, "title": {"type": "string"}},
                "required": ["change_id", "title"]}
_FENCE = re.compile(r"^\s*```(?:markdown|md)?\s*\n(.*?)\n```\s*$", re.S)


def _strip(text: str) -> str:
    text = (text or "").strip()
    m = _FENCE.match(text)
    return (m.group(1) if m else text).strip() + "\n"


def living_specs(files: dict[str, str]) -> str:
    """The project's existing capability specs (what `openspec list --specs` + `show` would give an agent)."""
    specs = {k: v for k, v in files.items() if k.startswith("openspec/specs/") and k.endswith("/spec.md")}
    if not specs:
        return "The project has no living specs yet: every capability in this change is new."
    parts = [f"<spec path=\"{k[len('openspec/specs/'):-len('/spec.md')]}\">\n{v.strip()[:12000]}\n</spec>" for k, v in specs.items()]
    return "Existing living specs (use these exact capability paths when a change modifies them):\n" + "\n".join(parts)


def project_config(memory: str, extra: str) -> str:
    """openspec/config.yaml: OpenSpec passes `context` and `rules` into every artifact's instructions."""
    ctx_lines = ["This project is designed in ArchFlow; its approved architecture and requirements are the source of truth."]
    if memory:
        facts = [ln.strip()[2:] for ln in memory.splitlines() if ln.strip().startswith("- ")]
        if facts:
            ctx_lines.append("Settled project facts:")
            ctx_lines += [f"- {f}" for f in facts]
    if extra.strip():
        ctx_lines.append("Team instructions: " + " ".join(extra.split()))
    body = "\n".join("  " + ln for ln in ctx_lines)
    return ("schema: spec-driven\n\n# Written by ArchFlow from the project memory; edits here are overwritten when "
            "a change is generated.\ncontext: |\n" + body + "\n\nrules:\n  tasks:\n"
            "    - Include a test task for every requirement ID in scope, named after the requirement\n"
            "    - Keep each task small and verifiable\n")


# --------------------------------------------------------------------------- generation
Progress = Callable[[str], None]


def _unique_id(store: Any, pid: str, base: str) -> str:
    base = base if CHANGE_ID_RE.match(base) else "implement-design"
    taken = {c["change_id"] for c in store.list_openspec_changes(pid)}
    cid, n = base, 2
    while cid in taken:
        cid, n = f"{base[:58]}-{n}", n + 1
    return cid


def _name_change(context: str, scope_title: str, org_id: str | None, use_llm: bool) -> tuple[str, str]:
    if use_llm:
        try:
            data = agents.run_json("openspec.name", _NAME_SCHEMA, org_id=org_id, variables={"context": context[:20000]})
            cid = change_spec.slug(data.get("change_id", ""), 60)
            if CHANGE_ID_RE.match(cid):
                return cid, change_spec._clip(data.get("title") or scope_title, 100)
        except (llm.LLMUnavailable, llm.LLMRefused) as exc:
            log.info("naming the change via LLM failed: %s", type(exc).__name__)
    return change_spec.slug(scope_title, 60), scope_title


def _write_artifact(ws: Workspace, change_id: str, artifact: str, design_context: str, specs_context: str,
                    org_id: str | None) -> None:
    info = ws.run("instructions", artifact, "--change", change_id)
    deps = []
    written = ws.change_files(change_id)
    for d in info.get("dependencies") or []:
        pattern = d.get("path") or ""
        for rel, text in written.items():
            if rel == pattern or ("*" in pattern and rel.startswith(pattern.split("*")[0])):
                deps.append(f"<artifact path=\"{rel}\">\n{text}\n</artifact>")
    variables = {"artifact": artifact, "instruction": info["instruction"], "project_context": info.get("context") or "",
                 "rules": list(info.get("rules") or [])}
    user = "\n\n".join(x for x in [
        design_context, specs_context,
        ("Artifacts already written for this change:\n" + "\n".join(deps)) if deps else "",
        f"Template for {info.get('outputPath')}:\n{info.get('template', '')}",
        f"Write `{info.get('outputPath')}` for change `{change_id}` now."] if x)
    if artifact == "specs":
        data = agents.run_json("openspec.artifact", _FILES_SCHEMA, variables=variables, user=user, org_id=org_id)
        for f in data.get("files") or []:
            rel = f.get("path", "").replace("\\", "/").lstrip("/")
            if rel.startswith("specs/") and rel.endswith("spec.md") and f.get("content", "").strip():
                ws.write(f"openspec/changes/{change_id}/{rel}", _strip(f["content"]))
    else:
        text = agents.run_text("openspec.artifact", variables=variables, user=user, org_id=org_id)
        if not text.strip():
            raise llm.LLMUnavailable(f"the model returned an empty {artifact}")
        ws.write(f"openspec/changes/{change_id}/{info.get('outputPath') or artifact + '.md'}", _strip(text))


def validate(ws: Workspace, change_id: str) -> dict[str, Any]:
    data = ws.run("validate", change_id, "--strict")
    item = (data.get("items") or [{}])[0]
    return {"valid": bool(item.get("valid")), "issues": item.get("issues") or [], "strict": True}


def _repair(ws: Workspace, change_id: str, result: dict[str, Any], org_id: str | None) -> bool:
    files = ws.change_files(change_id)
    issues = "\n".join(f"- [{i.get('level')}] {i.get('path', '')}: {i.get('message')}" for i in result["issues"])
    body = "\n".join(f"<file path=\"{k}\">\n{v}\n</file>" for k, v in files.items())
    data = agents.run_json("openspec.repair", _FILES_SCHEMA, org_id=org_id, variables={"issues": issues, "files": body})
    changed = False
    for f in data.get("files") or []:
        rel = f.get("path", "").replace("\\", "/").lstrip("/")
        if rel in files and f.get("content", "").strip() and _strip(f["content"]) != files[rel]:
            ws.write(f"openspec/changes/{change_id}/{rel}", _strip(f["content"]))
            changed = True
    return changed


def _derived_files(state: ProjectState, project_name: str, node_ids: list[str], requirement_ids: list[str],
                   decisions: list[dict[str, Any]] | None, target: str) -> change_spec.ChangeSpec:
    return change_spec.generate(state, project_name, node_ids, requirement_ids, target=target, decisions=decisions,
                                use_llm=False)


def generate(store: Any, pid: str, state: ProjectState, project_name: str, node_ids: list[str],
             requirement_ids: list[str], *, actor: str, branch: str = "main", target: str = "claude-code",
             extra: str = "", memory: str = "", decisions: list[dict[str, Any]] | None = None,
             org_id: str | None = None, use_llm: bool = True, progress: Progress = lambda s: None) -> dict[str, Any]:
    """Write, validate and store a new change; returns its metadata (see the module docstring)."""
    state = without_deleted(state)
    scope = resolve_scope(state, node_ids, requirement_ids)
    if not scope.node_ids and not scope.requirement_ids:
        raise ValueError("There is nothing to specify yet: add components or requirements first.")
    use_llm = use_llm and llm.available(org_id)
    scope_meta = {"node_ids": scope.node_ids if not scope.whole_system else [], "requirement_ids": requirement_ids,
                  "whole_system": scope.whole_system, "target": target, "extra": extra}
    context = change_spec.build_context(state, project_name, scope, memory=memory, decisions=decisions, target=target,
                                        extra=extra)
    derived = None
    if not use_llm:
        derived = _derived_files(state, project_name, node_ids, requirement_ids, decisions, target)
    progress("Naming the change")
    base, title = _name_change(context, derived.title if derived else change_spec.offline_change(
        state, project_name, scope, decisions)["title"], org_id, use_llm)
    change_id = _unique_id(store, pid, base)

    if not available():
        derived = derived or _derived_files(state, project_name, node_ids, requirement_ids, decisions, target)
        files = {f"openspec/changes/{change_id}/{k}": v for k, v in derived.files.items()}
        store.write_openspec_files(pid, files, actor)
        return store.save_openspec_change(pid, change_id, {
            "title": title, "status": "draft", "engine": "archflow", "branch": branch, "scope": scope_meta,
            "validation": {"skipped": "OpenSpec is not installed on the API server; the change was not validated."}}, actor)

    with Workspace(store, pid) as ws:
        ws.write("openspec/config.yaml", project_config(memory, extra))
        ws.run("new", "change", change_id)
        engine = "openspec+llm" if use_llm else "openspec"
        notes: list[str] = []
        if use_llm:
            design_context = f"# Approved design (from ArchFlow)\n{context}"
            specs_context = living_specs(ws.snapshot())
            try:
                for _ in range(8):  # follow OpenSpec's artifact graph until every artifact is written
                    status = ws.run("status", "--change", change_id)
                    ready = [a["id"] for a in status.get("artifacts") or [] if a.get("status") == "ready"]
                    if not ready:
                        break
                    for artifact in ready:
                        progress(f"Writing {artifact}")
                        _write_artifact(ws, change_id, artifact, design_context, specs_context, org_id)
            except (llm.LLMUnavailable, llm.LLMRefused) as exc:
                log.warning("LLM failed while writing change %s: %s", change_id, exc)
                notes.append(f"The AI stopped part-way ({exc}); missing parts were derived from the design.")
                use_llm, engine = False, "openspec"
        if not use_llm:  # fill whatever is missing from the derived change
            derived = derived or _derived_files(state, project_name, node_ids, requirement_ids, decisions, target)
            have = ws.change_files(change_id)
            for rel, text in derived.files.items():
                if rel not in have and not (rel.startswith("specs/") and any(h.startswith("specs/") for h in have)):
                    ws.write(f"openspec/changes/{change_id}/{rel}", text)
        progress("Validating with OpenSpec")
        result = validate(ws, change_id)
        rounds = 0
        while not result["valid"] and use_llm and rounds < MAX_REPAIRS:
            rounds += 1
            progress(f"Fixing {len(result['issues'])} validation issue(s)")
            try:
                if not _repair(ws, change_id, result, org_id):
                    break
            except (llm.LLMUnavailable, llm.LLMRefused) as exc:
                notes.append(f"Automatic repair failed: {exc}")
                break
            result = validate(ws, change_id)
        result.update({"repairs": rounds, "notes": notes})
        ws.save(actor)
    return store.save_openspec_change(pid, change_id, {
        "title": title, "status": "valid" if result["valid"] else "invalid", "engine": engine, "branch": branch,
        "scope": scope_meta, "validation": result}, actor)


# --------------------------------------------------------------------------- lifecycle
def revalidate(store: Any, pid: str, change_id: str, actor: str) -> dict[str, Any]:
    with Workspace(store, pid) as ws:
        result = validate(ws, change_id)
    return store.save_openspec_change(pid, change_id, {"status": "valid" if result["valid"] else "invalid",
                                                       "validation": result}, actor)


def archive(store: Any, pid: str, change_id: str, actor: str) -> dict[str, Any]:
    """`openspec archive`: merge the change's spec deltas into the living specs and move it to changes/archive/."""
    with Workspace(store, pid) as ws:
        data = ws.run("archive", change_id, "--yes")
        ws.save(actor)
    info = data.get("archive") or {}
    return store.save_openspec_change(pid, change_id, {"status": "archived", "archived_as": info.get("archivedAs"),
                                                       "validation": {"archive": info}}, actor)


def change_dir(store: Any, pid: str, change: dict[str, Any]) -> str:
    if change["status"] == "archived" and change.get("archived_as"):
        return f"openspec/changes/archive/{change['archived_as']}/"
    return f"openspec/changes/{change['change_id']}/"


def change_files(store: Any, pid: str, change: dict[str, Any]) -> dict[str, str]:
    prefix = change_dir(store, pid, change)
    return {f["path"][len(prefix):]: f["content"] for f in store.list_openspec_files(pid, prefix)
            if not f["path"].endswith(".openspec.yaml")}


def zip_tree(store: Any, pid: str, extra: dict[str, str] | None = None) -> bytes:
    """The project's openspec/ tree, plus ``extra`` files at the root (the coding-agent guides)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in store.list_openspec_files(pid):
            z.writestr(f["path"], f["content"])
        for path, content in (extra or {}).items():
            z.writestr(path, content)
    return buf.getvalue()


def guide_files(store: Any, pid: str) -> dict[str, str]:
    """CLAUDE.md, AGENTS.md and .cursorrules derived from the main branch (no AI: the download stays instant)."""
    from . import exports

    try:
        return exports.guide_files(exports.collect(store, pid, "main"), use_llm=False)["files"]
    except KeyError:  # no main branch
        return {}


# --------------------------------------------------------------------------- REST
JOB_TTL = 3600


class NewChangeIn(BaseModel):
    node_ids: list[str] = []
    requirement_ids: list[str] = []
    target: str = "claude-code"
    extra: str = Field("", max_length=4000)
    use_ai: bool = True


class FileIn(BaseModel):
    path: str = Field(min_length=1, max_length=300)  # relative to the change directory, e.g. specs/x/spec.md
    content: str = Field(max_length=MAX_FILE_CHARS)


def register_routes(app: FastAPI, store: Any, *, authz: Any, project_context: Callable[..., dict[str, Any]]) -> None:
    """``project_context(pid, branch)`` → {state, name, memory, decisions} for generation. Reading needs any role;
    writing, editing, validating, archiving and deleting changes need developer or above."""
    read, edit = authz.project("read"), authz.project("edit")
    jobs: dict[str, dict[str, Any]] = {}
    running: set[asyncio.Task] = set()

    def change_or_404(pid: str, cid: str) -> dict[str, Any]:
        c = store.get_openspec_change(pid, cid)
        if not c:
            raise HTTPException(404, "change not found")
        return c

    def guard(fn, *args, **kw):
        try:
            return fn(*args, **kw)
        except OpenSpecError as exc:
            raise HTTPException(503 if "not installed" in str(exc) else 422, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/projects/{pid}/openspec")
    def overview(pid: str, a=Depends(read)):
        specs = [{"capability": f["path"][len("openspec/specs/"):-len("/spec.md")], "path": f["path"],
                  "updated_at": f["updated_at"], "updated_by": f["updated_by"]}
                 for f in store.list_openspec_files(pid, "openspec/specs/", with_content=False)
                 if f["path"].endswith("/spec.md")]
        return {"available": available(), "ai": llm.available(a.org_id), "changes": store.list_openspec_changes(pid),
                "specs": specs}

    @app.post("/api/projects/{pid}/openspec/changes", status_code=202)
    async def new_change(pid: str, body: NewChangeIn, branch: str = "main", a=Depends(edit)):
        """Start writing a change (1-2 minutes with an LLM); poll the returned job."""
        ctx = project_context(pid, branch)
        for j in list(jobs.values()):  # forget finished jobs after an hour
            if j["status"] != "running" and time.time() - j["started"] > JOB_TTL:
                jobs.pop(j["id"], None)
        job: dict[str, Any] = {"id": secrets.token_hex(6), "project_id": pid, "status": "running", "step": "Starting",
                               "steps": [], "change_id": None, "error": None, "started": time.time()}
        jobs[job["id"]] = job

        def progress(step: str) -> None:
            job["step"] = step
            job["steps"].append({"step": step, "at": round(time.time() - job["started"], 1)})

        def work() -> None:
            try:
                change = generate(store, pid, ctx["state"], ctx["name"], body.node_ids, body.requirement_ids,
                                  actor=a.actor, branch=branch, target=body.target, extra=body.extra,
                                  memory=ctx["memory"], decisions=ctx["decisions"], org_id=a.org_id,
                                  use_llm=body.use_ai, progress=progress)
                job.update(status="done", change_id=change["change_id"], step="Done")
            except (OpenSpecError, ValueError) as exc:
                job.update(status="failed", error=str(exc))
            except Exception:  # noqa: BLE001 - reported to the user, details logged
                log.exception("writing an OpenSpec change failed")
                job.update(status="failed", error="Something went wrong while writing the change.")

        task = asyncio.ensure_future(asyncio.to_thread(work))
        running.add(task)
        task.add_done_callback(running.discard)
        return {k: v for k, v in job.items() if k != "project_id"}

    @app.get("/api/projects/{pid}/openspec/jobs/{job_id}")
    def job_status(pid: str, job_id: str, a=Depends(read)):
        job = jobs.get(job_id)
        if not job or job["project_id"] != pid:
            raise HTTPException(404, "job not found")
        return {**{k: v for k, v in job.items() if k != "project_id"}, "elapsed": round(time.time() - job["started"])}

    @app.get("/api/projects/{pid}/openspec/changes/{cid}")
    def get_change(pid: str, cid: str, target: str = "claude-code", a=Depends(read)):
        c = change_or_404(pid, cid)
        files = change_files(store, pid, c)
        return {"change": c, "dir": change_dir(store, pid, c), "files": files,
                "prompt": change_spec.agent_prompt(cid, files, target)}

    @app.put("/api/projects/{pid}/openspec/changes/{cid}/files")
    def save_file(pid: str, cid: str, body: FileIn, a=Depends(edit)):
        """Edit (or add) one file of an open change, then validate the change again."""
        c = change_or_404(pid, cid)
        if c["status"] == "archived":
            raise HTTPException(409, "Archived changes are read-only.")
        rel = body.path.replace("\\", "/").lstrip("/")
        if rel == ".openspec.yaml":
            raise HTTPException(422, "That file is managed by OpenSpec.")
        path = guard(safe_path, f"openspec/changes/{cid}/{rel}")
        store.write_openspec_files(pid, {path: body.content.replace("\r\n", "\n")}, a.actor)
        if not available():
            return store.save_openspec_change(pid, cid, {"status": "draft"}, a.actor)
        return guard(revalidate, store, pid, cid, a.actor)

    @app.post("/api/projects/{pid}/openspec/changes/{cid}/validate")
    def validate_change(pid: str, cid: str, a=Depends(edit)):
        if change_or_404(pid, cid)["status"] == "archived":
            raise HTTPException(409, "Archived changes are not validated again.")
        return guard(revalidate, store, pid, cid, a.actor)

    @app.post("/api/projects/{pid}/openspec/changes/{cid}/archive")
    def archive_change(pid: str, cid: str, a=Depends(edit)):
        """Merge the change into the living specs (`openspec archive`); OpenSpec validates it first."""
        if change_or_404(pid, cid)["status"] == "archived":
            raise HTTPException(409, "Already archived.")
        return guard(archive, store, pid, cid, a.actor)

    @app.delete("/api/projects/{pid}/openspec/changes/{cid}", status_code=204)
    def delete_change(pid: str, cid: str, a=Depends(edit)):
        if change_or_404(pid, cid)["status"] == "archived":
            raise HTTPException(409, "Archived changes are part of the spec history and cannot be deleted.")
        store.delete_openspec_change(pid, cid)

    @app.get("/api/projects/{pid}/openspec/file")
    def get_file(pid: str, path: str, a=Depends(read)):
        f = store.get_openspec_file(pid, guard(safe_path, path))
        if not f:
            raise HTTPException(404, "file not found")
        return f

    @app.get("/api/projects/{pid}/openspec.zip")
    def download(pid: str, a=Depends(read)):
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", a.project["name"]).strip("-") or "project"
        return Response(zip_tree(store, pid, guide_files(store, pid)), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}-openspec.zip"'})
