import { useEffect, useState } from "react";
import { Icon, RefreshCw } from "../icons";
import { api } from "../api";
import type { JiraIssuePreview, JiraPlan, JiraPlanItem, JiraPushResult, JiraSettings, JiraWebhookSetup } from "../types";
import { Modal } from "./OrgDialogs";

interface Props {
  pid: string;
  branch: string;
  defaultKey: string;
  aiAvailable: boolean;
  /** The project's organization; admins of it can set up live updates (the Jira webhook). */
  orgId?: string;
  isAdmin?: boolean;
  onClose: () => void;
  /** After a push, a sync or an import: reload the links shown next to requirements and components. */
  onChanged: () => void;
  /** Short confirmation messages (e.g. after an import). */
  toast?: (text: string) => void;
}

type Tab = "push" | "pull" | "live";
const TYPE_LABEL: Record<JiraPlanItem["type"], string> = { epic: "Epic", story: "Story", subtask: "Sub-task", link: "Link" };
const CATEGORY_LABEL: Record<string, string> = { new: "To Do", indeterminate: "In Progress", done: "Done" };

/**
 * Jira, both ways. Push: always a dry-run preview first, then an explicit push (re-pushing updates the same issues).
 * Pull: pick issues from the Jira project that are not linked yet and import them as requirements.
 * Live updates: the webhook that keeps issue status current on the canvas, with "Sync now" as the fallback.
 */
export function JiraDialog({ pid, branch, defaultKey, aiAvailable, orgId, isAdmin = false, onClose, onChanged, toast }: Props) {
  const [tab, setTab] = useState<Tab>("push");
  const [error, setError] = useState("");
  const [syncing, setSyncing] = useState(false);
  const sync = async () => {
    setSyncing(true); setError("");
    try {
      const r = await api.jiraRefresh(pid, branch); onChanged();
      toast?.(`Synced the status of ${r.updated} issue${r.updated === 1 ? "" : "s"} from Jira`);
    } catch (e) { setError((e as Error).message); }
    setSyncing(false);
  };

  return (
    <Modal title="Jira" onClose={onClose}>
      <div className="row between gap jira-tabs-row">
        <div className="seg-chips" role="tablist" aria-label="Jira">
          <button role="tab" aria-selected={tab === "push"} className={tab === "push" ? "on" : ""} onClick={() => setTab("push")}>Push to Jira</button>
          <button role="tab" aria-selected={tab === "pull"} className={tab === "pull" ? "on" : ""} onClick={() => setTab("pull")}>Pull from Jira</button>
          <button role="tab" aria-selected={tab === "live"} className={tab === "live" ? "on" : ""} onClick={() => setTab("live")}>Live updates</button>
        </div>
        <button className="small" onClick={sync} disabled={syncing} title="Read the current status of every linked issue from Jira">
          {syncing ? "Syncing…" : <><Icon as={RefreshCw} size={14} /> Sync now</>}</button>
      </div>
      {error && <p className="error small">{error}</p>}
      {tab === "push" && <PushTab pid={pid} branch={branch} defaultKey={defaultKey} aiAvailable={aiAvailable} onClose={onClose} onChanged={onChanged} />}
      {tab === "pull" && <PullTab pid={pid} branch={branch} aiAvailable={aiAvailable} onChanged={onChanged} toast={toast} />}
      {tab === "live" && <LiveTab orgId={orgId} isAdmin={isAdmin} />}
    </Modal>
  );
}

