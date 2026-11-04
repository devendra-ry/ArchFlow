import { useEffect, useState } from "react";
import { api } from "../api";
import type { Issue, JiraLink, Op, ProjectState, Requirement, RequirementRevision } from "../types";
import { Field } from "./Field";
import { ImportRequirementsDialog, sourceLabel } from "./ImportRequirementsDialog";
import { MeetingImportDialog } from "./MeetingImportDialog";

interface Props {
  pid: string;
  branch: string;
  toast: (text: string) => void;
  onMemoryChanged: () => void;
  state: ProjectState;
  issues: Issue[];
  traced: string | null;
  onTrace: (id: string | null) => void;
  send: (ops: Op[]) => void;
  onPrompt: (reqId: string) => void;
  readOnly?: boolean;
  /** Admins may remove a deleted requirement permanently. */
  isAdmin?: boolean;
  /** Jira stories pushed for requirements, by requirement id. */
  jira?: Record<string, JiraLink>;
  /** Open the Jira push dialog (shown when the org has Jira connected and you may push). */
  onJira?: () => void;
  /** GitHub issues pushed for requirements, by requirement id. */
  github?: Record<string, JiraLink>;
  /** Open the GitHub push dialog (shown when the org has GitHub connected and you may push). */
  onGitHub?: () => void;
  /** After a meeting import: decisions were recorded, or the knowledge base changed. */
  onMeetingImported?: (changed: { decisions: boolean; knowledge: boolean }) => void;
}

/** A requirement's Jira issue key, linked, with the status last read from Jira. */
export function JiraBadge({ link }: { link: JiraLink }) {
  return (
    <a className={`jira-badge ${link.status_category || "unknown"}`} href={link.external_url || undefined} target="_blank" rel="noreferrer"
      title={link.status ? `Jira status: ${link.status}` : "Pushed to Jira (refresh status from the Jira dialog)"}
      onClick={(e) => e.stopPropagation()}>
      {link.external_key}{link.status && <span className="muted"> · {link.status}</span>}
    </a>
  );
}

const splitList = (s: string, sep: string | RegExp) => s.split(sep).map((x) => x.trim()).filter(Boolean);
const STATUS = ["draft", "review", "approved", "implemented"] as const;
const PRIORITY = ["low", "medium", "high", "critical"] as const;
type View = "all" | "untraced" | "issues" | "nfr" | "deleted";

const ACTION_LABEL: Record<RequirementRevision["action"], string> = {
  added: "Created", updated: "Changed", deleted: "Deleted", restored: "Restored", purged: "Removed permanently",
};
const KIND_LABEL: Record<RequirementRevision["actor_kind"], string> = { ai: "AI", agent: "Agent", human: "Person" };
const show = (v: unknown) => (v === null || v === undefined || v === "" ? "—" : Array.isArray(v) ? v.join("; ") || "—"
  : typeof v === "object" ? JSON.stringify(v) : String(v));

/** Version history of one requirement: who changed which field, when, and whether a person, the AI or an agent did. */
function History({ pid, branch, req }: { pid: string; branch: string; req: Requirement }) {
  const [rows, setRows] = useState<RequirementRevision[] | null>(null);
  const [error, setError] = useState("");
  const content = JSON.stringify({ ...req, rev: undefined, field_revs: undefined });
  useEffect(() => {
    // The API records a canvas edit when the live document is stored (a second or two later).
    const t = window.setTimeout(() => api.requirementHistory(pid, branch, req.id)
      .then((r) => { setRows(r); setError(""); }).catch((e) => setError((e as Error).message)), rows ? 2500 : 0);
    return () => window.clearTimeout(t);
  }, [pid, branch, req.id, content]); // eslint-disable-line react-hooks/exhaustive-deps
  if (error) return <p className="small warn">{error}</p>;
  if (!rows) return <p className="small muted">Loading history…</p>;
  if (!rows.length) return <p className="small muted">No recorded changes yet.</p>;
  return (
    <ol className="req-history">
      {rows.map((h) => (
        <li key={h.id}>
          <div className="small">
            <b>{ACTION_LABEL[h.action]}</b> by {h.actor} <span className={`who ${h.actor_kind}`}>{KIND_LABEL[h.actor_kind]}</span>
            <span className="muted tiny"> · {new Date(h.created_at).toLocaleString()} · rev {h.rev}</span>
          </div>
          {h.changes.length > 0 && (
            <ul className="tiny">
              {h.changes.map((c) => (
                <li key={c.field}><span className="mono">{c.field}</span>{" "}
                  {h.action !== "added" && <><del className="muted">{show(c.before)}</del> → </>}<span>{show(c.after)}</span></li>
              ))}
            </ul>
          )}
        </li>
      ))}
    </ol>
  );
}

