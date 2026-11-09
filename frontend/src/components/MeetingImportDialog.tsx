import { useEffect, useRef, useState } from "react";
import { Icon, X } from "../icons";
import { api } from "../api";
import { useEscape } from "../useEscape";
import type { MeetingAction, MeetingDecision, MeetingNote, MeetingProposal, MeetingResult, MeetingStep, MemoryCategory } from "../types";
import { ImportProgress, sourceLabel } from "./ImportRequirementsDialog";

interface Props {
  pid: string;
  branch: string;
  canAddKnowledge: boolean;
  onDone: (message: string, changed: { memory: boolean; decisions: boolean; knowledge: boolean }) => void;
  onClose: () => void;
}

const STAGES = [
  { key: "upload", label: "Upload" },
  { key: "read", label: "Read the transcript" },
  { key: "split", label: "Split the conversation" },
  { key: "extract", label: "Find requirements, decisions and actions" },
  { key: "compare", label: "Check duplicates and conflicts" },
  { key: "knowledge", label: "Add to the knowledge base" },
];
const NOTE_KINDS: { key: "open_questions" | "assumptions" | "constraints"; label: string; category: MemoryCategory; prefix: string }[] = [
  { key: "open_questions", label: "Open questions", category: "context", prefix: "Open question: " },
  { key: "assumptions", label: "Assumptions", category: "context", prefix: "Assumption: " },
  { key: "constraints", label: "Constraints", category: "constraint", prefix: "" },
];
const AGREEMENT: Record<MeetingProposal["agreement"], string> = { agreed: "agreed", proposed: "only proposed", disputed: "disputed" };
const at = (ts?: string) => (ts ? ` · ${ts}` : "");

/**
 * Upload a meeting transcript (Teams / Zoom / Meet .vtt, .srt, text, Word) or a recording, review the requirements,
 * decisions and action items found, and accept a selection: requirements with the speaker and time they came from,
 * decisions as proposed ADRs, action items and open questions as project memory.
 */
