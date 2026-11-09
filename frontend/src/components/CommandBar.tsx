import { useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, ChevronUp, Icon, Pencil, RefreshCw, Sparkles, X } from "../icons";
import { ArrowUp, Brain, History, Maximize2, Minimize2, PanelLeft, PanelRight, PictureInPicture2, Plus, type LucideIcon } from "lucide-react";
import type { Assistant } from "../assistant";
import type { AiSuggestion, ConversationMessage } from "../types";
import { Markdown, type EntityLinks } from "./Markdown";

interface Props {
  assistant: Assistant;
  selection: string[];
  aiBusy: string | null;
  links: EntityLinks;
  memoryCount: number;
  onOpenMemory: () => void;
  /** Your user id and whether you are an org admin: who may rename, clear or delete a thread. */
  meId: string;
  isAdmin: boolean;
  userName: string;
  /** Viewer/reviewer: only "Ask" (questions about the project) is available. */
  readOnly?: boolean;
  /** Design (guided: questions, then architecture options), Edit (change the canvas) or Ask. */
  mode: ChatMode;
  onMode: (mode: ChatMode) => void;
  /** Where the panel lives: docked in the left or right sidebar, or floating over the canvas. */
  dock: ChatDock;
  onDock: (dock: ChatDock) => void;
  /** The guided design conversation, shown in Design mode (absent for read-only roles). */
  designSlot?: React.ReactNode;
  /** For the current mode: starting prompts (empty conversation) or follow-ups to the last answer, written by the
   * LLM for this project (aiSuggestions.ts), or the offline ones built from the canvas (suggestions.ts). */
  suggestions: AiSuggestion[];
  suggestionsSource: "ai" | "offline";
  suggestionsLoading?: boolean;
  /** Text handed over from elsewhere (e.g. "Fix with AI" in Health); it fills the box for the person to review. */
  prefill?: { text: string; at: number } | null;
}

export type ChatMode = "design" | "command" | "ask";
export type ChatDock = "left" | "float" | "right";
const DOCKS: { id: ChatDock; icon: LucideIcon; label: string }[] = [
  { id: "left", icon: PanelLeft, label: "Dock left" }, { id: "float", icon: PictureInPicture2, label: "Float over the canvas" },
  { id: "right", icon: PanelRight, label: "Dock right" },
];
const ic = (Cmp: LucideIcon, size = 14) => <Cmp size={size} strokeWidth={2} aria-hidden focusable={false} />;


