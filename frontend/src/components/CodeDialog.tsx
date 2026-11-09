import { useCallback, useEffect, useRef, useState } from "react";
import { Icon, X } from "../icons";
import { api } from "../api";
import { useEscape } from "../useEscape";
import type { CodeJob, CodeOverview, DriftResult, ImpactResult, Issue } from "../types";

/** Browsable URL of a path in a GitHub/GitLab repository at a commit (null for other hosts). */
export function codeUrl(repo: string | null | undefined, sha: string | null | undefined, path = ""): string | null {
  const m = repo?.match(/^https:\/\/(github\.com|gitlab\.com)\/(.+?)(?:\.git)?\/?$/);
  if (!m) return null;
  const base = `https://${m[1]}/${m[2]}`;
  if (!path) return base;
  const ref = sha || "HEAD";
  const clean = path.split("/").map(encodeURIComponent).join("/");
  return m[1] === "gitlab.com" ? `${base}/-/tree/${ref}/${clean}` : `${base}/tree/${ref}/${clean}`;
}

interface Props {
  pid: string;
  branch: string;
  readOnly: boolean;
  onClose: () => void;
  onOpenBranch: (branch: string) => void;
  onFocus: (issue: Issue) => void;
  toast: (text: string) => void;
}

const PHASES: Record<string, string> = {
  queued: "Waiting for a worker…", cloning: "Cloning the repository (shallow)…", analysing: "Parsing code and building the graph…",
  storing: "Saving the analysis…",
};

