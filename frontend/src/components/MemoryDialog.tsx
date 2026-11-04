import { useState } from "react";
import { api } from "../api";
import { useEscape } from "../useEscape";
import { Select, plainOptions } from "./Select";
import { MEMORY_CATEGORIES, type MemoryCategory, type MemoryItem } from "../types";

interface Props {
  pid: string;
  branch: string;
  items: MemoryItem[];
  readOnly: boolean;
  onChanged: () => void;
  onClose: () => void;
}

const SOURCE: Record<MemoryItem["source"], string> = {
  user: "added by", ai: "learned by AI from a conversation", "design-chat": "answered in the design chat by", mcp: "added over MCP by",
  import: "imported from a document by",
};

/** Project memory: facts every AI answer, edit and design takes into account. */
export function MemoryDialog({ pid, branch, items, readOnly, onChanged, onClose }: Props) {
  const escRef = useEscape(onClose);
  const [text, setText] = useState("");
  const [category, setCategory] = useState<MemoryCategory>("constraint");
  const [pinned, setPinned] = useState(false);
  const [filter, setFilter] = useState("");
  const [editing, setEditing] = useState<{ id: string; text: string; category: MemoryCategory } | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError("");
    try { await fn(); onChanged(); return true; }
    catch (e) { setError((e as Error).message); return false; }
    finally { setBusy(false); }
  };

  const add = async (e: React.FormEvent) => {
    e.preventDefault();
    if (await run(() => api.addMemory(pid, branch, text.trim(), category, pinned))) { setText(""); setPinned(false); }
  };

  const q = filter.trim().toLowerCase();
  const shown = items.filter((m) => !q || m.text.toLowerCase().includes(q) || m.category.includes(q));
  const learned = items.filter((m) => m.source === "ai").length;

  return (
    <div className="modal-backdrop" onClick={onClose} ref={escRef}>
      <div className="modal wide memory" role="dialog" aria-label="Project memory" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Project memory</h3>
          <button className="ghost" onClick={onClose} aria-label="close">✕</button>
        </div>
        <p className="muted small">
          Facts the team has settled. Every AI answer, canvas edit and design-chat question takes them into account.
          Pinned facts are always included; the rest are included newest first up to a size limit.
          {learned > 0 && <> ArchFlow learned {learned} of these from conversations. Remove any that are wrong.</>}
        </p>
        {!readOnly && (
          <form className="mem-add" onSubmit={add}>
            <input value={text} maxLength={240} onChange={(e) => setText(e.target.value)}
              placeholder="e.g. “All customer data stays in the EU”, “Team knows Go, not Java”, “Budget under $2k/month”" />
            <Select value={category} onChange={setCategory} aria-label="Category" options={plainOptions(MEMORY_CATEGORIES)} />
            <label className="row gap small"><input type="checkbox" checked={pinned} onChange={(e) => setPinned(e.target.checked)} /> Pin</label>
            <button className="primary" disabled={!text.trim() || busy}>Remember</button>
          </form>
        )}
        {error && <div className="error small">{error}</div>}
        {items.length > 6 && <input className="mem-filter" placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} />}
        {items.length === 0 && (
          <p className="muted small">Nothing yet. Add facts here, answer the design chat's questions, or just talk to the
            assistant: ArchFlow remembers what you settle (when an LLM is configured).</p>
        )}
        <ul className="mem-list">
          {shown.map((m) => (
            <li key={m.id} className={m.pinned ? "pinned" : ""}>
              {editing?.id === m.id ? (
                <form className="mem-edit" onSubmit={async (e) => {
                  e.preventDefault();
                  if (await run(() => api.updateMemory(pid, branch, m.id, { text: editing.text.trim(), category: editing.category }))) setEditing(null);
                }}>
                  <input autoFocus value={editing.text} maxLength={240} onChange={(e) => setEditing({ ...editing, text: e.target.value })}
                    onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); setEditing(null); } }} aria-label="Fact" />
                  <Select size="sm" value={editing.category} onChange={(v) => setEditing({ ...editing, category: v })} aria-label="Category"
                    options={plainOptions(MEMORY_CATEGORIES)} />
                  <button className="small primary" disabled={!editing.text.trim() || busy}>Save</button>
                  <button type="button" className="small" onClick={() => setEditing(null)}>Cancel</button>
                </form>
              ) : (
                <>
                  <span className={`mem-cat ${m.category}`}>{m.category}</span>
                  <div className="mem-body">
                    <div>{m.text}</div>
                    <div className="muted tiny">
                      {m.source === "ai" ? SOURCE.ai : `${SOURCE[m.source]} ${m.created_by}`} · {new Date(m.created_at).toLocaleDateString()}
                    </div>
                  </div>
                  {!readOnly && (
                    <span className="mem-actions">
                      <button className={`ghost tiny-btn ${m.pinned ? "on" : ""}`} disabled={busy} title={m.pinned ? "Unpin" : "Pin: always include"}
                        onClick={() => run(() => api.updateMemory(pid, branch, m.id, { pinned: !m.pinned }))}>{m.pinned ? "★" : "☆"}</button>
                      <button className="ghost tiny-btn" disabled={busy} title="Edit"
                        onClick={() => setEditing({ id: m.id, text: m.text, category: m.category })}>✎</button>
                      <button className="ghost tiny-btn" disabled={busy} title="Forget this fact" aria-label={`Forget ${m.text}`}
                        onClick={() => run(() => api.deleteMemory(pid, branch, m.id))}>✕</button>
                    </span>
                  )}
                  {readOnly && m.pinned && <span className="muted" title="Pinned">★</span>}
                </>
              )}
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
