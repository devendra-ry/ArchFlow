import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Icon } from "../icons";
import { api } from "../api";
import type {
  ArchitecturesCard, ArchOption, ArchReview, ChatTurn, Conversation, ConversationMessage, ProjectState, QuestionCard, User,
} from "../types";
import { useAiSuggestions } from "../aiSuggestions";
import { TYPE_META, TypeIcon } from "./ArchNode";
import { Select } from "./Select";

interface Props {
  pid: string;
  branch: string;
  user: User;
  canvasEmpty: boolean;
  aiAvailable: boolean;
  onApplied: (r: { branch: string; version: number; summary: string[]; state: ProjectState }) => void;
  /** Latest "conversation changed" event from the server (a teammate answered in a shared design chat). */
  remote: { id: string; at: number } | null;
  /** What the project already has, so the starting prompts fit it. */
  context: DesignContext;
}

export interface DesignContext { name: string; description: string; requirements: { id: string; title: string }[]; components: number }

/** Offline only (no AI configured): examples for a project that has nothing yet. */
const EXAMPLES = ["AI customer-support platform on WhatsApp and web", "Food delivery marketplace with live tracking",
  "Internal knowledge assistant over company documents"];

/** Offline starting prompts built from the project (its requirements, canvas, name and description); with AI the
 * starting points come from the LLM (useAiSuggestions). The generic examples are shown only for an empty project. */
export function starters(c: DesignContext): { label: string; prompt: string }[] {
  const out: { label: string; prompt: string }[] = [];
  const reqs = c.requirements;
  if (reqs.length) {
    const list = reqs.slice(0, 25).map((r) => `${r.id} ${r.title}`).join("; ");
    out.push({ label: `Design the system for this project's ${reqs.length} requirement${reqs.length === 1 ? "" : "s"}`,
      prompt: `Design the system for these requirements: ${list}${reqs.length > 25 ? ` (and ${reqs.length - 25} more in the project)` : ""}.` });
  }
  if (c.components > 0) {
    out.push({ label: `Review the current architecture (${c.components} components) and propose improvements`,
      prompt: `Review the current architecture of ${c.name} (${c.components} components): find gaps against the requirements and propose improved options.` });
  }
  const idea = c.description.trim();
  if (idea) out.push({ label: `${c.name}: ${idea}`, prompt: `${c.name}: ${idea}` });
  else if (!reqs.length && !c.components && c.name.trim().length > 3 && !/^(test|untitled|new project|demo)/i.test(c.name.trim())) {
    out.push({ label: `Design ${c.name}`, prompt: `Design ${c.name}.` });
  }
  return out;
}

// Before design chats were stored on the server they lived in this browser only; they are imported once.
const legacyKey = (pid: string, branch: string) => `archflow.chat.${pid}.${branch}`;
const openKey = (pid: string, branch: string) => `archflow.designchat.${pid}.${branch}`;

function readLegacy(pid: string, branch: string): ChatTurn[] {
  try { return JSON.parse(localStorage.getItem(legacyKey(pid, branch)) ?? "[]"); } catch { return []; }
}
function dropLegacy(pid: string, branch: string) {
  try { localStorage.removeItem(legacyKey(pid, branch)); } catch { /* storage unavailable */ }
}
function readOpen(pid: string, branch: string): string | null {
  try { return localStorage.getItem(openKey(pid, branch)); } catch { return null; }
}
function writeOpen(pid: string, branch: string, id: string | null) {
  try { if (id) localStorage.setItem(openKey(pid, branch), id); else localStorage.removeItem(openKey(pid, branch)); }
  catch { /* storage unavailable */ }
}

const toTurn = (m: ConversationMessage): ChatTurn => ({
  role: m.role, content: m.content, card: m.data.card ?? undefined, answer: m.data.answer,
  applied: typeof m.data.applied === "object" ? m.data.applied : undefined,
});

/**
 * Guided design chat: the assistant asks multiple-choice questions, then offers architectures to pick from.
 * Chats are stored on the server (shared with the team, kept across reloads); answers become project memory.
 */
