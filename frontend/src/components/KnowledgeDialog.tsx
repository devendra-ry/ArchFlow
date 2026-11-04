import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { KbAnswer, KbCitation, KbPassage, KbStatus } from "../types";
import { Markdown } from "./Markdown";
import { Modal } from "./OrgDialogs";

interface Props {
  pid: string;
  canEdit: boolean;
  onClose: () => void;
  /** Bumped when the knowledge base changes elsewhere (a teammate, a meeting import). */
  version: number;
}

const where = (c: Pick<KbCitation, "document" | "section" | "page" | "locator">) =>
  [c.document, c.section && `§ ${c.section}`, c.page && `p. ${c.page}`, c.locator].filter(Boolean).join(" · ");
const ORIGIN: Record<string, string> = { upload: "uploaded", import: "imported", meeting: "meeting" };

/**
 * The project's knowledge base: documents people uploaded (and meeting transcripts), searchable by keywords and
 * meaning, with answers that cite the passages they come from. The assistant (Ask) uses the same passages.
 */
export function KnowledgeDialog({ pid, canEdit, onClose, version }: Props) {
  const [status, setStatus] = useState<KbStatus | null>(null);
  const [tab, setTab] = useState<"ask" | "search" | "documents">("ask");
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<KbAnswer | null>(null);
  const [results, setResults] = useState<KbPassage[] | null>(null);
  const [mode, setMode] = useState("");
  const [busy, setBusy] = useState<"" | "ask" | "search" | "upload" | "reindex">("");
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const load = () => api.knowledge(pid).then(setStatus).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [pid, version]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (status && !status.documents.length) setTab("documents"); }, [status?.documents.length]); // eslint-disable-line

  const ask = async (q = question) => {
    if (!q.trim()) return;
    setQuestion(q); setBusy("ask"); setError("");
    try { setAnswer(await api.askKnowledge(pid, q)); } catch (e) { setError((e as Error).message); }
    setBusy("");
  };
  const search = async () => {
    if (!question.trim()) return;
    setBusy("search"); setError("");
    try { const r = await api.searchKnowledge(pid, question); setResults(r.results); setMode(r.mode); } catch (e) { setError((e as Error).message); }
    setBusy("");
  };
  const add = async (files: FileList) => {
    setBusy("upload"); setError(""); setNote("");
    const done: string[] = [];
    for (const f of Array.from(files)) {
      try {
        const d = await api.addKnowledge(pid, f);
        done.push(`${d.name} (${d.chunks} passages${d.embedding_model ? "" : ", keywords only"})`);
        if (d.note) setNote(d.note);
      } catch (e) { setError(`${f.name}: ${(e as Error).message}`); }
    }
    if (done.length) setNote((n) => [`Added ${done.join(", ")}.`, n].filter(Boolean).join(" "));
    if (fileRef.current) fileRef.current.value = "";
    await load();
    setBusy("");
  };
  const remove = async (id: string, name: string) => {
    if (!window.confirm(`Remove ${name} from the knowledge base?`)) return;
    try { await api.deleteKnowledge(pid, id); await load(); } catch (e) { setError((e as Error).message); }
  };
  const reindex = async () => {
    setBusy("reindex"); setError("");
    try {
      const r = await api.reindexKnowledge(pid);
      setStatus(r);
      setNote(`Re-indexed ${r.reindexed} document(s) with ${r.model}.` + (r.failed.length ? ` ${r.failed.length} failed.` : ""));
    } catch (e) { setError((e as Error).message); }
    setBusy("");
  };

  const docs = status?.documents ?? [];
  return (
    <Modal title="Knowledge base" onClose={onClose}>
      <div className="row between">
        <div className="seg" role="tablist">
          {(["ask", "search", "documents"] as const).map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
              {t === "ask" ? "Ask" : t === "search" ? "Search" : `Documents (${docs.length})`}</button>
          ))}
        </div>
        <span className="tiny muted">
          {status ? (status.embedding_model ? `Hybrid search · ${status.embedding_model}` : "Keyword search (no embedding model)") : ""}
          {status && status.chunks ? ` · ${status.chunks} passages` : ""}
        </span>
      </div>

      {(tab === "ask" || tab === "search") && (
        <form className="row gap" onSubmit={(e) => { e.preventDefault(); if (tab === "ask") ask(); else search(); }}>
          <input className="grow" value={question} onChange={(e) => setQuestion(e.target.value)} aria-label="Question"
            placeholder={tab === "ask" ? "Ask the documents, e.g. How long do we keep customer data?" : "Search words or a phrase"} />
          <button className="primary" disabled={!question.trim() || busy !== "" || !docs.length}>
            {busy === "ask" ? "Answering…" : busy === "search" ? "Searching…" : tab === "ask" ? "Ask" : "Search"}</button>
        </form>
      )}
      {tab !== "documents" && !docs.length && <p className="small muted">Add documents first (Documents tab).</p>}

      {tab === "ask" && answer && (
        <div className="kb-answer">
          <div className="tiny muted">{answer.engine === "llm" ? `Answered from ${answer.citations.length} passage(s)` : "No AI model: the best passages"}
            {" "}· {answer.mode} search</div>
          <Markdown text={answer.answer} />
          {answer.citations.length > 0 && (
            <ol className="kb-citations small">
              {answer.citations.map((c) => (
                <li key={c.ref}><b>[{c.ref}]</b> {where(c)}{c.quote && <> — <q>{c.quote}</q></>}</li>
              ))}
            </ol>
          )}
          {answer.follow_ups.length > 0 && (
            <div className="seg-chips">
              {answer.follow_ups.map((f) => <button key={f} className="small" onClick={() => ask(f)}>{f}</button>)}
            </div>
          )}
        </div>
      )}

      {tab === "search" && results && (
        <>
          <p className="tiny muted">{results.length} passage(s) · {mode === "hybrid" ? "keywords and meaning" : "keywords"}</p>
          <ul className="kb-results">
            {results.map((r) => (
              <li key={r.id}>
                <div className="small"><b>{where(r)}</b> <span className="muted tiny">score {r.score}
                  {r.vector_score !== null ? ` · similarity ${r.vector_score}` : ""}</span></div>
                <div className="small kb-text">{r.text}</div>
              </li>
            ))}
          </ul>
        </>
      )}

      {tab === "documents" && (
        <>
          <p className="small muted">PDF, Word, Markdown, text and meeting transcripts (.vtt, .srt). Documents are split into passages
            {status?.embedding_model ? " and embedded for search by meaning" : "; with a Gemini or OpenAI key they are also embedded for search by meaning"}.
            Personal data and secrets are masked before anything is sent to a model. At most {status?.max_documents ?? 200} documents.</p>
          {canEdit && (
            <div className="row gap">
              <input ref={fileRef} type="file" multiple aria-label="Add documents" disabled={busy !== ""}
                accept=".pdf,.docx,.md,.markdown,.txt,.text,.vtt,.srt" onChange={(e) => e.target.files?.length && add(e.target.files)} />
              {busy === "upload" && <span className="small muted"><span className="spinner" aria-hidden /> Reading…</span>}
              {status && status.stale > 0 && (
                <button className="small" onClick={reindex} disabled={busy !== ""}
                  title={`Embed ${status.stale} document(s) with ${status.embedding_model}`}>
                  {busy === "reindex" ? "Indexing…" : `Index ${status.stale} for search by meaning`}</button>
              )}
            </div>
          )}
          {docs.length === 0 ? <p className="small">No documents yet.</p> : (
            <table className="tokens">
              <thead><tr><th>Document</th><th>Source</th><th>Passages</th><th>Index</th><th>Added</th><th /></tr></thead>
              <tbody>
                {docs.map((d) => (
                  <tr key={d.id}>
                    <td>{d.name}</td>
                    <td className="muted">{ORIGIN[d.origin] ?? d.origin} · {d.type}</td>
                    <td>{d.chunks}</td>
                    <td className="muted" title={d.embedding_model || "keyword search only"}>{d.embedding_model ? "keywords + meaning" : "keywords"}</td>
                    <td className="muted">{new Date(d.created_at).toLocaleDateString()} · {d.created_by}</td>
                    <td>{canEdit && <button className="ghost small" onClick={() => remove(d.id, d.name)} aria-label={`Remove ${d.name}`}>✕</button>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
      {note && <p className="small ok">{note}</p>}
      {error && <p className="small error">{error}</p>}
    </Modal>
  );
}
