import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ArrowLeftRight, Box, Check, Icon, ListChecks, Scale, Target } from "../icons";
import { api } from "../api";
import type { Comment, CommentTarget, Member, ProjectState } from "../types";

interface Props {
  pid: string;
  branch: string;
  comments: Comment[];
  members: Member[];
  meId: string;
  state: ProjectState | null;
  /** The component selected on the canvas: new comments go on it, and the list can be narrowed to it. */
  selectedNode: string | null;
  /** The one selected connection, if exactly one is selected: new comments go on it. */
  selectedEdge?: string | null;
  /** A requirement opened for discussion from the Requirements panel: new comments go on it until cleared. */
  focusRequirement?: string | null;
  onClearRequirement?: () => void;
  onShowRequirement?: (id: string) => void;
  /** A thread to scroll to and highlight (from a notification or a canvas badge). */
  focusThread: string | null;
  canComment: boolean;
  canResolveAny: boolean;
  isAdmin: boolean;
  onChanged: () => void;
  onShowNode: (id: string) => void;
  toast: (text: string) => void;
}

function ago(iso: string) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

/** Mentioned people whose @Name is still in the text. */
function keptMentions(text: string, picked: Map<string, string>): string[] {
  return [...picked].filter(([, name]) => text.includes(`@${name}`)).map(([id]) => id);
}

/** A comment's text with @mentions of known people highlighted. */
function Body({ text, names }: { text: string; names: string[] }) {
  if (!names.length) return <>{text}</>;
  const esc = names.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).sort((a, b) => b.length - a.length);
  const parts = text.split(new RegExp(`(@(?:${esc.join("|")}))`, "g"));
  return <>{parts.map((p, i) => (i % 2 ? <span key={i} className="mention">{p}</span> : p))}</>;
}

