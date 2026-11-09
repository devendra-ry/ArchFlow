import { useEffect, useRef, useState } from "react";
import { Check, Circle, Icon, X } from "../icons";
import { api } from "../api";
import { useEscape } from "../useEscape";
import { Select, plainOptions } from "./Select";
import type { IntakeMatch, IntakeNote, IntakeProposal, IntakeResult, IntakeStep, MemoryCategory } from "../types";

interface Props {
  pid: string;
  branch: string;
  onDone: (message: string, savedMemory: boolean) => void;
  onClose: () => void;
}

const PRIORITY = ["low", "medium", "high", "critical"] as const;
const NOTE_KINDS: { key: "assumptions" | "constraints" | "open_questions"; label: string; category: MemoryCategory; prefix: string }[] = [
  { key: "assumptions", label: "Assumptions", category: "context", prefix: "Assumption: " },
  { key: "constraints", label: "Constraints", category: "constraint", prefix: "" },
  { key: "open_questions", label: "Open questions", category: "context", prefix: "Open question: " },
];

/** "prd.pdf §2.1 Login p. 3" (a document) or "standup.vtt · Priya · 00:12:05" (a meeting) from a provenance record. */
export function sourceLabel(src: Record<string, string> | undefined): string {
  if (!src || !src.ref) return "";
  if (src.type === "meeting") return [`🎙 ${src.ref}`, src.section, src.timestamp].filter(Boolean).join(" · ");
  return [src.ref, src.section && `§ ${src.section}`, src.page && `p. ${src.page}`].filter(Boolean).join(" · ");
}

export const STAGES: { key: string; label: string }[] = [
  { key: "upload", label: "Upload" },
  { key: "read", label: "Read the file" },
  { key: "split", label: "Split into parts" },
  { key: "extract", label: "Find requirements" },
  { key: "compare", label: "Check duplicates and conflicts" },
];

export const size = (bytes: number) => bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;