export function DesignChat({ pid, branch, user, canvasEmpty, aiAvailable, onApplied, remote, context }: Props) {
  const [threads, setThreads] = useState<Conversation[]>([]);
  const [conv, setConv] = useState<Conversation | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [engine, setEngine] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const convRef = useRef<Conversation | null>(null);
  convRef.current = conv;
  const busyRef = useRef(false);
  // Starting ideas written by the LLM for this project; the built-in starters are the offline fallback.
  const aiStarters = useAiSuggestions(pid, branch, { mode: "design" }, aiAvailable && !loading && turns.length === 0,
    context.components * 1000 + context.requirements.length);
  // After options are proposed or applied (not while a multiple-choice question is open): next steps from the LLM.
  const last = turns[turns.length - 1];
  const lastUser = [...turns].reverse().find((t) => t.role === "user" && t.content)?.content ?? "";
  const lastAnswer = last?.role === "assistant" && last.card?.type !== "question"
    ? [last.content, ...(last.card?.type === "architectures" ? last.card.options.map((o) => `${o.name}: ${o.summary}`) : [])].join("\n") : "";
  const aiNext = useAiSuggestions(pid, branch, { mode: "design", conversationId: conv?.id, lastQuestion: lastUser, lastAnswer },
    aiAvailable && !busy && !!lastAnswer, context.components * 1000 + context.requirements.length);

  const loadThreads = useCallback(async () => {
    const list = await api.conversations(pid, "design");
    setThreads(list);
    return list;
  }, [pid]);

  const open = useCallback(async (id: string | null) => {
    setError("");
    if (!id) { setConv(null); setTurns([]); writeOpen(pid, branch, null); return; }
    const { messages, ...c } = await api.conversation(pid, id);
    setConv(c);
    setTurns(messages.map(toTurn));
    writeOpen(pid, branch, id);
  }, [pid, branch]);

  // Reopen this branch's chat (a chat follows the draft branch it created); import a browser-only chat once.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setConv(null);
    setTurns([]);
    (async () => {
      try {
        let list = await loadThreads();
        const legacy = readLegacy(pid, branch);
        if (legacy.length) {
          const imported = await api.createConversation(pid, branch, "design", legacy);
          dropLegacy(pid, branch);
          list = [imported, ...list];
          setThreads(list);
          writeOpen(pid, branch, imported.id);
        }
        const wanted = readOpen(pid, branch);
        const pick = list.find((c) => c.id === wanted) ?? list.find((c) => c.branch === branch);
        if (!cancelled && pick) await open(pick.id);
      } catch (e) {
        if (!cancelled) setError((e as Error).message);
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [pid, branch, loadThreads, open]);

  // A teammate took a turn in the chat you have open.
  useEffect(() => {
    if (!remote) return;
    loadThreads().catch(() => {});
    if (remote.id === convRef.current?.id && !busyRef.current) open(remote.id).catch(() => {});
  }, [remote, loadThreads, open]);

  useEffect(() => { listRef.current?.scrollTo({ top: listRef.current.scrollHeight, behavior: "smooth" }); }, [turns, busy]);

  const ensureConv = async (): Promise<Conversation> => {
    if (convRef.current) return convRef.current;
    const c = await api.createConversation(pid, branch, "design");
    setConv(c);
    convRef.current = c;
    writeOpen(pid, branch, c.id);
    setThreads((t) => [c, ...t]);
    return c;
  };

  const step = async (turn: ChatTurn) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setError("");
    setTurns((t) => [...t, turn]);
    let c: Conversation | null = null;
    try {
      c = await ensureConv();
      const r = await api.chat(pid, branch, [turn], user.name, c.id);
      setEngine(r.engine);
      setTurns((t) => [...t, { role: "assistant", content: r.message, card: r.card }]);
      if (!c.title && !turn.answer && turn.content) setConv({ ...c, title: turn.content.slice(0, 60) });
      loadThreads().catch(() => {});
    } catch (e) {
      setError((e as Error).message);
      if (c) open(c.id).catch(() => {}); else setTurns((t) => t.slice(0, -1));
    } finally {
      busyRef.current = false;
      setBusy(false);
    }
  };

  const sendText = (e?: React.FormEvent) => {
    e?.preventDefault();
    const t = text.trim();
    if (!t || busy) return;
    setText("");
    step({ role: "user", content: t });
  };

  const answer = (option_ids: string[], other: string, labels: string[]) =>
    step({ role: "user", content: labels.join(", "), answer: { option_ids, other: other || undefined } });

  const apply = async (card: ArchitecturesCard, option: ArchOption, mode: "replace" | "branch") => {
    const c = convRef.current;
    setBusy(true);
    setError("");
    try {
      const r = await api.chatApply(pid, branch, option, card.requirements, mode, user.name, undefined, c?.id);
      if (c) {
        writeOpen(pid, r.branch, c.id); // continue the same conversation on the draft branch
        if (r.branch === branch) await open(c.id);
      }
      onApplied(r);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const lastIdx = turns.length - 1;

  return (
    <div className="design-chat">
      {!aiAvailable && (
        <div className="dc-banner" role="note">
          <b>AI is off.</b> Questions and architectures come from built-in templates, so they won't adapt to your idea.
          Add <code>GEMINI_API_KEY=…</code> to <code>backend/.env</code> and restart the API to turn it on.
        </div>
      )}
      {threads.length > 0 && (
        <div className="dc-bar">
          <Select value={conv?.id ?? ""} disabled={busy} aria-label="Design chats"
            onChange={(v) => open(v || null).catch((err) => setError((err as Error).message))}
            options={[{ value: "", label: "+ New design chat" }, ...threads.map((t) => ({
              value: t.id, label: (t.title || "Untitled").slice(0, 48),
              hint: `${t.branch}${t.created_by !== user.name ? ` · ${t.created_by}` : ""}`,
            }))]} />
        </div>
      )}
      <div className="dc-list" ref={listRef}>
        {loading && <div className="muted small pulse">Loading…</div>}
        {!loading && turns.length === 0 && (
          <div className="dc-empty">
            <h3>Design with ArchFlow</h3>
            <p className="muted small">Describe what you want to build. I'll ask a few multiple-choice questions, then show
              you architecture options to pick from. Your answers are saved to the project memory.</p>
            {(() => {
              if (aiStarters.items) {
                return (
                  <>
                    <span className="tiny muted dc-starters-label">Start from this project <span className="ai-tag" title="Written by the AI from this project">AI</span></span>
                    {aiStarters.items.map((s) => (
                      <button key={s.label} className="dc-suggest" title={s.why || s.prompt} onClick={() => step({ role: "user", content: s.prompt })}>{s.label}</button>
                    ))}
                  </>
                );
              }
              if (aiStarters.loading) return <span className="tiny muted dc-starters-label pulse">Finding starting points for this project…</span>;
              const fromProject = starters(context);
              return fromProject.length ? (
                <>
                  <span className="tiny muted dc-starters-label">Start from this project</span>
                  {fromProject.map((s) => (
                    <button key={s.label} className="dc-suggest" title={s.prompt} onClick={() => step({ role: "user", content: s.prompt })}>{s.label}</button>
                  ))}
                </>
              ) : (
                <>
                  <span className="tiny muted dc-starters-label">Or try an example</span>
                  {EXAMPLES.map((s) => <button key={s} className="dc-suggest" onClick={() => step({ role: "user", content: s })}>{s}</button>)}
                </>
              );
            })()}
          </div>
        )}
        {turns.map((t, i) => {
          if (t.role === "user") {
            return t.content ? <div key={i} className="dc-msg user">{t.content}</div> : null;
          }
          const active = i === lastIdx && !busy;
          return (
            <div key={i} className="dc-msg bot">
              {t.content && <div>{t.content}</div>}
              {t.card?.type === "question" && (
                <QuestionView card={t.card} active={active} answered={turns[i + 1]?.answer}
                  onAnswer={answer} onSkip={() => step({ role: "user", content: "Skip — show me the architecture options" })} />
              )}
              {t.card?.type === "architectures" && (
                <ArchitecturesView card={t.card} disabled={busy} canvasEmpty={canvasEmpty}
                  onApply={(o, mode) => apply(t.card as ArchitecturesCard, o, mode)} />
              )}
            </div>
          );
        })}
        {!busy && lastAnswer && (aiNext.items?.length || aiNext.loading) ? (
          <div className="cmd-followups dc-next" aria-label="Next steps">
            <span className="tiny muted">{aiNext.loading ? "Thinking of next steps…" : "Next"}</span>
            {(aiNext.items ?? []).map((s) => (
              <button key={s.label} type="button" className={s.kind === "reply" ? "reply-chip" : "followup-chip"} title={s.why || s.prompt}
                onClick={() => step({ role: "user", content: s.prompt })}>{s.label}</button>
            ))}
          </div>
        ) : null}
        {busy && <div className="dc-msg bot muted pulse">Thinking…</div>}
        {error && <div className="error small pad">{error}</div>}
      </div>
      <form className="dc-input" onSubmit={sendText}>
        <textarea rows={2} value={text} placeholder={turns.length ? "Reply, add detail, or say “show options”…" : "Describe your product or system…"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendText(); } }} />
        <div className="row between">
          <span className="muted tiny">{engine === "offline" ? "Offline interviewer (no LLM key)" : engine ? "AI interviewer" : ""}</span>
          <span className="row gap">
            {turns.length > 0 && <button type="button" className="small" disabled={busy} onClick={() => open(null)}>New</button>}
            <button className="primary small" disabled={!text.trim() || busy}>Send</button>
          </span>
        </div>
      </form>
    </div>
  );
}

function QuestionView({ card, active, answered, onAnswer, onSkip }: {
  card: QuestionCard; active: boolean; answered?: { option_ids: string[]; other?: string };
  onAnswer: (ids: string[], other: string, labels: string[]) => void; onSkip: () => void;
}) {
  const [picked, setPicked] = useState<string[]>([]);
  const [other, setOther] = useState("");
  const [showOther, setShowOther] = useState(false);

  if (answered) {
    const labels = card.options.filter((o) => answered.option_ids.includes(o.id)).map((o) => o.label);
    if (answered.other) labels.push(answered.other);
    return (
      <div className="q-card done">
        <div className="q-title">{card.question}</div>
        <div className="row gap">{labels.map((l) => <span key={l} className="chip on">{l}</span>)}</div>
      </div>
    );
  }

  const toggle = (id: string) => {
    if (!active) return;
    if (card.multi) setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
    else {
      const o = card.options.find((x) => x.id === id)!;
      onAnswer([id], "", [o.label]);  // single choice submits immediately
    }
  };
  const submit = () => {
    const labels = card.options.filter((o) => picked.includes(o.id)).map((o) => o.label);
    onAnswer(picked, other.trim(), other.trim() ? [...labels, other.trim()] : labels);
  };
  const recommended = card.options.find((o) => o.recommended);

  return (
    <div className={`q-card ${active ? "" : "inactive"}`}>
      <div className="row between">
        <div className="q-title">{card.question}</div>
        {card.step && <span className="muted tiny">{card.step}{card.total ? `/${card.total}` : ""}</span>}
      </div>
      {card.why && <div className="muted tiny">{card.why}</div>}
      <div className="q-options" role={card.multi ? "group" : "radiogroup"} aria-label={card.question}>
        {card.options.map((o) => {
          const on = picked.includes(o.id);
          return (
            <button key={o.id} type="button" disabled={!active}
              className={`q-option ${on ? "on" : ""}`} role={card.multi ? "checkbox" : "radio"} aria-checked={on}
              onClick={() => toggle(o.id)}>
              <span className={`q-mark ${card.multi ? "box" : "dot"}`} aria-hidden>{on ? <Icon as={Check} size={12} /> : ""}</span>
              <span className="q-text">
                <span className="q-label">{o.label}{o.recommended && <span className="badge">Recommended</span>}</span>
                {o.description && <span className="q-desc">{o.description}</span>}
              </span>
            </button>
          );
        })}
      </div>
      {active && (
        <>
          {showOther ? (
            <input autoFocus placeholder="Your own answer…" value={other} onChange={(e) => setOther(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && other.trim()) submit(); }} />
          ) : (
            <button type="button" className="link small" onClick={() => setShowOther(true)}>Other…</button>
          )}
          <div className="row gap q-actions">
            {(card.multi || other.trim()) && (
              <button type="button" className="primary small" disabled={!picked.length && !other.trim()} onClick={submit}>
                Continue{picked.length ? ` (${picked.length})` : ""}
              </button>
            )}
            {recommended && !picked.length && !other && (
              <button type="button" className="small" onClick={() => onAnswer([recommended.id], "", [recommended.label])}>Use recommended</button>
            )}
            <button type="button" className="small ghost" onClick={onSkip}>Skip to options</button>
          </div>
        </>
      )}
    </div>
  );
}