/** A text box that suggests teammates after "@" and remembers whom you picked. */
export function MentionInput({ members, meId, value, onChange, onSubmit, placeholder, autoFocus, label, submitLabel, onCancel }: {
  members: Member[]; meId: string; value: string; onChange: (text: string, mentions: string[]) => void;
  onSubmit: () => void; placeholder: string; autoFocus?: boolean; label: string; submitLabel: string; onCancel?: () => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const picked = useRef(new Map<string, string>());
  const caretAfterUpdate = useRef<number | null>(null);
  const [query, setQuery] = useState<{ text: string; start: number } | null>(null);
  const [active, setActive] = useState(0);
  const matches = useMemo(() => {
    if (!query) return [];
    const q = query.text.toLowerCase();
    return members.filter((m) => m.user_id !== meId && (m.name.toLowerCase().includes(q) || m.email.toLowerCase().startsWith(q))).slice(0, 6);
  }, [query, members, meId]);

  const update = (text: string, caret: number) => {
    const m = /(^|\s)@([^\s@]{0,30})$/.exec(text.slice(0, caret));
    setQuery(m ? { text: m[2], start: caret - m[2].length - 1 } : null);
    setActive(0);
    onChange(text, keptMentions(text, picked.current));
  };
  const pick = (m: Member) => {
    if (!query) return;
    const el = ref.current!;
    const caret = el.selectionStart;
    const text = `${value.slice(0, query.start)}@${m.name} ${value.slice(caret)}`;
    picked.current.set(m.user_id, m.name);
    setQuery(null);
    onChange(text, keptMentions(text, picked.current));
    // Place the caret after the mention in the same commit that writes the new text (layout effect below). With
    // requestAnimationFrame, a key typed before the next frame landed at the end of the box.
    caretAfterUpdate.current = query.start + m.name.length + 2;
    el.focus();
  };
  useLayoutEffect(() => {
    const pos = caretAfterUpdate.current;
    if (pos === null || !ref.current) return;
    caretAfterUpdate.current = null;
    ref.current.setSelectionRange(pos, pos);
  }, [value]);

  return (
    <form className="mention-form" onSubmit={(e) => { e.preventDefault(); if (value.trim()) onSubmit(); }}>
      <div className="mention-box">
        <textarea ref={ref} value={value} placeholder={placeholder} aria-label={label} autoFocus={autoFocus} rows={2}
          onChange={(e) => update(e.target.value, e.target.selectionStart)}
          onKeyDown={(e) => {
            if (matches.length) {
              if (e.key === "ArrowDown") { e.preventDefault(); setActive((active + 1) % matches.length); return; }
              if (e.key === "ArrowUp") { e.preventDefault(); setActive((active - 1 + matches.length) % matches.length); return; }
              if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pick(matches[active]); return; }
              if (e.key === "Escape") { e.preventDefault(); setQuery(null); return; }
            }
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); if (value.trim()) onSubmit(); }
            if (e.key === "Escape" && onCancel) onCancel();
          }}
          onBlur={() => window.setTimeout(() => setQuery(null), 150)} />
        {matches.length > 0 && (
          <ul className="mention-menu" role="listbox" aria-label="People to mention">
            {matches.map((m, i) => (
              <li key={m.user_id} role="option" aria-selected={i === active} className={i === active ? "on" : ""}
                onMouseDown={(e) => { e.preventDefault(); pick(m); }}>
                <b>{m.name}</b> <span className="muted small">{m.email}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="row gap end">
        {onCancel && <button type="button" className="small" onClick={onCancel}>Cancel</button>}
        <button className="primary small" disabled={!value.trim()}>{submitLabel}</button>
      </div>
    </form>
  );
}

/** Threaded comments on components, decisions and the project, with @mentions, replies and resolving. */
export function CommentsPanel({ pid, branch, comments, members, meId, state, selectedNode, selectedEdge = null, focusRequirement = null,
  onClearRequirement, onShowRequirement, focusThread, canComment, canResolveAny, isAdmin, onChanged, onShowNode, toast }: Props) {
  const [show, setShow] = useState<"open" | "resolved">("open");
  const [onlySelected, setOnlySelected] = useState(false);
  const [draft, setDraft] = useState({ text: "", mentions: [] as string[] });
  const [replyTo, setReplyTo] = useState<string | null>(null);
  const [reply, setReply] = useState({ text: "", mentions: [] as string[] });
  const [editing, setEditing] = useState<{ id: string; text: string; mentions: string[] } | null>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const names = useMemo(() => members.map((m) => m.name), [members]);
  const threads = useMemo(() => {
    const roots = comments.filter((c) => !c.parent_id);
    const replies = new Map<string, Comment[]>();
    for (const c of comments) if (c.parent_id) (replies.get(c.parent_id) ?? replies.set(c.parent_id, []).get(c.parent_id)!).push(c);
    return roots.map((root) => ({ root, replies: replies.get(root.id) ?? [] })).reverse(); // newest thread first
  }, [comments]);

  const focused = focusThread ? threads.find((t) => t.root.id === focusThread) : undefined;
  useEffect(() => {
    if (!focused) return;
    if (focused.root.resolved !== (show === "resolved")) setShow(focused.root.resolved ? "resolved" : "open");
    setOnlySelected(false);
    window.setTimeout(() => listRef.current?.querySelector(`[data-thread="${focused.root.id}"]`)?.scrollIntoView({ block: "center", behavior: "smooth" }), 60);
  }, [focusThread, focused?.root.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const edgeLabel = (id: string) => {
    const e = state?.edges[id];
    return e ? `${state?.nodes[e.source]?.name ?? e.source} → ${state?.nodes[e.target]?.name ?? e.target}` : undefined;
  };
  const req = focusRequirement ? state?.requirements[focusRequirement] : undefined;
  // What a new comment is about: a requirement opened for discussion, else the selected component or connection.
  const target: { target_type: CommentTarget; target_id: string; label?: string } = req
    ? { target_type: "requirement", target_id: req.id, label: `${req.id} ${req.title}` }
    : selectedNode && state?.nodes[selectedNode] ? { target_type: "node", target_id: selectedNode, label: state.nodes[selectedNode].name }
    : selectedEdge && edgeLabel(selectedEdge) ? { target_type: "edge", target_id: selectedEdge, label: edgeLabel(selectedEdge) }
    : { target_type: "project", target_id: "" };
  const selectedName = target.label;
  const visible = threads.filter((t) => (show === "resolved") === t.root.resolved
    && (!onlySelected || target.target_type === "project"
      || (t.root.target_type === target.target_type && t.root.target_id === target.target_id)));
  const openCount = threads.filter((t) => !t.root.resolved).length;
  const resolvedCount = threads.length - openCount;

  const run = async (p: Promise<unknown>, ok?: () => void) => {
    try { await p; ok?.(); onChanged(); } catch (e) { toast((e as Error).message); }
  };
  const targetLabel = (c: Comment) => c.target_type === "node" ? (state?.nodes[c.target_id]?.name ?? "a deleted component")
    : c.target_type === "edge" ? (edgeLabel(c.target_id) ?? "a deleted connection")
    : c.target_type === "requirement" ? (state?.requirements[c.target_id] ? `${c.target_id} ${state.requirements[c.target_id].title}` : `${c.target_id} (deleted)`)
    : c.target_type === "decision" ? c.target_id : "Whole project";
  const showTarget = (c: Comment) => {
    if (c.target_type === "node") onShowNode(c.target_id);
    else if (c.target_type === "edge") { const e = state?.edges[c.target_id]; if (e) onShowNode(e.source); }
    else if (c.target_type === "requirement") onShowRequirement?.(c.target_id);
  };
  const canShow = (c: Comment) => (c.target_type === "node" && !!state?.nodes[c.target_id])
    || (c.target_type === "edge" && !!state?.edges[c.target_id])
    || (c.target_type === "requirement" && !!state?.requirements[c.target_id] && !!onShowRequirement);
  const TargetIcon = (c: Comment) => c.target_type === "node" ? <Icon as={Box} size={12} />
    : c.target_type === "edge" ? <Icon as={ArrowLeftRight} size={12} />
    : c.target_type === "requirement" ? <Icon as={ListChecks} size={12} />
    : c.target_type === "decision" ? <Icon as={Scale} size={12} /> : <Icon as={Target} size={12} />;

  const commentView = (c: Comment, isRoot: boolean) => {
    const mine = c.author_id === meId;
    const member = members.find((m) => m.user_id === c.author_id);
    if (editing?.id === c.id) {
      return (
        <MentionInput members={members} meId={meId} value={editing.text} label="Edit comment" placeholder="Edit your comment" autoFocus
          submitLabel="Save" onChange={(text, mentions) => setEditing({ id: c.id, text, mentions: [...new Set([...c.mentions.filter((m) => text.includes(`@${members.find((x) => x.user_id === m)?.name}`)), ...mentions])] })}
          onCancel={() => setEditing(null)}
          onSubmit={() => run(api.updateComment(pid, c.id, { body: editing.text, mentions: editing.mentions }), () => setEditing(null))} />
      );
    }
    return (
      <div className="comment">
        <div className="comment-head">
          <span className="avatar sm" aria-hidden>{c.author_name[0]?.toUpperCase()}</span>
          <b>{c.author_name}</b>
          <span className="muted small" title={new Date(c.created_at).toLocaleString()}>{ago(c.created_at)}{c.edited_at ? " · edited" : ""}</span>
          {!member && <span className="muted small">(left the team)</span>}
          <span className="grow" />
          {isRoot && canComment && (mine || canResolveAny) && (
            <button className="small ghost" onClick={() => run(api.updateComment(pid, c.id, { resolved: !c.resolved }))}
              title={c.resolved ? "Reopen this thread" : "Mark as resolved and hide it"}>{c.resolved ? "Reopen" : <><Icon as={Check} size={12} /> Resolve</>}</button>
          )}
          {mine && canComment && <button className="small ghost" onClick={() => setEditing({ id: c.id, text: c.body, mentions: c.mentions })}>Edit</button>}
          {(mine || isAdmin) && canComment && (
            <button className="small ghost danger" onClick={() => run(api.deleteComment(pid, c.id))}
              title={isRoot ? "Delete this thread and its replies" : "Delete this reply"}>Delete</button>
          )}
        </div>
        <div className="comment-body"><Body text={c.body} names={names} /></div>
      </div>
    );
  };

  return (
    <div className="panel comments-panel">
      {canComment ? (
        <div className="panel-head">
          <div className="panel-summary small">
            Comment on {selectedName ? <b>{selectedName}</b> : <b>the whole project</b>}
            {req && onClearRequirement && <> <button className="link small" onClick={onClearRequirement}>(comment on the project instead)</button></>}
            {!selectedName && <span className="muted"> · select a component or connection, or use Discuss on a requirement</span>}. Type @ to mention someone.
          </div>
          <MentionInput members={members} meId={meId} value={draft.text} label="New comment" submitLabel="Comment"
            placeholder={selectedName ? `Comment on ${selectedName}…` : "Ask a question or leave feedback…"}
            onChange={(text, mentions) => setDraft({ text, mentions })}
            onSubmit={() => run(api.addComment(pid, branch, { body: draft.text, mentions: draft.mentions, ...target }), () => setDraft({ text: "", mentions: [] }))} />
        </div>
      ) : (
        <div className="panel-head"><div className="panel-summary small">Your role can read comments. Ask an admin for the reviewer role to comment.</div></div>
      )}

      <div className="panel-tools">
        <div className="seg" role="tablist" aria-label="Show comments">
          <button role="tab" aria-selected={show === "open"} className={show === "open" ? "on" : ""} onClick={() => setShow("open")}>Open · {openCount}</button>
          <button role="tab" aria-selected={show === "resolved"} className={show === "resolved" ? "on" : ""} onClick={() => setShow("resolved")}>Resolved · {resolvedCount}</button>
        </div>
        {selectedName && (
          <label className="small row gap"><input type="checkbox" checked={onlySelected} onChange={(e) => setOnlySelected(e.target.checked)} /> Only {selectedName}</label>
        )}
      </div>

      {visible.length === 0 && (
        <div className="empty-state"><p className="small">{show === "open" ? "No open comments." : "No resolved comments."}</p></div>
      )}
      <ul className="thread-list" ref={listRef}>
        {visible.map(({ root, replies }) => (
          <li key={root.id} data-thread={root.id} className={`thread ${focusThread === root.id ? "focused" : ""} ${root.resolved ? "resolved" : ""}`}>
            <button className="thread-target small" disabled={!canShow(root)} onClick={() => showTarget(root)}
              title={canShow(root) ? (root.target_type === "requirement" ? "Open the requirement" : "Show on the canvas") : undefined}>
              {TargetIcon(root)} {targetLabel(root)}
            </button>
            {commentView(root, true)}
            {replies.map((r) => <div key={r.id} className="reply">{commentView(r, false)}</div>)}
            {root.resolved && <div className="muted small">Resolved by {root.resolved_by}</div>}
            {canComment && !root.resolved && (replyTo === root.id ? (
              <div className="reply">
                <MentionInput members={members} meId={meId} value={reply.text} label="Reply" placeholder="Reply…" autoFocus submitLabel="Reply"
                  onChange={(text, mentions) => setReply({ text, mentions })}
                  onCancel={() => { setReplyTo(null); setReply({ text: "", mentions: [] }); }}
                  onSubmit={() => run(api.addComment(pid, branch, { body: reply.text, mentions: reply.mentions, parent_id: root.id }),
                    () => { setReplyTo(null); setReply({ text: "", mentions: [] }); })} />
              </div>
            ) : (
              <button className="small ghost reply-btn" onClick={() => { setReplyTo(root.id); setReply({ text: "", mentions: [] }); }}>Reply</button>
            ))}
          </li>
        ))}
      </ul>
    </div>
  );
}
