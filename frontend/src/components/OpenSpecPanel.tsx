import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { OpenSpecChange, OpenSpecJob, OpenSpecOverview } from "../types";
import { Markdown } from "./Markdown";
import { Select } from "./Select";

interface Props {
  pid: string;
  branch: string;
  nodeIds: string[];
  requirementIds: string[];
  targets: { key: string; name: string }[];
  readOnly: boolean;
  /** True while a change is being written (the dialog should not close). */
  onBusy: (busy: boolean) => void;
}

type View = { kind: "new" } | { kind: "change"; id: string } | { kind: "spec"; path: string };
const PROMPT = "__prompt__";
const STEPS = ["Naming the change", "Writing proposal", "Writing specs", "Writing design", "Writing tasks", "Validating with OpenSpec"];

const STATUS: Record<OpenSpecChange["status"], { label: string; cls: string }> = {
  valid: { label: "✓ valid", cls: "ok" }, invalid: { label: "! issues", cls: "bad" },
  draft: { label: "draft", cls: "" }, archived: { label: "archived", cls: "muted" },
};

/**
 * OpenSpec workspace: changes written with the real OpenSpec workflow (proposal → specs → design → tasks, validated
 * with `openspec validate --strict`), stored in ArchFlow; edit, re-validate, archive into the living specs.
 */