// ---------------------------------------------------------------- push
function PushTab({ pid, branch, defaultKey, aiAvailable, onClose, onChanged }:
  { pid: string; branch: string; defaultKey: string; aiAvailable: boolean; onClose: () => void; onChanged: () => void }) {
  const [key, setKey] = useState(defaultKey);
  const [useAi, setUseAi] = useState(aiAvailable);
  const [plan, setPlan] = useState<JiraPlan | null>(null);
  const [result, setResult] = useState<JiraPushResult | null>(null);
  const [busy, setBusy] = useState<"" | "plan" | "push">("");
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState("");

  const preview = async () => {
    setBusy("plan"); setError(""); setResult(null);
    try { setPlan(await api.jiraPlan(pid, branch, key, useAi)); } catch (e) { setError((e as Error).message); }
    setBusy("");
  };
  useEffect(() => { if (key) preview(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const push = async () => {
    if (!plan) return;
    const n = plan.counts.create + plan.counts.update;
    if (!n || !window.confirm(`Push to Jira project ${plan.project_key}? ${plan.counts.create} to create, ${plan.counts.update} to update.`)) return;
    setBusy("push"); setError("");
    try { setResult(await api.jiraPush(pid, branch, key, useAi)); onChanged(); setPlan(await api.jiraPlan(pid, branch, key, useAi)); }
    catch (e) { setError((e as Error).message); }
    setBusy("");
  };

  const failed = result?.results.filter((r) => r.status === "error") ?? [];
  return (
    <>
      <div className="row gap wrap">
        <label className="field"><span>Jira project</span>
          <input className="mono" value={key} maxLength={10} onChange={(e) => setKey(e.target.value.toUpperCase())} placeholder="SHOP" />
        </label>
        <label className="row gap small" title={aiAvailable ? "Let the AI write the stories, criteria and estimates" : "AI is off: rules derive the stories"}>
          <input type="checkbox" checked={useAi} disabled={!aiAvailable} onChange={(e) => setUseAi(e.target.checked)} /> AI-written stories
        </label>
        <button className="small" onClick={preview} disabled={!key || busy !== ""}>{busy === "plan" ? "Preparing…" : "Preview"}</button>
      </div>
      {plan && (
        <>
          <p className="small">
            <b>Preview (nothing sent yet):</b> {plan.by_type.epic} epics, {plan.by_type.story} stories, {plan.by_type.subtask} sub-tasks,
            {" "}{plan.by_type.link} links · <b>{plan.counts.create}</b> to create, <b>{plan.counts.update}</b> to update,
            {" "}{plan.counts.skip} unchanged · {plan.engine === "llm" ? "written by AI" : "written by rules"}
          </p>
          <p className="muted tiny">{plan.mapping}</p>
          <ul className="jira-plan">
            {plan.items.map((i) => (
              <li key={i.ref} className={`jira-item ${i.type} ${i.action}`}>
                <button className="ghost jira-row" onClick={() => setOpen(open === i.ref ? null : i.ref)} aria-expanded={open === i.ref}>
                  <span className={`jira-action ${i.action}`}>{i.action === "skip" ? "unchanged" : i.action}</span>
                  <span className="muted small">{TYPE_LABEL[i.type]}</span>
                  <span className="grow">{i.summary}</span>
                  {i.story_points ? <span className="chip small" title={i.points_basis}>{i.story_points} pts</span> : null}
                  {i.external_key && <a className="mono small" href={i.external_url || undefined} target="_blank" rel="noreferrer"
                    onClick={(e) => e.stopPropagation()}>{i.external_key}</a>}
                </button>
                {open === i.ref && i.type !== "link" && (
                  <div className="jira-detail small">
                    {i.acceptance_criteria && i.acceptance_criteria.length > 0 && (
                      <ul>{i.acceptance_criteria.map((c, k) => <li key={k}><b>Given</b> {c.given}, <b>when</b> {c.when}, <b>then</b> {c.then}</li>)}</ul>
                    )}
                    {i.points_basis && <p className="muted">Estimate: {i.points_basis}</p>}
                    {(i.labels?.length ?? 0) > 0 && <p className="muted">Labels: {i.labels!.join(", ")}</p>}
                    {(i.components?.length ?? 0) > 0 && <p className="muted">Components: {i.components!.join(", ")}</p>}
                    <pre className="jira-desc">{i.description}</pre>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
      {result && (
        <p className={`small ${result.counts.failed ? "warn" : "ok"}`}>
          Pushed to {result.project_key}: {result.counts.created} created, {result.counts.updated} updated, {result.counts.unchanged} unchanged
          {result.counts.failed > 0 && <>, {result.counts.failed} failed. Push again to retry only those.</>}
        </p>
      )}
      {failed.length > 0 && <ul className="small error">{failed.map((f) => <li key={f.ref}>{f.summary}: {f.error}</li>)}</ul>}
      {error && <p className="error small">{error}</p>}
      <div className="row end gap">
        <button onClick={onClose}>Close</button>
        <button className="primary" onClick={push} disabled={!plan || busy !== "" || plan.counts.create + plan.counts.update === 0}>
          {busy === "push" ? "Pushing…" : plan ? `Push ${plan.counts.create + plan.counts.update} change${plan.counts.create + plan.counts.update === 1 ? "" : "s"}` : "Push"}
        </button>
      </div>
    </>
  );
}

// ---------------------------------------------------------------- pull
function PullTab({ pid, branch, aiAvailable, onChanged, toast }:
  { pid: string; branch: string; aiAvailable: boolean; onChanged: () => void; toast?: (text: string) => void }) {
  const [issues, setIssues] = useState<JiraIssuePreview[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [hidden, setHidden] = useState(0);
  const [projectKey, setProjectKey] = useState("");
  const [q, setQ] = useState("");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [open, setOpen] = useState<string | null>(null);
  const [useAi, setUseAi] = useState(aiAvailable);
  const [busy, setBusy] = useState<"" | "load" | "more" | "import">("");
  const [error, setError] = useState("");
  const [done, setDone] = useState("");

  const load = async (more = false) => {
    setBusy(more ? "more" : "load"); setError("");
    try {
      const page = await api.jiraIssues(pid, { q, page_token: more ? next : null });
      setIssues((cur) => (more ? [...cur, ...page.issues.filter((i) => !cur.some((c) => c.key === i.key))] : page.issues));
      setNext(page.next_page_token); setProjectKey(page.project_key);
      setHidden((h) => (more ? h + page.hidden_linked : page.hidden_linked));
      if (!more) setPicked(new Set());
    } catch (e) { setError((e as Error).message); }
    setBusy("");
  };
  useEffect(() => { load(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (key: string) => setPicked((s) => { const n = new Set(s); if (n.has(key)) n.delete(key); else n.add(key); return n; });
  const allPicked = issues.length > 0 && issues.every((i) => picked.has(i.key));
  const doImport = async () => {
    if (!picked.size) return;
    setBusy("import"); setError(""); setDone("");
    try {
      const r = await api.jiraImport(pid, branch, [...picked], useAi);
      const got = new Set(r.imported.map((i) => i.key));
      setIssues((cur) => cur.filter((i) => !got.has(i.key)));
      setPicked(new Set());
      const msg = `Imported ${r.imported.length} issue${r.imported.length === 1 ? "" : "s"} as requirements`
        + (r.skipped.length ? ` · skipped ${r.skipped.map((s) => `${s.key} (${s.reason})`).join(", ")}` : "");
      setDone(msg); toast?.(msg); onChanged();
    } catch (e) { setError((e as Error).message); }
    setBusy("");
  };

  return (
    <>
      <p className="small muted">Issues in {projectKey ? <b className="mono">{projectKey}</b> : "the Jira project"} that are not linked to this
        project yet. Pick the ones to bring in as requirements on <b>{branch}</b>.</p>
      <form className="row gap wrap" onSubmit={(e) => { e.preventDefault(); load(); }}>
        <input type="search" className="grow" placeholder="Search Jira issues…" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search Jira issues" />
        <button className="small" type="submit" disabled={busy !== ""}>{busy === "load" ? "Loading…" : "Search"}</button>
      </form>
      {issues.length > 0 && (
        <label className="row gap small jira-pick-all">
          <input type="checkbox" checked={allPicked} onChange={() => setPicked(allPicked ? new Set() : new Set(issues.map((i) => i.key)))} />
          Select all shown ({issues.length})
        </label>
      )}
      <ul className="jira-plan jira-pull">
        {issues.map((i) => {
          const r = i.requirement;
          return (
            <li key={i.key} className="jira-item">
              <div className="jira-row">
                <input type="checkbox" checked={picked.has(i.key)} onChange={() => toggle(i.key)} aria-label={`Import ${i.key}`} />
                <a className="mono small" href={i.url} target="_blank" rel="noreferrer">{i.key}</a>
                <button className="ghost grow jira-pull-title" onClick={() => setOpen(open === i.key ? null : i.key)} aria-expanded={open === i.key}>
                  {i.summary || "(no summary)"}</button>
                <span className={`jira-chip ${i.status_category || "unknown"}`} title={i.status}>{CATEGORY_LABEL[i.status_category] ?? (i.status || "?")}</span>
              </div>
              <div className="jira-pull-meta tiny muted">
                {i.issue_type || "Issue"} · becomes {r.kind === "non_functional" ? `NFR${r.category ? ` (${r.category})` : ""}` : "functional"} · {r.priority}
                {r.acceptance_criteria.length > 0 && <> · {r.acceptance_criteria.length} criteria</>}
              </div>
              {open === i.key && (
                <div className="jira-detail small">
                  {r.description && <pre className="jira-desc">{r.description}</pre>}
                  {r.acceptance_criteria.length > 0 && <><b>Acceptance criteria</b><ul>{r.acceptance_criteria.map((c, k) => <li key={k}>{c}</li>)}</ul></>}
                  {i.labels.length > 0 && <p className="muted">Labels: {i.labels.join(", ")}</p>}
                </div>
              )}
            </li>
          );
        })}
      </ul>
      {busy !== "load" && issues.length === 0 && !error && <p className="small muted">Nothing to import: every issue found is already linked.</p>}
      <div className="row between gap wrap">
        <span className="tiny muted">{hidden > 0 && `${hidden} already-linked issue${hidden === 1 ? "" : "s"} hidden`}</span>
        {next && <button className="small" onClick={() => load(true)} disabled={busy !== ""}>{busy === "more" ? "Loading…" : "Load more"}</button>}
      </div>
      {done && <p className="small ok">{done}</p>}
      {error && <p className="error small">{error}</p>}
      <div className="row end gap wrap">
        <label className="row gap small" title={aiAvailable ? "Let the AI map issues to requirements" : "AI is off: rules map the issues"}>
          <input type="checkbox" checked={useAi} disabled={!aiAvailable} onChange={(e) => setUseAi(e.target.checked)} /> AI mapping
        </label>
        <button className="primary" onClick={doImport} disabled={!picked.size || busy !== ""}>
          {busy === "import" ? "Importing…" : `Import ${picked.size || ""} as requirement${picked.size === 1 ? "" : "s"}`.replace("  ", " ")}
        </button>
      </div>
    </>
  );
}

// ---------------------------------------------------------------- live updates (webhook)
function LiveTab({ orgId, isAdmin }: { orgId?: string; isAdmin: boolean }) {
  const [settings, setSettings] = useState<JiraSettings | null>(null);
  const [setup, setSetup] = useState<JiraWebhookSetup | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    if (isAdmin && orgId) api.jiraSettings(orgId).then(setSettings).catch((e) => setError((e as Error).message));
  }, [isAdmin, orgId]);

  if (!isAdmin || !orgId) {
    return <p className="small">Jira can tell ArchFlow when an issue changes, so status chips on the canvas stay current.
      An organization admin sets this up here. Until then, use <b>Sync now</b>.</p>;
  }
  const create = async () => {
    const replacing = settings?.webhook?.configured;
    if (replacing && !window.confirm("Create a new secret? The current Jira webhook stops working until you paste the new URL into Jira.")) return;
    setBusy(true); setError(""); setCopied(false);
    try { const s = await api.createJiraWebhook(orgId); setSetup(s); setSettings(s.settings); } catch (e) { setError((e as Error).message); }
    setBusy(false);
  };
  const remove = async () => {
    if (!window.confirm("Turn off live updates? Jira's calls will be refused until you set it up again.")) return;
    try { await api.deleteJiraWebhook(orgId); setSetup(null); setSettings(await api.jiraSettings(orgId)); } catch (e) { setError((e as Error).message); }
  };
  const copy = async () => {
    if (!setup) return;
    try { await navigator.clipboard.writeText(setup.url); setCopied(true); } catch { setCopied(false); }
  };
  const on = settings?.webhook?.configured;
  return (
    <div className="jira-live">
      <p className="small">Jira calls ArchFlow when an issue is created, changed or deleted. Status chips on the canvas and
        in requirements then update for everyone, live.</p>
      <p className="small">Status: {on
        ? <b className="ok">On{settings?.webhook?.created_at ? ` (secret created ${new Date(settings.webhook.created_at).toLocaleDateString()})` : ""}</b>
        : <b className="muted">Not set up</b>}</p>
      {setup && (
        <div className="jira-secret">
          <p className="small warn"><b>Copy this URL now.</b> It holds the secret, and ArchFlow will not show it again.</p>
          <div className="row gap">
            <input className="mono grow" readOnly value={setup.url} onFocus={(e) => e.target.select()} aria-label="Webhook URL" />
            <button className="small" onClick={copy}>{copied ? "Copied" : "Copy"}</button>
          </div>
          <ol className="small">{setup.steps.map((s, k) => <li key={k}>{s}</li>)}</ol>
          <p className="tiny muted">JQL filter: <code>{setup.jql}</code> · Secret (for Jira's optional Secret field): <code className="jira-secret-value">{setup.secret}</code></p>
        </div>
      )}
      {error && <p className="error small">{error}</p>}
      <div className="row end gap wrap">
        {on && <button className="danger small" onClick={remove}>Turn off</button>}
        <button className="primary" onClick={create} disabled={busy}>{busy ? "Creating…" : on ? "Create a new secret" : "Set up live updates"}</button>
      </div>
    </div>
  );
}