export function CodeDialog({ pid, branch, readOnly, onClose, onOpenBranch, onFocus, toast }: Props) {
  const escRef = useEscape(onClose);
  const [overview, setOverview] = useState<CodeOverview | null>(null);
  const [url, setUrl] = useState("");
  const [job, setJob] = useState<CodeJob | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [drift, setDrift] = useState<DriftResult | null>(null);
  const [impactFiles, setImpactFiles] = useState("");
  const [impact, setImpact] = useState<ImpactResult | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(() => {
    api.codeOverview(pid).then((o) => {
      setOverview(o);
      if (o.latest) api.codeDrift(pid, branch).then(setDrift).catch(() => setDrift(null));
    }).catch((e) => setError(e.message));
  }, [pid, branch]);
  useEffect(refresh, [refresh]);

  // Poll the running job until it finishes.
  useEffect(() => {
    if (!job || job.status === "done" || job.status === "failed") return;
    const t = window.setTimeout(() => {
      api.codeJob(pid, job.id).then((j) => {
        setJob(j);
        if (j.status === "done") { toast("Repository analysed"); refresh(); }
        if (j.status === "failed") setError(j.error || "Analysis failed");
      }).catch((e) => setError(e.message));
    }, 1500);
    return () => window.clearTimeout(t);
  }, [job, pid, refresh, toast]);

  const analyze = async () => {
    setError("");
    try { setJob(await api.codeAnalyze(pid, url.trim())); } catch (e) { setError((e as Error).message); }
  };
  const uploadFile = async (file: File) => {
    setError(""); setBusy("upload");
    try {
      await api.codeUpload(pid, file);
      toast("Code graph uploaded");
      refresh();
    } catch (e) { setError((e as Error).message); } finally {
      setBusy("");
      if (fileRef.current) fileRef.current.value = "";
    }
  };
  const reconstruct = async () => {
    if (!overview?.latest) return;
    setError(""); setBusy("reconstruct");
    try {
      const r = await api.codeReconstruct(pid, overview.latest.id);
      toast(`Reconstructed ${r.components} components, ${r.connections} connections → ${r.branch} (v${r.version})`);
      onOpenBranch(r.branch);
      onClose();
    } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  };
  const runImpact = async () => {
    const files = impactFiles.split(/[\n,]/).map((f) => f.trim()).filter(Boolean);
    if (!files.length) return;
    setError(""); setBusy("impact");
    try { setImpact(await api.codeImpact(pid, branch, files)); } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  };

  const latest = overview?.latest;
  const s = latest?.summary;
  const running = job && (job.status === "queued" || job.status === "running");
  const repoLink = codeUrl(latest?.repo_url, null);

  return (
    <div className="modal-backdrop" onClick={onClose} ref={escRef}>
      <div className="modal wide code-dialog" role="dialog" aria-label="Code analysis" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Code ↔ architecture</h3>
          <button className="ghost" onClick={onClose} aria-label="Close"><Icon as={X} size={16} /></button>
        </div>
        <p className="muted small">
          Analyse a repository into a code graph (files, symbols, calls, imports, communities) and detected infrastructure,
          reconstruct its architecture as a draft branch, and see where the design and the code disagree.
        </p>

        {!readOnly && (
          <section className="code-section">
            <h4>Analyse a repository</h4>
            {overview && !overview.available && <p className="small warn">{overview.install_hint}</p>}
            <form className="row gap" onSubmit={(e) => { e.preventDefault(); analyze(); }}>
              <input className="grow" type="url" placeholder="https://github.com/org/repo" value={url}
                onChange={(e) => setUrl(e.target.value)} aria-label="Public Git repository URL" disabled={!!running} />
              <button className="primary" disabled={!overview?.available || !!running || !/^https:\/\//.test(url.trim())}>
                {running ? "Analysing…" : "Analyse"}
              </button>
            </form>
            {running && (
              <div className="code-progress" role="status">
                <div className="meter-bar"><div className="meter-fill indeterminate" /></div>
                <span className="small muted">{PHASES[job!.phase] ?? job!.phase}</span>
              </div>
            )}
            <details className="small">
              <summary>Private code? Analyse it on your machine and upload only the graph</summary>
              <p className="muted">Source code never leaves your machine; the file holds paths, symbol names, relations and detected infrastructure.</p>
              <pre className="code-cli">{overview?.cli ?? "uv run archflow-codegraph <folder> -o archflow-codegraph.json"}</pre>
              <label className="row gap">
                <input ref={fileRef} type="file" accept=".json,application/json" disabled={busy === "upload"}
                  onChange={(e) => { const f = e.target.files?.[0]; if (f) uploadFile(f); }} />
                {busy === "upload" && <span className="muted">Uploading…</span>}
              </label>
            </details>
          </section>
        )}
        {error && <p className="small error">{error}</p>}

        {overview && !latest && <p className="muted small">No analysis yet{readOnly ? "." : ": analyse a repository or upload a graph."}</p>}

        {latest && s && (
          <section className="code-section">
            <div className="row between">
              <h4>Latest analysis</h4>
              <span className="small muted">
                {latest.repo_url ? (repoLink ? <a href={repoLink} target="_blank" rel="noreferrer">{latest.repo_url}</a> : latest.repo_url)
                  : s.root_name || latest.source}
                {latest.commit_sha && <> @ <span className="mono">{latest.commit_sha.slice(0, 10)}</span></>}
                {" · "}{latest.source} · {new Date(latest.created_at).toLocaleString()}
              </span>
            </div>
            <div className="code-stats">
              {([["files", s.stats.files], ["symbols", s.stats.symbols], ["relations", s.stats.edges],
                ["communities", s.stats.communities]] as const).map(([k, v]) => (
                <div key={k} className="code-stat"><b>{v ?? 0}</b><span className="small muted">{k}</span></div>
              ))}
            </div>
            <div className="row gap small">
              {Object.entries(s.languages).map(([lang, n]) => <span key={lang} className="chip">{lang} {n}</span>)}
            </div>
            <div className="code-grid">
              <div>
                <h4>Detected infrastructure</h4>
                {s.infrastructure.length === 0 && <p className="muted small">None detected.</p>}
                <ul className="code-list">
                  {s.infrastructure.map((i) => (
                    <li key={i.key}><b>{i.label}</b> <span className="muted">{i.technology && i.technology !== i.label ? `· ${i.technology} ` : ""}
                      · {i.files ? `${i.files} file(s)` : i.evidence[0] ?? ""}</span></li>
                  ))}
                  {s.frameworks.map((f) => <li key={f.name}>{f.name} <span className="muted">· {f.kind === "ui" ? "UI" : "web framework"} · {f.files} file(s)</span></li>)}
                  {s.compose_services.some((c) => !c.dev_tool) && <li className="muted">docker-compose: {s.compose_services.filter((c) => !c.dev_tool).map((c) => c.name).join(", ")}</li>}
                  {(s.dev_tools?.length ?? 0) > 0 && (
                    <li className="muted" title="Dev/test tools are left out of the reconstructed architecture">
                      dev tools (not in the architecture): {s.dev_tools!.map((c) => c.name).join(", ")}</li>
                  )}
                  {(s.dockerfiles > 0 || s.k8s > 0 || s.terraform > 0) && (
                    <li className="muted">{s.dockerfiles} Dockerfile(s) · {s.k8s} Kubernetes object(s) · {s.terraform} Terraform resource(s)</li>
                  )}
                </ul>
              </div>
              <div>
                <h4>Entry points</h4>
                <ul className="code-list mono small">
                  {s.entry_points.slice(0, 8).map((e, k) => <li key={k} title={e.kind}>{e.path} <span className="muted">({e.kind})</span></li>)}
                  {s.entry_points.length === 0 && <li className="muted">None found.</li>}
                </ul>
                <h4>Largest communities</h4>
                <ul className="code-list small">
                  {s.communities.slice(0, 6).map((c) => <li key={c.id}>{c.label} <span className="muted">· {c.size} symbols · {c.paths.join(", ")}</span></li>)}
                </ul>
              </div>
            </div>
            {!readOnly && (
              <div className="row gap">
                <button className="primary" disabled={busy === "reconstruct"} onClick={reconstruct}
                  title="Creates a new draft branch from main with the architecture found in the code; main is not changed">
                  {busy === "reconstruct" ? "Reconstructing…" : "Reconstruct architecture → open draft branch"}
                </button>
              </div>
            )}
          </section>
        )}

        {latest && drift && (
          <section className="code-section">
            <div className="row between">
              <h4>Drift: {branch} vs code</h4>
              <span className={`small ${drift.alignment >= 80 ? "ok" : drift.alignment >= 50 ? "warn" : "error"}`}>
                alignment {drift.alignment}%
              </span>
            </div>
            {drift.issues.length === 0 && <p className="muted small">The design matches the code.</p>}
            <ul className="issue-list">
              {drift.issues.map((i, k) => (
                <li key={k} className={`issue ${i.severity}`} title={i.node_ids.length ? "Show on the canvas" : undefined}
                  onClick={() => { if (i.node_ids.length) { onFocus(i); onClose(); } }}>
                  <div><span className="sev">{i.severity}</span> {i.message}</div>
                  {i.suggestion && <div className="small muted">{i.suggestion}</div>}
                </li>
              ))}
            </ul>
          </section>
        )}

        {latest && (
          <section className="code-section">
            <h4>Change impact</h4>
            <div className="row gap">
              <textarea className="grow mono small" rows={2} placeholder="Changed files, one per line (e.g. backend/app/orders.py)"
                value={impactFiles} onChange={(e) => setImpactFiles(e.target.value)} aria-label="Changed files" />
              <button disabled={busy === "impact" || !impactFiles.trim()} onClick={runImpact}>Check impact</button>
            </div>
            {impact && (
              <div className="small">
                <p>
                  {impact.affected_symbol_count} dependent symbol(s) in {impact.affected_files.length} file(s)
                  {impact.unmatched_files.length > 0 && <span className="muted"> · not in the graph: {impact.unmatched_files.join(", ")}</span>}
                </p>
                <p>Components: {impact.components.length ? impact.components.map((c) => (
                  <button key={c.id} className="link" onClick={() => {
                    onFocus({ severity: "info", code: "impact", message: "", suggestion: "", node_ids: [c.id], requirement_ids: [] });
                    onClose();
                  }}>{c.name}</button>
                )).reduce<React.ReactNode[]>((acc, x, k) => (k ? [...acc, ", ", x] : [x]), []) : <span className="muted">none matched in {branch}</span>}</p>
                <p>Requirements: {impact.requirements.length
                  ? impact.requirements.map((r) => `${r.id} ${r.title}`).join(" · ")
                  : <span className="muted">none traced</span>}</p>
              </div>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