/** What the import is doing right now: each stage with its latest message, the current one spinning. */
export function ImportProgress({ file, steps, seconds, onCancel, stages = STAGES }: {
  file: { name: string; size: number }; steps: Partial<Record<string, Omit<IntakeStep, "stage"> & { stage: string }>>; seconds: number;
  onCancel: () => void; stages?: { key: string; label: string }[];
}) {
  const reached = stages.reduce((at, s, i) => (steps[s.key] ? i : at), 0);
  return (
    <div className="intake-progress" role="status" aria-live="polite">
      <div className="row between">
        <span className="small"><span className="spinner" aria-hidden /> Reading <b>{file.name}</b>{file.size > 0 && <> · {size(file.size)}</>}</span>
        <span className="small muted">{seconds}s</span>
      </div>
      <ol className="intake-steps">
        {stages.map((s, i) => {
          const step = steps[s.key];
          const state = i < reached ? "done" : i === reached ? "now" : "todo";
          const message = step?.message ?? (s.key === "upload" && file.size > 0 ? `Sending ${size(file.size)} to the server…` : "");
          return (
            <li key={s.key} className={state}>
              <span className="intake-step-icon" aria-hidden>{state === "done" ? <Icon as={Check} size={12} /> : state === "now" ? <span className="spinner" /> : <Icon as={Circle} size={12} />}</span>
              <div>
                <div className="small">{s.label}</div>
                {message && <div className="small muted">{message}</div>}
                {state === "now" && step?.total ? (
                  <div className="meter-bar" title={`${step.done} of ${step.total}`}>
                    <div className="meter-fill good" style={{ width: `${Math.round(((step.done ?? 0) / step.total) * 100)}%` }} />
                  </div>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>
      <div className="row end"><button onClick={onCancel}>Cancel</button></div>
    </div>
  );
}

const matchTitle = (m: IntakeMatch) =>
  `${m.id} ${m.title} (${m.where === "project" ? "in the project" : "earlier in this document"})` +
  `${m.reason ? ` — ${m.reason}` : ""}${sourceLabel(m.source) ? `\nSource: ${sourceLabel(m.source)}` : ""}` +
  `${m.proposed_source ? `\nThis document: ${sourceLabel(m.proposed_source)}` : ""}`;

/** Upload a requirements document, review what the AI (or the offline rules) found, edit, and accept a selection. */
export function ImportRequirementsDialog({ pid, branch, onDone, onClose }: Props) {
  const escRef = useEscape(onClose);
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState<"" | "read" | "accept">("");
  const [error, setError] = useState("");
  const [result, setResult] = useState<IntakeResult | null>(null);
  const [items, setItems] = useState<IntakeProposal[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [notes, setNotes] = useState<Set<string>>(new Set());
  const [reading, setReading] = useState<File | null>(null);
  const [steps, setSteps] = useState<Partial<Record<IntakeStep["stage"], IntakeStep>>>({});
  const [seconds, setSeconds] = useState(0);
  const [cancelled, setCancelled] = useState(false);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => abortRef.current?.abort(), []); // closing the dialog cancels a running import
  useEffect(() => {
    if (busy !== "read") return;
    const started = Date.now();
    const timer = window.setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [busy]);

  const read = async (file: File) => {
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setError(""); setCancelled(false); setSteps({}); setSeconds(0); setReading(file); setBusy("read");
    try {
      const r = await api.importRequirements(pid, branch, file, (s) => setSteps((x) => ({ ...x, [s.stage]: s })), ctrl.signal);
      setResult(r); setItems(r.requirements); setNotes(new Set());
    } catch (e) {
      if ((e as Error).name === "AbortError") setCancelled(true); else setError((e as Error).message);
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null;
      if (fileRef.current) fileRef.current.value = ""; // the same file can be picked again
      setReading(null); setBusy("");
    }
  };
  const cancel = () => abortRef.current?.abort();
  const patch = (id: string, changes: Partial<IntakeProposal>) =>
    setItems((xs) => xs.map((x) => (x.id === id ? { ...x, ...changes } : x)));
  const chosen = items.filter((x) => x.selected && x.title.trim());
  const noteKey = (k: string, i: number) => `${k}:${i}`;

  const accept = async () => {
    if (!result) return;
    setError(""); setBusy("accept");
    const memory = NOTE_KINDS.flatMap((k) => result[k.key].flatMap((n: IntakeNote, i) =>
      notes.has(noteKey(k.key, i)) ? [{ ...n, text: k.prefix + n.text, category: k.category }] : []));
    try {
      const r = await api.acceptImport(pid, branch, result.file, chosen.map((p) => ({
        title: p.title.trim(), description: p.description, kind: p.kind, category: p.kind === "non_functional" ? p.category : "",
        priority: p.priority, actors: p.actors, acceptance_criteria: p.acceptance_criteria.map((c) => c.trim()).filter(Boolean), source: p.source,
      })), memory);
      onDone(`Imported ${r.added.length} requirement(s) from ${result.file.name}`
        + (r.memory.length ? ` · ${r.memory.length} note(s) saved to memory` : ""), r.memory.length > 0);
    } catch (e) { setError((e as Error).message); setBusy(""); }
  };

  return (
    <div className="modal-backdrop" onClick={onClose} ref={escRef}>
      <div className="modal wide intake" role="dialog" aria-label="Import requirements from a document" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Import requirements from a document</h3>
          <button className="ghost" onClick={onClose} aria-label="Close"><Icon as={X} size={16} /></button>
        </div>
        {!result && (
          <div className="intake-pick">
            <p className="small muted">Upload a PRD, RFP or spec (PDF, Word .docx, Markdown or text). ArchFlow proposes functional and
              non-functional requirements with the section and page they came from; nothing is added until you accept.</p>
            <input ref={fileRef} type="file" accept=".pdf,.docx,.md,.markdown,.txt,.text" disabled={busy === "read"} aria-label="Document"
              hidden={busy === "read"} onChange={(e) => { const f = e.target.files?.[0]; if (f) read(f); }} />
            {busy === "read" && reading && <ImportProgress file={reading} steps={steps} seconds={seconds} onCancel={cancel} />}
            {cancelled && <p className="small muted">Import cancelled. Nothing was added; choose a file to start again.</p>}
          </div>
        )}
        {result && (
          <>
            <p className="small muted">
              <b>{result.file.name}</b>{result.file.pages ? ` · ${result.file.pages} page(s)` : ""} · {result.file.sections} section(s) ·{" "}
              {result.reader && <>read with {result.reader.library} · </>}
              {result.engine === "offline" ? "requirements found by the offline rules (no AI configured)"
                : `requirements found by ${result.reader?.model || "the AI"}${result.engine === "mixed" ? " (some parts by the offline rules)" : ""}`}
              {result.stakeholders.length > 0 && <> · stakeholders: {result.stakeholders.join(", ")}</>}
            </p>
            {result.notes.map((n, i) => <p key={i} className="small warn">{n}</p>)}
            {items.length === 0 && <p className="small">No requirements found in this document.</p>}
            {items.length > 0 && (
              <div className="row gap small">
                <button className="small" onClick={() => setItems((xs) => xs.map((x) => ({ ...x, selected: true })))}>Select all</button>
                <button className="small" onClick={() => setItems((xs) => xs.map((x) => ({ ...x, selected: false })))}>Select none</button>
                <span className="muted">{chosen.length} of {items.length} selected · duplicates start unselected</span>
              </div>
            )}
            <ul className="intake-list">
              {items.map((p) => (
                <li key={p.id} className={p.selected ? "" : "off"}>
                  <div className="row gap">
                    <input type="checkbox" checked={p.selected} aria-label={`Import ${p.title}`}
                      onChange={(e) => patch(p.id, { selected: e.target.checked })} />
                    <input className="intake-title" value={p.title} aria-label="Title" onChange={(e) => patch(p.id, { title: e.target.value })} />
                    <button className="ghost small" onClick={() => setOpen(open === p.id ? null : p.id)} aria-expanded={open === p.id}>
                      {open === p.id ? "Less" : "Edit"}</button>
                  </div>
                  <div className="small muted intake-meta">
                    {p.kind === "non_functional" ? `NFR${p.category ? ` · ${p.category}` : ""}` : "Functional"} · {p.priority}
                    {sourceLabel(p.source) && <> · <span title="Where it is in the document">{sourceLabel(p.source)}</span></>}
                    {p.duplicates.map((m) => <span key={`d${m.id}`} className="intake-badge dup" title={matchTitle(m)}>Duplicate of {m.id}</span>)}
                    {p.conflicts.map((m) => <span key={`c${m.id}`} className="intake-badge conflict" title={matchTitle(m)}>Conflicts with {m.id}</span>)}
                  </div>
                  {open === p.id && (
                    <div className="intake-edit">
                      <label className="small">Description
                        <textarea rows={3} value={p.description} onChange={(e) => patch(p.id, { description: e.target.value })} /></label>
                      <div className="grid2">
                        <label className="small">Kind
                          <Select size="sm" value={p.kind} aria-label="Kind" onChange={(v) => patch(p.id, { kind: v })}
                            options={plainOptions(["functional", "non_functional"] as const)} /></label>
                        <label className="small">Priority
                          <Select size="sm" value={p.priority} aria-label="Priority" onChange={(v) => patch(p.id, { priority: v })}
                            options={plainOptions(PRIORITY)} /></label>
                        {p.kind === "non_functional" && (
                          <label className="small">Category
                            <input value={p.category} onChange={(e) => patch(p.id, { category: e.target.value })} /></label>
                        )}
                        <label className="small">Actors (comma-separated)
                          <input value={p.actors.join(", ")}
                            onChange={(e) => patch(p.id, { actors: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} /></label>
                      </div>
                      <label className="small">Acceptance criteria (one per line)
                        <textarea rows={3} value={p.acceptance_criteria.join("\n")}
                          onChange={(e) => patch(p.id, { acceptance_criteria: e.target.value.split("\n") })} /></label>
                      {[...p.duplicates, ...p.conflicts].map((m) => (
                        <p key={`r${m.id}`} className="small muted">{matchTitle(m)}</p>
                      ))}
                    </div>
                  )}
                </li>
              ))}
            </ul>
            {NOTE_KINDS.some((k) => result[k.key].length > 0) && (
              <div className="intake-notes">
                <h4>Save to project memory?</h4>
                {NOTE_KINDS.filter((k) => result[k.key].length > 0).map((k) => (
                  <div key={k.key}>
                    <div className="small"><b>{k.label}</b></div>
                    {result[k.key].map((n, i) => (
                      <label key={i} className="row gap small">
                        <input type="checkbox" checked={notes.has(noteKey(k.key, i))} onChange={(e) => setNotes((s) => {
                          const next = new Set(s);
                          if (e.target.checked) next.add(noteKey(k.key, i)); else next.delete(noteKey(k.key, i));
                          return next;
                        })} />
                        <span>{n.text}{n.section ? <span className="muted"> · § {n.section}{n.page ? ` · p. ${n.page}` : ""}</span> : null}</span>
                      </label>
                    ))}
                  </div>
                ))}
              </div>
            )}
          </>
        )}
        {error && <p className="error small">{error}</p>}
        {result && (
          <div className="row gap end">
            <button onClick={() => { setResult(null); setItems([]); }}>Choose another file</button>
            <button className="primary" disabled={busy === "accept" || (chosen.length === 0 && notes.size === 0)} onClick={accept}>
              {busy === "accept" ? "Adding…" : `Add ${chosen.length} requirement${chosen.length === 1 ? "" : "s"}`}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