export function RequirementsPanel({ pid, branch, toast, onMemoryChanged, state, issues, traced, onTrace, send, onPrompt,
  readOnly = false, isAdmin = false, jira = {}, onJira, github = {}, onGitHub, onMeetingImported }: Props) {
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<"edit" | "history">("edit");
  const [importing, setImporting] = useState(false);
  const [meeting, setMeeting] = useState(false);
  const [filter, setFilter] = useState("");
  const [view, setView] = useState<View>("all");
  const everything = Object.values(state.requirements);
  const all = everything.filter((r) => !r.deleted);
  const deleted = everything.filter((r) => r.deleted);
  const linked = (id: string) => Object.values(state.nodes).filter((n) => n.requirement_ids.includes(id));
  const issuesOf = (id: string) => issues.filter((i) => i.requirement_ids.includes(id));
  const untraced = all.filter((r) => linked(r.id).length === 0);
  const withIssues = all.filter((r) => issuesOf(r.id).length > 0);
  const views: { id: View; label: string; count: number }[] = [
    { id: "all", label: "All", count: all.length },
    { id: "untraced", label: "Not traced", count: untraced.length },
    { id: "issues", label: "Has issues", count: withIssues.length },
    { id: "nfr", label: "NFRs", count: all.filter((r) => r.kind === "non_functional").length },
    { id: "deleted", label: "Deleted", count: deleted.length },
  ];
  const reqs = (view === "deleted" ? deleted : all).filter((r) =>
    (view === "all" || view === "deleted" || (view === "untraced" && untraced.includes(r)) || (view === "issues" && withIssues.includes(r))
      || (view === "nfr" && r.kind === "non_functional"))
    && (!filter || `${r.id} ${r.title} ${r.description}`.toLowerCase().includes(filter.toLowerCase())));

  const purge = async (r: Requirement) => {
    if (!window.confirm(`Remove ${r.id} permanently? Its links go too, and this cannot be undone. Its history is kept.`)) return;
    try {
      await api.purgeRequirement(pid, branch, r.id);
      toast?.(`${r.id} removed permanently`);
    } catch (e) {
      toast?.(`⚠ ${(e as Error).message}`);
    }
  };
  const add = (kind: Requirement["kind"]) =>
    send([{ op: "add_requirement", requirement: { title: kind === "functional" ? "New requirement" : "New non-functional requirement", kind } }]);

  return (
    <div className="panel">
      <div className="panel-head">
        <div className="panel-summary small">
          {all.length === 0 ? "No requirements yet"
            : <><b>{all.length}</b> requirement{all.length === 1 ? "" : "s"}
              {untraced.length > 0 && <> · <span className="warn">{untraced.length} not traced</span></>}
              {withIssues.length > 0 && <> · <span className="warn">{withIssues.length} with issues</span></>}</>}
        </div>
        {!readOnly && (
          <div className="row gap">
            <button className="small primary" onClick={() => add("functional")} title="A feature the system must provide">+ Functional</button>
            <button className="small" onClick={() => add("non_functional")} title="A quality target: performance, security, cost…">+ Non-functional</button>
            <button className="small" onClick={() => setImporting(true)} title="Import requirements from a document (PDF, Word, Markdown, text)">⇪ Import</button>
            <button className="small" onClick={() => setMeeting(true)}
              title="Import requirements, decisions and action items from a meeting transcript or recording">🎙 Meeting</button>
            {onJira && all.length > 0 && <button className="small" onClick={onJira} title="Preview, then push requirements to Jira as epics and stories">Jira ↗</button>}
            {onGitHub && all.length > 0 && <button className="small" onClick={onGitHub} title="Preview, then push requirements to GitHub as issues">GitHub ↗</button>}
          </div>
        )}
      </div>
      {importing && (
        <ImportRequirementsDialog pid={pid} branch={branch} onClose={() => setImporting(false)}
          onDone={(message, savedMemory) => { setImporting(false); toast(message); if (savedMemory) onMemoryChanged(); }} />
      )}
      {meeting && (
        <MeetingImportDialog pid={pid} branch={branch} canAddKnowledge={!readOnly} onClose={() => setMeeting(false)}
          onDone={(message, changed) => {
            setMeeting(false); toast(message);
            if (changed.memory) onMemoryChanged();
            onMeetingImported?.(changed);
          }} />
      )}
      {everything.length > 0 && (
        <div className="panel-filters">
          <div className="seg-chips" role="tablist" aria-label="Show">
            {views.filter((v) => v.id !== "deleted" || v.count > 0 || view === "deleted").map((v) => (
              <button key={v.id} role="tab" aria-selected={view === v.id} className={view === v.id ? "on" : ""}
                onClick={() => setView(v.id)} disabled={v.count === 0 && v.id !== "all"}>{v.label} <span className="muted">{v.count}</span></button>
            ))}
          </div>
          {everything.length > 6 && <input type="search" placeholder="Search requirements…" value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Search requirements" />}
        </div>
      )}
      {all.length === 0 && view !== "deleted" && (
        <div className="empty-state">
          <p className="small">Requirements are what the system must do and how well. Add them here, or describe your idea in the
            assistant's <b>Design</b> mode and it will write them with you.</p>
        </div>
      )}
      {everything.length > 0 && reqs.length === 0 && (all.length > 0 || view === "deleted") && <p className="muted small pad">Nothing matches.</p>}
      <ul className="req-list">
        {reqs.map((r) => {
          const mine = r.deleted ? [] : issuesOf(r.id);
          const nodes = linked(r.id);
          const isOpen = open === r.id;
          const upd = (field: string, value: unknown, base_rev: number) =>
            send([{ op: "update_requirement", id: r.id, changes: { [field]: value }, base_rev }]);
          return (
            <li key={r.id} className={`req ${traced === r.id ? "traced" : ""} ${isOpen ? "open" : ""} ${r.deleted ? "deleted" : ""}`}>
              <button className="req-row" aria-expanded={isOpen}
                onClick={() => { setOpen(isOpen ? null : r.id); if (!r.deleted) onTrace(traced === r.id ? null : r.id); }}
                title={nodes.length ? `Implemented by ${nodes.map((n) => n.name).join(", ")}` : "No component implements this yet"}>
                <span className={`req-dot ${mine.length ? "bad" : nodes.length ? "ok" : "warn"}`} aria-hidden />
                <span className="req-title">{r.title}</span>
                <span className={`status s-${r.deleted ? "deleted" : r.status}`}>{r.deleted ? "deleted" : r.status}</span>
              </button>
              <div className="req-meta small muted">
                <span className="mono">{r.id}</span> · {r.kind === "non_functional" ? `NFR${r.category ? ` · ${r.category}` : ""}` : "Functional"} · {r.priority}
                {" · "}{nodes.length ? `${nodes.length} component${nodes.length > 1 ? "s" : ""}` : <span className="warn">not traced</span>}
                {mine.length > 0 && <span className="warn"> · {mine.length} issue{mine.length > 1 ? "s" : ""}</span>}
                {sourceLabel(r.source) && (
                  <span title={`Imported${r.source?.at ? ` on ${new Date(r.source.at).toLocaleString()}` : ""}`}> · from {sourceLabel(r.source)}</span>
                )}
                {jira[r.id] && <> · <JiraBadge link={jira[r.id]} /></>}
                {github[r.id] && <> · <JiraBadge link={github[r.id]} /></>}
              </div>
              {isOpen && (
                <div className="req-edit">
                  <div className="seg-chips" role="tablist" aria-label="Requirement detail">
                    <button role="tab" aria-selected={detail === "edit"} className={detail === "edit" ? "on" : ""} onClick={() => setDetail("edit")}>Details</button>
                    <button role="tab" aria-selected={detail === "history"} className={detail === "history" ? "on" : ""} onClick={() => setDetail("history")}>History</button>
                  </div>
                  {detail === "history" ? <History pid={pid} branch={branch} req={r} /> : <>
                    {mine.map((i, k) => (
                      <div key={k} className={`issue ${i.severity}`}><b>{i.message}</b> {i.suggestion}</div>
                    ))}
                    {r.source?.type && (
                      <p className="tiny muted">Source: {r.source.type}{r.source.ref ? ` · ${r.source.ref}` : ""}
                        {r.source.url && <> · <a href={r.source.url} target="_blank" rel="noreferrer">open</a></>}</p>
                    )}
                    <fieldset className="bare" disabled={readOnly || r.deleted}>
                      <Field label="Title" value={r.title} rev={state.rev} onCommit={(v, b) => upd("title", v, b)} />
                      <Field label="Description" value={r.description} rev={state.rev} multiline onCommit={(v, b) => upd("description", v, b)} />
                      <div className="grid2">
                        <Field label="Status" value={r.status} rev={state.rev} options={STATUS} onCommit={(v, b) => upd("status", v, b)} />
                        <Field label="Priority" value={r.priority} rev={state.rev} options={PRIORITY} onCommit={(v, b) => upd("priority", v, b)} />
                        <Field label="Kind" value={r.kind} rev={state.rev} options={["functional", "non_functional"]} onCommit={(v, b) => upd("kind", v, b)} />
                        <Field label="Category" value={r.category} rev={state.rev} placeholder="performance, security…" onCommit={(v, b) => upd("category", v, b)} />
                      </div>
                      <Field label="Actors (comma-separated)" value={r.actors.join(", ")} rev={state.rev}
                        onCommit={(v, b) => upd("actors", splitList(v, ","), b)} />
                      <Field label="Acceptance criteria (one per line, Ctrl+Enter to save)" value={r.acceptance_criteria.join("\n")} rev={state.rev} multiline
                        onCommit={(v, b) => upd("acceptance_criteria", splitList(v, /\n/), b)} />
                    </fieldset>
                  </>}
                  {r.deleted ? (
                    <div className="insp-actions">
                      {!readOnly && <button className="primary" onClick={() => send([{ op: "restore_requirement", id: r.id }])}>Restore</button>}
                      {isAdmin && <button className="danger" onClick={() => purge(r)} title="Admins only">Delete permanently</button>}
                    </div>
                  ) : (
                    <div className="insp-actions">
                      <button onClick={() => onPrompt(r.id)} title="Turn this requirement into a change spec for a coding agent">⚙ Vibe code this</button>
                      {!readOnly && <button className="danger" onClick={() => send([{ op: "remove_requirement", id: r.id }])}
                        title="Moves it to Deleted, where it can be restored">Delete</button>}
                    </div>
                  )}
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