function ago(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

/** The project assistant: ask questions or edit the canvas in a shared, remembered conversation. */
export function CommandBar({ assistant: a, selection, aiBusy, links, memoryCount, onOpenMemory, meId, isAdmin,
  userName, readOnly = false, mode: modeProp, onMode: setMode, dock, onDock, designSlot, suggestions, suggestionsSource,
  suggestionsLoading = false, prefill = null }: Props) {
  const [text, setText] = useState("");
  const mode: ChatMode = readOnly ? "ask" : modeProp === "design" && !designSlot ? "command" : modeProp;
  const docked = dock !== "float";
  const [expanded, setExpanded] = useState(true);
  const [tall, setTall] = useState(false);
  const [full, setFull] = useState(false);
  const [history, setHistory] = useState(false);
  const [showSummary, setShowSummary] = useState(false);
  const [editingTitle, setEditingTitle] = useState<string | null>(null);
  const [copied, setCopied] = useState<number | null>(null);
  const logRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const conv = a.current;
  const owns = (createdById: string | null) => createdById === meId || isAdmin;
  const hasLog = a.messages.length > 0 || a.draft !== null || !!a.error;
  const showLog = hasLog || full || docked;

  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" });
  }, [a.messages, a.draft?.answer, a.draft === null, a.error]);
  useEffect(() => { setShowSummary(false); setEditingTitle(null); }, [conv?.id]);
  // Full screen: Esc leaves it (after closing the history menu, if open).
  useEffect(() => {
    if (!full) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || editingTitle !== null || document.querySelector(".modal-backdrop")) return;
      if (history) setHistory(false); else setFull(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [full, history, editingTitle]);
  useEffect(() => { if (full) { setExpanded(true); inputRef.current?.focus(); } }, [full]);
  useEffect(() => {
    if (!prefill) return;
    setText(prefill.text); setExpanded(true);
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }, [prefill?.at]); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const t = text.trim();
    if (!t || a.busy) return;
    setText("");
    setExpanded(true);
    setHistory(false);
    const ok = mode === "command" ? await a.command(t, selection) : await a.ask(t);
    if (!ok) setText(t); // not sent: keep what was typed
  };

  /** Send right away: an answer tapped in a question bubble, or a follow-up question in Ask mode. (Suggested edits are
   * only put in the box, so the person checks them before anything changes on the canvas.) */
  const tap = async (t: string, as: ChatMode = mode) => {
    if (a.busy) return;
    if (as !== mode) setMode(as);
    setExpanded(true);
    const ok = as === "command" ? await a.command(t, selection) : await a.ask(t);
    if (!ok) setText(t);
  };

  const copy = async (m: ConversationMessage) => {
    try { await navigator.clipboard.writeText(m.content); setCopied(m.seq); window.setTimeout(() => setCopied(null), 1200); }
    catch { /* clipboard unavailable */ }
  };

  const lastAnswer = [...a.messages].reverse().find((m) => m.role === "assistant");
  const canRegenerate = !a.busy && lastAnswer?.mode === "ask" && a.messages[a.messages.length - 1] === lastAnswer;

  const renderAssistant = (m: ConversationMessage, isLast: boolean) => (
    <div key={m.seq} className="cmd-entry bot">
      {m.content && <Markdown text={m.content} links={links} />}
      {m.data.summary && m.data.summary.length > 0 && (
        <ul className="small cmd-changes">{m.data.summary.map((s, i) => <li key={i}>{s}</li>)}</ul>
      )}
      {m.data.questions && m.data.questions.length > 0 && (
        <div className="ask-bubbles" aria-label="Questions from ArchFlow">
          {m.data.questions.map((q, i) => {
            const options = m.data.question_options?.[q] ?? [];
            return (
              <div key={i} className="ask-bubble">
                <div className="ask-q small"><span className="ask-avatar" aria-hidden><Icon as={Sparkles} size={12} /></span>{q}</div>
                <div className="ask-options">
                  {options.map((o) => (
                    <button key={o} type="button" className="reply-chip" disabled={a.busy || !isLast}
                      onClick={() => tap(`${q} ${o}`, "command")} title="Answer and continue">{o}</button>
                  ))}
                  <button type="button" className="reply-chip other" disabled={a.busy}
                    onClick={() => { setText(`${q} `); setMode("command"); inputRef.current?.focus(); }}>{options.length ? "Other…" : "Answer…"}</button>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {m.data.error && <div className="error small">{m.data.error}</div>}
      <div className="cmd-meta">
        {m.mode === "command" && m.data.engine && <span className="muted tiny">{m.data.engine === "offline" ? "offline parser" : "AI edit"}</span>}
        {m.content && <button className="ghost tiny-btn" onClick={() => copy(m)} title="Copy answer">{copied === m.seq ? "Copied" : "Copy"}</button>}
        {isLast && canRegenerate && <button className="ghost tiny-btn" onClick={a.regenerate} title="Answer again"><Icon as={RefreshCw} size={12} /> {m.data.error ? "Retry" : "Regenerate"}</button>}
      </div>
      {isLast && !a.busy && !m.data.error && suggestionsSource === "ai" && (suggestions.length > 0 || suggestionsLoading) && (
        <div className="cmd-followups" aria-label={suggestions.some((s) => s.kind === "reply") ? "Quick replies" : "Suggested follow-ups"}>
          <span className="tiny muted">{suggestionsLoading ? "Thinking of follow-ups…"
            : suggestions.some((s) => s.kind === "reply") ? "Reply" : mode === "command" ? "Next edits" : "You could ask"}</span>
          {suggestions.map((s) => (
            <button key={s.label} type="button" className={s.kind === "reply" ? "reply-chip" : "followup-chip"} title={s.why || s.prompt}
              onClick={() => (mode === "command" ? (setText(s.prompt), inputRef.current?.focus()) : tap(s.prompt, "ask"))}>{s.label}</button>
          ))}
        </div>
      )}
    </div>
  );

  const renderUser = (m: ConversationMessage, key: string | number) => (
    <div key={key} className={`cmd-entry you ${m.mode}`}>
      <div className="cmd-bubble">
        {m.author && m.author !== userName && <span className="cmd-author">{m.author}</span>}
        {m.mode === "command" && <span className="cmd-tag">Edit</span>}
        {m.content}
      </div>
    </div>
  );

  const top = (
    <div className="cmd-top">
      <div className="seg" role="tablist" aria-label="Assistant mode">
        {designSlot && <button type="button" role="tab" aria-selected={mode === "design"} className={mode === "design" ? "on" : ""}
          onClick={() => setMode("design")} title="Start from an idea: ArchFlow asks a few questions, then proposes architectures">Design</button>}
        {!readOnly && <button type="button" role="tab" aria-selected={mode === "command"} className={mode === "command" ? "on" : ""}
          onClick={() => setMode("command")} title="Tell ArchFlow what to change on the canvas">Edit</button>}
        <button type="button" role="tab" aria-selected={mode === "ask"} className={mode === "ask" ? "on" : ""}
          onClick={() => setMode("ask")} title="Ask questions about the project">Ask</button>
      </div>
      <span className="spacer" />
      {!full && (
        <div className="dock-switch" role="group" aria-label="Assistant position">
          {DOCKS.map((d) => (
            <button key={d.id} type="button" className={`ghost tiny-btn ${dock === d.id ? "on" : ""}`} title={d.label} aria-label={d.label}
              aria-pressed={dock === d.id} onClick={() => onDock(d.id)}>{ic(d.icon)}</button>
          ))}
        </div>
      )}
      {!docked && hasLog && !full && mode !== "design" && (
        <button type="button" className="ghost tiny-btn" onClick={() => setExpanded(!expanded)} aria-label="Toggle conversation"
          title={expanded ? "Collapse" : "Expand"}>{expanded ? <Icon as={ChevronDown} size={14} /> : <Icon as={ChevronUp} size={14} />}</button>
      )}
    </div>
  );

  if (mode === "design" && designSlot) {
    return (
      <div className={`command-bar design-mode ${docked ? `docked ${dock}` : "tall"}`}>
        {top}
        <div className="cmd-design">{designSlot}</div>
        {aiBusy && <div className="ai-busy small"><Icon as={Sparkles} size={12} /> {aiBusy}</div>}
      </div>
    );
  }

  return (
    <div className={`command-bar ${full ? "full" : docked ? `docked ${dock}` : tall && expanded ? "tall" : ""}`}
      role={full ? "dialog" : undefined} aria-modal={full || undefined} aria-label={full ? "Conversation" : undefined}>
      {top}
      {history && (
        <div className="cmd-history" role="dialog" aria-label="Conversation history">
          <div className="row between">
            <b className="small">Conversations</b>
            <button className="small" onClick={() => { a.open(null); setHistory(false); inputRef.current?.focus(); }}>+ New chat</button>
          </div>
          {a.threads.length === 0 && <p className="muted small">No conversations yet. Ask something below to start one.</p>}
          <ul>
            {a.threads.map((t) => (
              <li key={t.id} className={t.id === conv?.id ? "on" : ""}>
                <button className="cmd-thread" onClick={() => { a.open(t.id); setHistory(false); setExpanded(true); }}>
                  <span className="cmd-thread-title">{t.title || "Untitled conversation"}</span>
                  <span className="muted tiny">{t.created_by} · {t.message_count} message{t.message_count === 1 ? "" : "s"} · {ago(t.updated_at)}</span>
                </button>
                {owns(t.created_by_id) && (
                  <button className="ghost tiny-btn" title="Delete conversation" aria-label={`Delete ${t.title}`}
                    onClick={() => { if (window.confirm(`Delete “${t.title || "this conversation"}” for everyone?`)) a.remove(t.id); }}><Icon as={X} size={12} /></button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
      {(expanded || docked) && (conv || showLog) && (
        <div className="cmd-head">
          {editingTitle !== null ? (
            <form className="row gap grow" onSubmit={(e) => { e.preventDefault(); a.rename(editingTitle); setEditingTitle(null); }}>
              <input autoFocus value={editingTitle} maxLength={120} onChange={(e) => setEditingTitle(e.target.value)}
                onKeyDown={(e) => e.key === "Escape" && setEditingTitle(null)} aria-label="Conversation title" />
              <button className="small primary" disabled={!editingTitle.trim()}>Save</button>
            </form>
          ) : (
            <span className="cmd-title" title={conv ? `Started by ${conv.created_by}` : undefined}>
              {conv?.title || "New conversation"}
              {conv && owns(conv.created_by_id) && <button className="ghost tiny-btn" onClick={() => setEditingTitle(conv.title)} title="Rename" aria-label="Rename"><Icon as={Pencil} size={12} /></button>}
            </span>
          )}
          <span className="spacer" />
          {conv && owns(conv.created_by_id) && a.messages.length > 0 && (
            <button className="ghost tiny-btn" disabled={a.busy} title="Remove every message in this conversation"
              onClick={() => { if (window.confirm("Clear this conversation for everyone?")) a.clear(); }}>Clear</button>
          )}
          <button className="ghost tiny-btn" onClick={() => { a.open(null); inputRef.current?.focus(); }} disabled={a.busy}>{ic(Plus, 12)} New chat</button>
          {!full && !docked && <button className="ghost tiny-btn" onClick={() => setTall(!tall)} title={tall ? "Smaller" : "Taller"}>{tall ? <Icon as={ChevronDown} size={14} /> : <Icon as={ChevronUp} size={14} />}</button>}
        </div>
      )}
      {(expanded || docked) && showLog && (
        <div className="cmd-log" ref={logRef} aria-live="polite">
          {!hasLog && (
            <div className="cmd-empty muted">
              <h3>{mode === "command" ? "Edit with ArchFlow" : "Ask ArchFlow"}</h3>
              <p className="small">{mode === "command"
                ? "Tell ArchFlow what to change: it edits the canvas and lists every change. Select components first to focus the edit."
                : "Ask anything about this project: requirements, components, decisions or trade-offs."} The conversation is saved
                and shared with your team.</p>
              {(suggestions.length > 0 || suggestionsLoading) && (
                <div className="cmd-suggest">
                  <span className="tiny muted dc-starters-label">
                    {suggestionsLoading && !suggestions.length ? "Finding the most useful next steps…"
                      : selection.length ? "For your selection and this project" : "For this project"}
                    {suggestionsSource === "ai" && <span className="ai-tag" title="Written by the AI from this project">AI</span>}
                  </span>
                  {suggestions.map((s) => (
                    <button key={s.label} type="button" className="dc-suggest" title={s.why || undefined}
                      onClick={() => { setText(s.prompt); inputRef.current?.focus(); }}>{s.label}</button>
                  ))}
                </div>
              )}
            </div>
          )}
          {conv?.summary && (
            <div className="cmd-summary small">
              <button className="link" onClick={() => setShowSummary(!showSummary)}>
                {showSummary ? <Icon as={ChevronDown} size={12} /> : <Icon as={ChevronRight} size={12} />} Earlier messages are summarized for the AI
              </button>
              {showSummary && <Markdown text={conv.summary} className="muted" />}
            </div>
          )}
          {a.messages.map((m, i) => m.role === "user" ? renderUser(m, m.seq) : renderAssistant(m, i === a.messages.length - 1))}
          {a.draft && (
            <>
              {!a.draft.regenerate && renderUser(a.draft.user, "draft-user")}
              <div className="cmd-entry bot">
                {a.draft.answer
                  ? <Markdown text={a.draft.answer} links={links} className="streaming" />
                  : <div className="muted small pulse">{a.draft.mode === "command" ? "AI is editing the canvas…" : "Thinking…"}</div>}
              </div>
            </>
          )}
          {a.error && (
            <div className="error small row between">
              <span>{a.error}</span>
              <button className="ghost tiny-btn" onClick={a.dismissError} aria-label="Dismiss"><Icon as={X} size={12} /></button>
            </div>
          )}
        </div>
      )}
      <form className="cmd-form" onSubmit={submit}>
        <input
          ref={inputRef}
          list="cmd-suggestions"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={mode === "command"
            ? (selection.length ? `Tell ArchFlow what to change (${selection.length} selected)…` : "Tell ArchFlow what to change, or describe your product idea…")
            : a.messages.length ? "Ask a follow-up…" : suggestions[0] ? `Ask about the project, e.g. “${suggestions[0].label}”` : "Ask about the project…"}
        />
        <datalist id="cmd-suggestions">{suggestions.map((s) => <option key={s.label} value={s.prompt} />)}</datalist>
        <button className="primary" disabled={!text.trim() || a.busy}>{mode === "command" ? "Apply" : "Ask"} {ic(ArrowUp)}</button>
        <button type="button" className={`ghost ${history ? "on" : ""}`} onClick={() => { setHistory(!history); a.refreshThreads(); }}
          title="Conversation history" aria-label="Conversation history">{ic(History, 16)}</button>
        <button type="button" className={`ghost ${full ? "on" : ""}`} onClick={() => setFull(!full)}
          title={full ? "Exit full screen (Esc)" : "Full screen"} aria-label={full ? "Exit full screen" : "Full screen"}
          aria-pressed={full}>{ic(full ? Minimize2 : Maximize2, 16)}</button>
        <button type="button" className="ghost mem-btn" onClick={onOpenMemory}
          title="Project memory: facts every AI answer, edit and design takes into account">{ic(Brain, 16)} {memoryCount}</button>
      </form>
      {aiBusy && <div className="ai-busy small"><Icon as={Sparkles} size={12} /> {aiBusy}</div>}
    </div>
  );
}