export function OpenSpecPanel({ pid, branch, nodeIds, requirementIds, targets, readOnly, onBusy }: Props) {
  const [overview, setOverview] = useState<OpenSpecOverview | null>(null);
  const [view, setView] = useState<View>({ kind: "new" });
  const [target, setTarget] = useState("claude-code");
  const [extra, setExtra] = useState("");
  const [job, setJob] = useState<OpenSpecJob | null>(null);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<{ change: OpenSpecChange; dir: string; files: Record<string, string>; prompt: string } | null>(null);
  const [tab, setTab] = useState(PROMPT);
  const [editing, setEditing] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [confirm, setConfirm] = useState<"archive" | "delete" | null>(null);
  const [spec, setSpec] = useState<{ path: string; content: string } | null>(null);
  const [copied, setCopied] = useState("");
  const poll = useRef<number | undefined>(undefined);

  const refresh = useCallback(() => api.openspec(pid).then(setOverview).catch((e) => setError((e as Error).message)), [pid]);
  useEffect(() => { refresh(); return () => window.clearTimeout(poll.current); }, [refresh]);
  useEffect(() => { onBusy(job?.status === "running"); }, [job, onBusy]);

  const openChange = useCallback(async (id: string, keepTab = false) => {
    setError(""); setEditing(null); setConfirm(null);
    try {
      const d = await api.openspecChange(pid, id, target);
      setDetail(d);
      setView({ kind: "change", id });
      if (!keepTab) setTab(PROMPT);
    } catch (e) { setError((e as Error).message); }
  }, [pid, target]);
  // The agent prompt is phrased for the chosen agent.
  useEffect(() => { if (view.kind === "change") openChange(view.id, true); }, [target]); // eslint-disable-line react-hooks/exhaustive-deps

  const openSpec = async (path: string) => {
    setError("");
    try { const f = await api.openspecFile(pid, path); setSpec({ path, content: f.content }); setView({ kind: "spec", path }); }
    catch (e) { setError((e as Error).message); }
  };

  const start = async () => {
    setError("");
    try {
      const j = await api.openspecNew(pid, branch, nodeIds, requirementIds, target, extra, overview?.ai ?? false);
      setJob(j);
      const tick = async () => {
        try {
          const s = await api.openspecJob(pid, j.id);
          setJob(s);
          if (s.status === "running") { poll.current = window.setTimeout(tick, 1200); return; }
          await refresh();
          if (s.status === "done" && s.change_id) openChange(s.change_id);
          else setError(s.error ?? "Writing the change failed.");
        } catch (e) { setError((e as Error).message); setJob(null); }
      };
      poll.current = window.setTimeout(tick, 800);
    } catch (e) { setError((e as Error).message); }
  };

  const act = async (what: string, fn: () => Promise<unknown>) => {
    setBusy(what); setError("");
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(""); }
  };
  const copy = async (text: string, what: string) => {
    try { await navigator.clipboard.writeText(text); setCopied(what); window.setTimeout(() => setCopied(""), 1500); } catch { /* ignore */ }
  };

  const scope = nodeIds.length || requirementIds.length
    ? `${nodeIds.length} component(s), ${requirementIds.length} requirement(s) + traced context` : "the entire architecture";
  const active = overview?.changes.filter((c) => c.status !== "archived") ?? [];
  const archived = overview?.changes.filter((c) => c.status === "archived") ?? [];
  const files = detail ? ["proposal.md", ...Object.keys(detail.files).filter((f) => f.startsWith("specs/")).sort(), "design.md", "tasks.md",
    ...Object.keys(detail.files).filter((f) => !/^(proposal|design|tasks)\.md$/.test(f) && !f.startsWith("specs/"))]
    .filter((f, i, all) => f in detail.files && all.indexOf(f) === i) : [];
  const running = job?.status === "running";
  const changeItem = (c: OpenSpecChange) => (
    <li key={c.change_id}>
      <button className={`os-item ${view.kind === "change" && view.id === c.change_id ? "on" : ""}`} onClick={() => openChange(c.change_id)}>
        <span className="os-item-title">{c.title || c.change_id}</span>
        <span className="os-item-meta"><span className={`os-badge ${STATUS[c.status].cls}`}>{STATUS[c.status].label}</span>
          <span className="muted tiny">{c.change_id}</span></span>
      </button>
    </li>
  );

  return (
    <div className="os-panel">
      <aside className="os-side">
        {!readOnly && (
          <button className={`primary os-new ${view.kind === "new" ? "on" : ""}`} onClick={() => { setView({ kind: "new" }); setError(""); }}>
            ✦ New change
          </button>
        )}
        <div className="os-group">Changes</div>
        {overview && active.length === 0 && <p className="muted tiny os-empty">No open changes.</p>}
        <ul>{active.map(changeItem)}</ul>
        <div className="os-group">Living specs</div>
        {overview && overview.specs.length === 0 && <p className="muted tiny os-empty">None yet: archive a change to create them.</p>}
        <ul>
          {overview?.specs.map((s) => (
            <li key={s.path}><button className={`os-item ${view.kind === "spec" && view.path === s.path ? "on" : ""}`} onClick={() => openSpec(s.path)}>
              <span className="os-item-title">{s.capability}</span></button></li>
          ))}
        </ul>
        {archived.length > 0 && (
          <details className="os-archived"><summary className="os-group">Archived ({archived.length})</summary><ul>{archived.map(changeItem)}</ul></details>
        )}
        <button className="small os-zip" disabled={!overview || (!overview.changes.length && !overview.specs.length)}
          onClick={() => act("zip", () => api.openspecZip(pid))} title="The whole openspec/ folder: config, living specs, changes and archive">
          ⤓ openspec/ .zip
        </button>
        {overview && !overview.available && <p className="warn tiny">OpenSpec is not installed on the server: changes are saved but not validated.</p>}
      </aside>

      <section className="os-main">
        {error && <p className="error small">{error}</p>}

        {view.kind === "new" && (
          <div className="os-new-form">
            <h4>Write a new change</h4>
            <p className="muted small">
              Uses <b>OpenSpec's own workflow</b>: for each artifact ArchFlow asks OpenSpec for its instructions and template,
              {overview?.ai ? " the AI writes it" : " it is derived from the design (no LLM configured)"} from {scope}, its
              connections, internal designs, decisions, project memory and your living specs; then <code>openspec validate
              --strict</code> checks it{overview?.ai ? " and the AI fixes any issues" : ""}. Everything is saved in the project.
            </p>
            <div className="row gap">
              <Select value={target} onChange={setTarget} aria-label="Coding agent" disabled={running}
                options={targets.map((t) => ({ value: t.key, label: t.name }))} />
              <input className="grow" value={extra} onChange={(e) => setExtra(e.target.value)} disabled={running}
                placeholder="Instructions for this change (optional): conventions, libraries, what to leave out…" />
              <button className="primary" disabled={running || readOnly} onClick={start}>
                {running ? "Writing…" : overview?.ai ? "✦ Write with AI" : "Create change"}
              </button>
            </div>
            {job && (
              <ol className="os-steps">
                {STEPS.map((s) => {
                  const idx = job.steps.findIndex((x) => x.step === s);
                  const current = job.status === "running" && job.step === s;
                  const done = idx >= 0 && !current;
                  const fixing = s === "Validating with OpenSpec" ? job.steps.filter((x) => x.step.startsWith("Fixing")).length : 0;
                  return (
                    <li key={s} className={current ? "current" : done ? "done" : ""}>
                      <span className="os-step-mark">{current ? <span className="pulse">●</span> : done ? "✓" : "○"}</span> {s}
                      {fixing > 0 && <span className="muted small"> · fixed issues ×{fixing}</span>}
                    </li>
                  );
                })}
                <li className="muted small">{job.status === "running" ? `${job.elapsed ?? 0}s` : job.status === "done" ? "Done" : "Failed"}</li>
              </ol>
            )}
          </div>
        )}

        {view.kind === "spec" && spec && (
          <div className="os-doc">
            <div className="os-head"><b>Living spec</b> <code className="small">{spec.path}</code></div>
            <div className="prompt-out vibe-doc"><Markdown text={spec.content} /></div>
            <div className="row gap end"><button onClick={() => copy(spec.content, "spec")}>{copied === "spec" ? "Copied ✓" : "Copy"}</button></div>
          </div>
        )}

        {view.kind === "change" && detail && (
          <div className="os-doc">
            <div className="os-head">
              <div><b>{detail.change.title || detail.change.change_id}</b> <code className="small">{detail.dir}</code></div>
              <span className="row gap">
                <span className={`os-badge ${STATUS[detail.change.status].cls}`}>{STATUS[detail.change.status].label}</span>
                <span className="vibe-engine">{detail.change.engine === "openspec+llm" ? "✦ AI · OpenSpec" : detail.change.engine === "openspec" ? "OpenSpec · derived" : "not validated"}</span>
              </span>
            </div>
            {detail.change.validation.skipped && <p className="warn small">{detail.change.validation.skipped}</p>}
            {(detail.change.validation.notes ?? []).map((n, i) => <p key={i} className="muted small">ⓘ {n}</p>)}
            {!!detail.change.validation.issues?.length && (
              <ul className="os-issues">
                {detail.change.validation.issues.map((i, k) => (
                  <li key={k} className={i.level === "ERROR" ? "error" : "warn"}><b>{i.level}</b> {i.path && <code>{i.path}</code>} {i.message}</li>
                ))}
              </ul>
            )}
            {detail.change.status === "archived" && detail.change.validation.archive?.totals && (
              <p className="muted small">Merged into the living specs: {Object.entries(detail.change.validation.archive.totals)
                .filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(", ") || "no requirement changes"}.</p>
            )}
            <div className="row between">
              <div className="view-tabs vibe-tabs" role="tablist">
                <button role="tab" aria-selected={tab === PROMPT} className={`vchip ${tab === PROMPT ? "on" : ""}`} onClick={() => { setTab(PROMPT); setEditing(null); }}>
                  Agent prompt
                </button>
                {files.map((f) => (
                  <button key={f} role="tab" aria-selected={tab === f} className={`vchip ${tab === f ? "on" : ""}`} onClick={() => { setTab(f); setEditing(null); }}>
                    {f.replace(/^specs\/(.+)\/spec\.md$/, "spec: $1")}
                  </button>
                ))}
              </div>
              <Select size="sm" value={target} onChange={setTarget} aria-label="Coding agent for the prompt" className="os-target"
                options={targets.map((t) => ({ value: t.key, label: t.name }))} />
            </div>
            {tab === PROMPT ? <pre className="prompt-out">{detail.prompt}</pre>
              : editing !== null ? (
                <textarea className="prompt-out os-editor mono" value={editing} onChange={(e) => setEditing(e.target.value)} spellCheck={false} aria-label={`Edit ${tab}`} />
              ) : <div className="prompt-out vibe-doc"><Markdown text={detail.files[tab] ?? ""} /></div>}
            <div className="row gap end">
              {!readOnly && detail.change.status !== "archived" && (
                <>
                  {confirm === "delete" ? (
                    <><span className="small">Delete this change?</span>
                      <button className="small danger" disabled={!!busy} onClick={() => act("delete", async () => {
                        await api.openspecDelete(pid, detail.change.change_id); setDetail(null); setView({ kind: "new" }); await refresh();
                      })}>Delete</button>
                      <button className="small" onClick={() => setConfirm(null)}>Cancel</button></>
                  ) : <button className="small ghost" onClick={() => setConfirm("delete")}>Delete</button>}
                  <span className="spacer" />
                  {tab !== PROMPT && (editing === null
                    ? <button onClick={() => setEditing(detail.files[tab] ?? "")}>✎ Edit</button>
                    : <>
                        <button onClick={() => setEditing(null)}>Cancel</button>
                        <button className="primary" disabled={!!busy} onClick={() => act("save", async () => {
                          await api.openspecSaveFile(pid, detail.change.change_id, tab, editing); setEditing(null);
                          await openChange(detail.change.change_id, true); await refresh();
                        })}>{busy === "save" ? "Saving & validating…" : "Save & validate"}</button>
                      </>)}
                  {editing === null && <button disabled={!!busy} onClick={() => act("validate", async () => {
                    await api.openspecValidate(pid, detail.change.change_id); await openChange(detail.change.change_id, true); await refresh();
                  })}>{busy === "validate" ? "Validating…" : "Validate"}</button>}
                  {editing === null && (confirm === "archive" ? (
                    <><span className="small">Merge into the living specs?</span>
                      <button className="small primary" disabled={!!busy} onClick={() => act("archive", async () => {
                        await api.openspecArchive(pid, detail.change.change_id); setConfirm(null); await refresh(); await openChange(detail.change.change_id, true);
                      })}>{busy === "archive" ? "Archiving…" : "Archive"}</button>
                      <button className="small" onClick={() => setConfirm(null)}>Cancel</button></>
                  ) : <button disabled={detail.change.status !== "valid"} title={detail.change.status === "valid"
                    ? "openspec archive: merge the spec deltas into the living specs and move the change to the archive"
                    : "Fix the validation issues first"} onClick={() => setConfirm("archive")}>Archive</button>)}
                </>
              )}
              {editing === null && tab !== PROMPT && <button onClick={() => copy(detail.files[tab] ?? "", "file")}>{copied === "file" ? "Copied ✓" : "Copy file"}</button>}
              {editing === null && <button className="primary" onClick={() => copy(detail.prompt, "prompt")}>{copied === "prompt" ? "Copied ✓" : "Copy agent prompt"}</button>}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