export function MeetingImportDialog({ pid, branch, canAddKnowledge, onDone, onClose }: Props) {
  const escRef = useEscape(onClose);
  const fileRef = useRef<HTMLInputElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const [busy, setBusy] = useState<"" | "read" | "accept">("");
  const [error, setError] = useState("");
  const [toKnowledge, setToKnowledge] = useState(canAddKnowledge);
  const [reading, setReading] = useState<File | null>(null);
  const [steps, setSteps] = useState<Partial<Record<string, MeetingStep>>>({});
  const [seconds, setSeconds] = useState(0);
  const [cancelled, setCancelled] = useState(false);
  const [result, setResult] = useState<MeetingResult | null>(null);
  const [reqs, setReqs] = useState<MeetingProposal[]>([]);
  const [decisions, setDecisions] = useState<MeetingDecision[]>([]);
  const [actions, setActions] = useState<MeetingAction[]>([]);
  const [notes, setNotes] = useState<Set<string>>(new Set());

  useEffect(() => () => abortRef.current?.abort(), []);
  useEffect(() => {
    if (busy !== "read") return;
    const started = Date.now();
    const t = window.setInterval(() => setSeconds(Math.floor((Date.now() - started) / 1000)), 1000);
    return () => window.clearInterval(t);
  }, [busy]);

  const read = async (file: File) => {
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    setError(""); setCancelled(false); setSteps({}); setSeconds(0); setReading(file); setBusy("read");
    try {
      const r = await api.importMeeting(pid, branch, file, toKnowledge, (s) => setSteps((x) => ({ ...x, [s.stage]: s })), ctrl.signal);
      show(r);
    } catch (e) {
      if ((e as Error).name === "AbortError") setCancelled(true); else setError((e as Error).message);
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null;
      if (fileRef.current) fileRef.current.value = "";
      setReading(null); setBusy("");
    }
  };

  function show(r: MeetingResult) {
    setResult(r); setReqs(r.requirements); setDecisions(r.decisions); setActions(r.action_items); setNotes(new Set());
  }

  const chosenReqs = reqs.filter((r) => r.selected && r.title.trim());
  const chosenDecisions = decisions.filter((d) => d.selected && d.title.trim());
  const chosenActions = actions.filter((a) => a.selected && a.task.trim());
  const noteKey = (k: string, i: number) => `${k}:${i}`;
  const total = chosenReqs.length + chosenDecisions.length + chosenActions.length + notes.size;

  const accept = async () => {
    if (!result) return;
    setError(""); setBusy("accept");
    const memory = [
      ...chosenActions.map((a) => ({ text: `Action: ${a.task}${a.owner ? ` (${a.owner}` : ""}${a.due ? `${a.owner ? ", " : " ("}due ${a.due}` : ""}${a.owner || a.due ? ")" : ""}`,
        category: "context" as MemoryCategory })),
      ...NOTE_KINDS.flatMap((k) => result[k.key].flatMap((n: MeetingNote, i) =>
        notes.has(noteKey(k.key, i)) ? [{ text: k.prefix + n.text, category: k.category }] : [])),
    ];
    try {
      const r = await api.acceptMeeting(pid, branch, result.file, chosenReqs.map((p) => ({
        title: p.title.trim(), description: p.description, kind: p.kind, category: p.kind === "non_functional" ? p.category : "",
        priority: p.priority, actors: p.actors, acceptance_criteria: p.acceptance_criteria.filter((c) => c.trim()), source: p.source,
      })), chosenDecisions.map(({ id: _id, selected: _s, ...d }) => d), memory);
      const bits = [`${r.added.length} requirement(s)`, r.decisions.length && `${r.decisions.length} proposed ADR(s)`,
        r.memory.length && `${r.memory.length} note(s) in memory`].filter(Boolean);
      onDone(`From ${result.file.name}: ${bits.join(", ")}`,
        { memory: r.memory.length > 0, decisions: r.decisions.length > 0, knowledge: !!result.knowledge });
    } catch (e) { setError((e as Error).message); setBusy(""); }
  };

  const patchReq = (id: string, c: Partial<MeetingProposal>) => setReqs((xs) => xs.map((x) => (x.id === id ? { ...x, ...c } : x)));
  const patchDec = (id: string, c: Partial<MeetingDecision>) => setDecisions((xs) => xs.map((x) => (x.id === id ? { ...x, ...c } : x)));
  const patchAct = (id: string, c: Partial<MeetingAction>) => setActions((xs) => xs.map((x) => (x.id === id ? { ...x, ...c } : x)));

  return (
    <div className="modal-backdrop" onClick={onClose} ref={escRef}>
      <div className="modal wide intake" role="dialog" aria-label="Import a meeting" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Import a meeting</h3>
          <button className="ghost" onClick={onClose} aria-label="Close"><Icon as={X} size={16} /></button>
        </div>
        {!result && (
          <div className="intake-pick">
            <p className="small muted">Upload a transcript from Teams, Zoom or Google Meet (.vtt, .srt, .txt, .docx) or a recording
              (.mp3, .m4a, .wav, .webm, .mp4; needs a speech-to-text model). ArchFlow proposes the requirements people asked for, the
              decisions taken and the action items, with who said it and when. Nothing is added until you accept.</p>
            {canAddKnowledge && (
              <label className="row gap small">
                <input type="checkbox" checked={toKnowledge} disabled={busy === "read"} onChange={(e) => setToKnowledge(e.target.checked)} />
                Also add the transcript to the knowledge base (searchable, cited by the assistant)
              </label>
            )}
            <input ref={fileRef} type="file" aria-label="Transcript or recording" hidden={busy === "read"} disabled={busy === "read"}
              accept=".vtt,.srt,.txt,.text,.md,.docx,.mp3,.m4a,.wav,.webm,.mp4,.mpeg,.ogg,.flac"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) read(f); }} />
            {busy === "read" && reading && (
              <ImportProgress file={reading} steps={steps} seconds={seconds} onCancel={() => abortRef.current?.abort()}
                stages={STAGES.filter((s) => s.key !== "knowledge" || toKnowledge)} />
            )}
            {cancelled && <p className="small muted">Import cancelled. Nothing was added; choose a file to start again.</p>}
          </div>
        )}
        {result && (
          <>
            <p className="small muted">
              <b>{result.file.name}</b> · {result.file.turns} turns{result.file.duration ? ` · ${result.file.duration}` : ""}
              {result.participants.length > 0 && <> · {result.participants.join(", ")}</>} ·{" "}
              {result.reader.transcription && <>transcribed by {result.reader.transcription} · </>}
              {result.engine === "offline" ? "read by the offline rules (no AI configured)"
                : `read by ${result.reader.model || "the AI"}${result.engine === "mixed" ? " (some parts by the offline rules)" : ""}`}
              {result.knowledge && <> · added to the knowledge base ({result.knowledge.chunks} passages)</>}
            </p>
            {result.summary && <p className="small meeting-summary">{result.summary}</p>}
            {result.notes.map((n, i) => <p key={i} className="small warn">{n}</p>)}

            <h4>Requirements ({chosenReqs.length} of {reqs.length})</h4>
            {reqs.length === 0 && <p className="small muted">No requirements found.</p>}
            <ul className="intake-list">
              {reqs.map((p) => (
                <li key={p.id} className={p.selected ? "" : "off"}>
                  <div className="row gap">
                    <input type="checkbox" checked={p.selected} aria-label={`Add ${p.title}`} onChange={(e) => patchReq(p.id, { selected: e.target.checked })} />
                    <input className="intake-title" value={p.title} aria-label="Title" onChange={(e) => patchReq(p.id, { title: e.target.value })} />
                  </div>
                  <div className="small muted intake-meta">
                    {p.kind === "non_functional" ? `NFR${p.category ? ` · ${p.category}` : ""}` : "Functional"} · {p.priority}
                    {" "}<span className={`intake-badge ${p.agreement === "agreed" ? "ok-badge" : p.agreement === "disputed" ? "conflict" : "dup"}`}
                      title="Whether the meeting agreed on it">{AGREEMENT[p.agreement]}</span>
                    {sourceLabel(p.source) && <> · {sourceLabel(p.source)}</>}
                    {p.duplicates.map((m) => <span key={`d${m.id}`} className="intake-badge dup" title={`${m.title}: ${m.reason}`}>Duplicate of {m.id}</span>)}
                    {p.conflicts.map((m) => <span key={`c${m.id}`} className="intake-badge conflict" title={`${m.title}: ${m.reason}`}>Conflicts with {m.id}</span>)}
                  </div>
                  {p.description && p.description !== p.title && <div className="small">{p.description}</div>}
                </li>
              ))}
            </ul>

            {decisions.length > 0 && (
              <>
                <h4>Decisions → proposed ADRs ({chosenDecisions.length} of {decisions.length})</h4>
                <ul className="intake-list">
                  {decisions.map((d) => (
                    <li key={d.id} className={d.selected ? "" : "off"}>
                      <div className="row gap">
                        <input type="checkbox" checked={d.selected} aria-label={`Record ${d.title}`} onChange={(e) => patchDec(d.id, { selected: e.target.checked })} />
                        <input className="intake-title" value={d.title} aria-label="Decision title" onChange={(e) => patchDec(d.id, { title: e.target.value })} />
                      </div>
                      <div className="small">{d.decision}</div>
                      <div className="small muted">
                        {d.rationale && <>Why: {d.rationale} · </>}
                        {d.alternatives.length > 0 && <>Also discussed: {d.alternatives.join(", ")} · </>}
                        {d.decided_by || "unknown"}{at(d.timestamp)}
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            )}

            {actions.length > 0 && (
              <>
                <h4>Action items → project memory ({chosenActions.length} of {actions.length})</h4>
                <ul className="intake-list">
                  {actions.map((a) => (
                    <li key={a.id} className={a.selected ? "" : "off"}>
                      <div className="row gap">
                        <input type="checkbox" checked={a.selected} aria-label={`Save ${a.task}`} onChange={(e) => patchAct(a.id, { selected: e.target.checked })} />
                        <input className="intake-title" value={a.task} aria-label="Task" onChange={(e) => patchAct(a.id, { task: e.target.value })} />
                        <input className="meeting-owner" value={a.owner} placeholder="owner" aria-label="Owner" onChange={(e) => patchAct(a.id, { owner: e.target.value })} />
                        <input className="meeting-owner" value={a.due} placeholder="due" aria-label="Due" onChange={(e) => patchAct(a.id, { due: e.target.value })} />
                      </div>
                    </li>
                  ))}
                </ul>
              </>
            )}

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
                        <span>{n.text}<span className="muted">{at(n.timestamp)}</span></span>
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
            <button onClick={() => setResult(null)}>Choose another file</button>
            <button className="primary" disabled={busy === "accept" || total === 0} onClick={accept}>
              {busy === "accept" ? "Adding…" : `Add ${total} item${total === 1 ? "" : "s"}`}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