/** "Reviewed: fixed N issues" with an expandable list of the changes and anything still open. */
function ReviewNote({ review }: { review: ArchReview }) {
  const key = (i: { code: string; message: string }) => `${i.code}|${i.message}`;
  const after = new Set(review.issues_after.map(key));
  const fixed = review.issues_before.filter((i) => !after.has(key(i))).length;
  const open = review.issues_after;
  const by = review.engine === "llm+rules" ? "AI + rules" : "rules";
  const label = fixed > 0
    ? `Reviewed: fixed ${fixed} issue${fixed === 1 ? "" : "s"}`
    : review.fixes.length ? `Reviewed: ${review.fixes.length} improvement${review.fixes.length === 1 ? "" : "s"}` : "Reviewed: no issues found";
  if (!review.fixes.length && !open.length) return <div className="arch-review tiny ok"><Icon as={Check} size={12} /> {label}</div>;
  return (
    <details className="arch-review tiny">
      <summary>
        <span className={open.some((i) => i.severity === "error") ? "warn" : "ok"}><Icon as={Check} size={12} /> {label}</span>
        {open.length > 0 && <span className="muted"> · {open.length} open</span>}
        <span className="muted"> · by {by}</span>
      </summary>
      {review.fixes.length > 0 && <ul>{review.fixes.map((f, k) => <li key={k}>{f}</li>)}</ul>}
      {open.length > 0 && (
        <>
          <div className="muted">Still open:</div>
          <ul>{open.map((i, k) => <li key={k} className={`sev-${i.severity}`}>{i.message}</li>)}</ul>
        </>
      )}
    </details>
  );
}

