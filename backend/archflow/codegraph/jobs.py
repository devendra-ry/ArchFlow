"""Background analysis jobs.

The API submits a job and returns at once; a small thread pool runs it: fetch the source (clone or local dir),
run ``python -m archflow.codegraph.worker`` in a subprocess with a timeout, validate the worker's output and store
it. Job status lives in ``scale.job_store()``: an in-memory registry, or Redis hashes with a TTL when
``ARCHFLOW_REDIS_URL`` is set, so any API instance answers status polls and ``cancel`` (checked between phases)
works from any instance. The analysis itself runs on the instance that accepted it.

Seam for a real queue: ``JobRunner.submit`` / ``JobRunner.get`` are the whole interface used by the API and MCP.
A production deployment can replace this class with one backed by a durable queue (Redis/RQ, Celery, SQS, a
``jobs`` table + worker processes) without touching the routes; a restart forgets running jobs here (their stored
graphs are unaffected).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .. import scale
from . import normalize, sources

log = logging.getLogger("archflow.codegraph")
JOB_TTL = scale.JOB_TTL


class JobCancelled(Exception):
    pass


def analysis_timeout() -> int:
    try:
        return int(os.environ.get("ARCHFLOW_CODEGRAPH_TIMEOUT", "") or 600)
    except ValueError:
        return 600


def run_worker(repo_dir: Path, workdir: Path) -> dict[str, Any]:
    """Analyse ``repo_dir`` in a separate process; returns the validated normalized graph."""
    out = workdir / "graph.json"
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    try:
        proc = subprocess.run([sys.executable, "-m", "archflow.codegraph.worker", str(repo_dir), str(out)],
                              cwd=str(workdir), env=env, capture_output=True, text=True, timeout=analysis_timeout())
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"analysis timed out after {analysis_timeout()} s") from exc
    if proc.returncode != 0 or not out.exists():
        msg = (proc.stderr or "").strip().splitlines()
        raise RuntimeError(msg[-1][:500] if msg else f"analysis worker failed (exit {proc.returncode})")
    return normalize.validate(json.loads(out.read_text(encoding="utf-8")))


class JobRunner:
    def __init__(self, store: Any, max_workers: int | None = None,
                 worker: Callable[[Path, Path], dict[str, Any]] = run_worker, jobs: scale.JobStore | None = None):
        self.store = store
        self.worker = worker
        n = max_workers or int(os.environ.get("ARCHFLOW_CODEGRAPH_JOBS", "1") or 1)
        self.pool = ThreadPoolExecutor(max_workers=max(1, n), thread_name_prefix="codegraph")
        self.jobs: scale.JobStore = jobs or scale.job_store()

    # ---- interface -------------------------------------------------------------------------------------
    def submit(self, pid: str, actor: str, *, repo_url: str | None = None, local_path: str | None = None,
               on_done: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
        """Validate the source synchronously (raises sources.SourceError) and queue the analysis."""
        if repo_url:
            sources.validate_repo_url(repo_url)  # fail fast; re-checked (and pinned) at clone time
            source = "git"
        elif local_path:
            local = sources.check_local_path(local_path)
            source = "local"
        else:
            raise sources.SourceError("give repo_url or local_path")
        job = {"id": scale.new_job_id("job"), "kind": "codegraph", "project_id": pid, "status": "queued", "phase": "queued",
               "source": source, "repo_url": repo_url or None, "local_path": str(local) if local_path else None,
               "created_by": actor, "created_at": time.time(), "finished_at": None, "error": None,
               "code_graph_id": None}
        self.jobs.create(job["id"], job)
        self.pool.submit(self._run, dict(job), on_done)
        return self.public(job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        return self.public(job) if job else None

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        """Ask the job to stop (from any instance); it stops before its next phase."""
        job = self.jobs.cancel(job_id)
        return self.public(job) if job else None

    @staticmethod
    def public(job: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in job.items() if k != "local_path"}

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)

    # ---- internals ---------------------------------------------------------------------------------------
    def _set(self, job_id: str, **fields: Any) -> None:
        self.jobs.update(job_id, **fields)

    def _check(self, job_id: str) -> None:
        if self.jobs.cancelled(job_id):
            raise JobCancelled()

    def _run(self, job: dict[str, Any], on_done: Callable[[dict[str, Any]], None] | None) -> None:
        job_id = job["id"]
        workdir = Path(tempfile.mkdtemp(prefix="archflow-codegraph-"))
        try:
            commit = ""
            self._check(job_id)
            if job["source"] == "git":
                self._set(job_id, status="running", phase="cloning")
                repo_dir = workdir / "repo"
                commit = sources.clone(job["repo_url"], repo_dir)
            else:
                repo_dir = Path(job["local_path"])
                commit = sources.local_commit(repo_dir)
            self._check(job_id)
            self._set(job_id, status="running", phase="analysing", commit_sha=commit or None)
            graph = self.worker(repo_dir, workdir)
            if job["source"] == "git":  # the clone is not needed any more: delete it before storing
                shutil.rmtree(repo_dir, ignore_errors=True)
            self._check(job_id)
            self._set(job_id, phase="storing")
            record = self.store.add_code_graph(
                job["project_id"], source=job["source"], repo_url=job["repo_url"],
                commit_sha=commit or None, created_by=job["created_by"], status="ready",
                summary=normalize.summarize(graph), graph=normalize.compress(graph))
            self._set(job_id, status="done", phase="done", code_graph_id=record["id"], finished_at=time.time())
            if on_done:
                try:
                    on_done(self.get(job_id) or {})
                except Exception:  # pragma: no cover
                    log.exception("codegraph on_done hook failed")
        except JobCancelled:
            log.info("codegraph job %s cancelled", job_id)
            self._set(job_id, status="cancelled", phase="cancelled", finished_at=time.time())
        except Exception as exc:
            log.info("codegraph job %s failed: %s", job_id, exc)
            self._set(job_id, status="failed", phase="failed", error=str(exc)[:500], finished_at=time.time())
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