function ArchitecturesView({ card, disabled, canvasEmpty, onApply }: {
  card: ArchitecturesCard; disabled: boolean; canvasEmpty: boolean;
  onApply: (o: ArchOption, mode: "replace" | "branch") => void;
}) {
  const [open, setOpen] = useState<string | null>(card.options[0]?.id ?? null);
  const [confirm, setConfirm] = useState<string | null>(null);
  return (
    <div className="arch-options">
      <div className="q-title">{card.title}</div>
      {card.options.map((o) => {
        const isOpen = open === o.id;
        return (
          <div key={o.id} className={`arch-opt ${isOpen ? "open" : ""}`}>
            <button type="button" className="arch-head" onClick={() => setOpen(isOpen ? null : o.id)} aria-expanded={isOpen}>
              <span className="arch-name">{o.name}{o.recommended && <span className="badge">Recommended</span>}</span>
              <span className="row gap">
                <span className={`lvl l-${o.cost}`}>cost {o.cost}</span>
                <span className={`lvl l-${o.complexity}`}>complexity {o.complexity}</span>
              </span>
            </button>
            {isOpen && (
              <div className="arch-body">
                <p className="small">{o.summary}</p>
                {o.best_for && <p className="tiny muted">Best for: {o.best_for}</p>}
                {o.review && <ReviewNote review={o.review} />}
                <div className="pc">
                  <ul className="pros small">{o.pros.map((p) => <li key={p}>{p}</li>)}</ul>
                  <ul className="cons small">{o.cons.map((c) => <li key={c}>{c}</li>)}</ul>
                </div>
                <div className="arch-comps">
                  {o.components.map((c) => (
                    <span key={c.key} className="comp-chip" style={{ ["--accent" as string]: TYPE_META[c.type]?.color ?? "#888" }}
                      title={c.technology}>
                      {TYPE_META[c.type] && <TypeIcon type={c.type} size={14} />} {c.name}
                    </span>
                  ))}
                </div>
                <div className="row gap q-actions">
                  {confirm === o.id ? (
                    <>
                      <span className="tiny warn">Replaces the components on this canvas (requirements are kept).</span>
                      <button className="small danger" disabled={disabled} onClick={() => { setConfirm(null); onApply(o, "replace"); }}>Replace</button>
                      <button className="small" onClick={() => setConfirm(null)}>Cancel</button>
                    </>
                  ) : (
                    <>
                      <button className="primary small" disabled={disabled}
                        onClick={() => (canvasEmpty ? onApply(o, "replace") : setConfirm(o.id))}>Apply to canvas</button>
                      <button className="small" disabled={disabled} onClick={() => onApply(o, "branch")}
                        title="Create a draft branch with this architecture">Open as draft</button>
                    </>
                  )}
                </div>
              </div>
            )}
          </div>
        );
      })}
      {card.requirements.length > 0 && (
        <p className="tiny muted">{card.requirements.length} requirements from this conversation will be added with the architecture.</p>
      )}
    </div>
  );
}
