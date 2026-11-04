import { useEffect, useState } from "react";
import { api } from "../api";
import type { ConfluencePublish, GitHubPlan, GitHubPushResult, IntegrationKind, IntegrationSettings, IntegrationState } from "../types";
import { Modal } from "./OrgDialogs";

const KINDS: { kind: IntegrationKind; label: string; help: string }[] = [
  { kind: "github", label: "GitHub", help: "Push requirements as issues (labels, acceptance criteria as task lists); re-pushes update them." },
  { kind: "confluence", label: "Confluence", help: "Publish each branch's architecture specification as a page; re-publishing updates it." },
  { kind: "slack", label: "Slack", help: "Post to a channel when versions, reviews, imports or pushes happen." },
  { kind: "teams", label: "Teams", help: "Post to a Microsoft Teams channel (Workflows webhook) on the same events." },
];

/** Org settings for GitHub, Confluence, Slack and Teams (admins). Secrets are write-only and encrypted. */
export function IntegrationsDialog({ orgId, orgName, onClose }: { orgId: string; orgName: string; onClose: () => void }) {
  const [s, setS] = useState<IntegrationSettings | null>(null);
  const [tab, setTab] = useState<IntegrationKind>("github");
  const [error, setError] = useState("");
  useEffect(() => { api.integrations(orgId).then(setS).catch((e) => setError(e.message)); }, [orgId]);
  return (
    <Modal title={`Integrations · ${orgName}`} onClose={onClose}>
      <div className="seg" role="tablist">
        {KINDS.map((k) => (
          <button key={k.kind} role="tab" aria-selected={tab === k.kind} className={tab === k.kind ? "on" : ""} onClick={() => setTab(k.kind)}>
            {k.label}{s?.integrations[k.kind].configured ? " ✓" : ""}</button>
        ))}
      </div>
      {!s && !error && <p className="small muted">Loading…</p>}
      {s && !s.encryption && <p className="warn small">Secrets cannot be stored: set ARCHFLOW_SECRET_KEY in the API's environment and restart it.</p>}
      {s && <p className="small muted">{KINDS.find((k) => k.kind === tab)!.help}</p>}
      {s && <IntegrationForm key={tab} orgId={orgId} kind={tab} settings={s} onSaved={setS} />}
      {error && <p className="error small">{error}</p>}
    </Modal>
  );
}

function IntegrationForm({ orgId, kind, settings, onSaved }: {
  orgId: string; kind: IntegrationKind; settings: IntegrationSettings; onSaved: (s: IntegrationSettings) => void;
}) {
  const cur: IntegrationState = settings.integrations[kind];
  const [cfg, setCfg] = useState<Record<string, any>>(() => ({
    ...(kind === "github" ? { api_url: "https://api.github.com", include_nfr: true } : {}),
    ...(kind === "slack" || kind === "teams" ? { events: Object.keys(settings.events) } : {}),
    ...cur.config,
  }));
  const [secret, setSecret] = useState("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: string, v: unknown) => setCfg((c) => ({ ...c, [k]: v }));
  const text = (k: string, label: string, placeholder = "", extra: React.ReactNode = null) => (
    <label className="field"><span>{label} {extra}</span>
      <input value={cfg[k] ?? ""} placeholder={placeholder} onChange={(e) => set(k, e.target.value)} /></label>
  );
  const webhook = kind === "slack" || kind === "teams";
  const useJira = kind === "confluence" && !!cfg.use_jira_credentials;

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setMsg(null);
    const config = Object.fromEntries(Object.entries(cfg).filter(([k]) => !k.startsWith("last_")));
    try { onSaved(await api.saveIntegration(orgId, kind, config, secret.trim() || null)); setSecret(""); setMsg({ ok: true, text: "Saved" }); }
    catch (err) { setMsg({ ok: false, text: (err as Error).message }); }
    setBusy(false);
  };
  const test = async () => {
    setBusy(true); setMsg(null);
    try { const r = await api.testIntegration(orgId, kind); setMsg({ ok: r.ok, text: r.ok ? `✓ ${r.detail}` : `✗ ${r.error}` }); }
    catch (err) { setMsg({ ok: false, text: (err as Error).message }); }
    setBusy(false);
  };
  const disconnect = async () => {
    if (!window.confirm(`Disconnect ${cur.label}? Issues and pages already created stay where they are.`)) return;
    try { await api.deleteIntegration(orgId, kind); onSaved(await api.integrations(orgId)); } catch (err) { setMsg({ ok: false, text: (err as Error).message }); }
  };

  return (
    <form onSubmit={save}>
      {kind === "github" && <>
        {text("repo", "Repository", "owner/name, e.g. acme/shop")}
        {text("api_url", "API URL", "https://api.github.com", <span className="muted tiny">GitHub Enterprise: https://your-host/api/v3</span>)}
        <label className="row gap small"><input type="checkbox" checked={!!cfg.include_nfr} onChange={(e) => set("include_nfr", e.target.checked)} />
          Also push non-functional requirements</label>
      </>}
      {kind === "confluence" && <>
        {settings.jira.configured && (
          <label className="row gap small"><input type="checkbox" checked={useJira} onChange={(e) => set("use_jira_credentials", e.target.checked)} />
            Use the Jira connection ({settings.jira.site_url}, {settings.jira.email})</label>
        )}
        {!useJira && <>
          {text("site_url", "Site URL", "https://your-team.atlassian.net")}
          {text("email", "Account email", "confluence-bot@your-company.com")}
        </>}
        {text("space_key", "Space key", "ARCH")}
        {text("parent_id", "Parent page id (optional)", "the number in the parent page's URL")}
      </>}
      {webhook && <>
        {text("channel", "Channel (for your reference)", kind === "slack" ? "#architecture" : "Architecture / General")}
        <div className="small"><b>Notify when</b></div>
        {Object.entries(settings.events).map(([ev, label]) => (
          <label key={ev} className="row gap small">
            <input type="checkbox" checked={(cfg.events ?? []).includes(ev)} onChange={(e) =>
              set("events", e.target.checked ? [...(cfg.events ?? []), ev] : (cfg.events ?? []).filter((x: string) => x !== ev))} />
            {label}
          </label>
        ))}
        {cur.config.last_sent_at && <p className="tiny muted">Last message sent {new Date(cur.config.last_sent_at).toLocaleString()}</p>}
        {cur.config.last_error && <p className="tiny warn">Last failure: {cur.config.last_error}</p>}
      </>}
      {!useJira && (
        <label className="field"><span>{webhook ? "Webhook URL" : kind === "github" ? "Token" : "API token"} <span className="muted tiny">
          {cur.has_secret ? `Stored …${cur.last4}. Leave empty to keep it.` : kind === "github"
            ? "A fine-grained token with Issues: read and write on the repository"
            : kind === "confluence" ? "From id.atlassian.com → Security → API tokens"
            : kind === "slack" ? "Slack app → Incoming Webhooks" : "Teams channel → Workflows → Post to a channel when a webhook request is received"}</span></span>
          <input type="password" autoComplete="off" value={secret} onChange={(e) => setSecret(e.target.value)}
            placeholder={cur.has_secret ? "Enter a new one to replace it" : webhook ? "https://…" : "Paste the token"} disabled={!settings.encryption} />
        </label>
      )}
      <div className="row between">
        <span className="row gap">
          {cur.configured && <button type="button" className="small" onClick={test} disabled={busy}>{webhook ? "Send a test message" : "Test connection"}</button>}
          {msg && <span className={`small ${msg.ok ? "ok" : "error"}`}>{msg.text}</span>}
        </span>
        <span className="row gap">
          {cur.configured && <button type="button" className="small danger" onClick={disconnect}>Disconnect</button>}
          <button className="primary" disabled={busy || !settings.encryption}>Save</button>
        </span>
      </div>
    </form>
  );
}

/** Preview, then push this branch's requirements to GitHub Issues. */
export function GitHubDialog({ pid, branch, repo, onClose, onChanged }: {
  pid: string; branch: string; repo: string; onClose: () => void; onChanged: () => void;
}) {
  const [plan, setPlan] = useState<GitHubPlan | null>(null);
  const [result, setResult] = useState<GitHubPushResult | null>(null);
  const [nfr, setNfr] = useState<boolean | undefined>(undefined);
  const [busy, setBusy] = useState<"" | "plan" | "push" | "refresh">("");
  const [error, setError] = useState("");
  const [open, setOpen] = useState<string | null>(null);

  const preview = async (include = nfr) => {
    setBusy("plan"); setError("");
    try { const p = await api.githubPlan(pid, branch, include); setPlan(p); setNfr(p.include_nfr); } catch (e) { setError((e as Error).message); }
    setBusy("");
  };
  useEffect(() => { preview(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const changes = plan ? plan.counts.create + plan.counts.update + plan.counts.close : 0;
  const push = async () => {
    if (!plan || !changes || !window.confirm(`Push to ${plan.repo}? ${plan.counts.create} to open, ${plan.counts.update} to update, ${plan.counts.close} to close.`)) return;
    setBusy("push"); setError("");
    try { setResult(await api.githubPush(pid, branch, nfr)); onChanged(); setPlan(await api.githubPlan(pid, branch, nfr)); }
    catch (e) { setError((e as Error).message); }
    setBusy("");
  };
  const refresh = async () => {
    setBusy("refresh"); setError("");
    try { const r = await api.githubRefresh(pid, branch); onChanged(); setError(""); window.alert(`Updated the state of ${r.updated} issue(s).`); }
    catch (e) { setError((e as Error).message); }
    setBusy("");
  };
  const failed = result?.results.filter((r) => r.status === "error") ?? [];
  return (
    <Modal title={`Push to GitHub · ${repo}`} onClose={onClose}>
      <div className="row gap">
        <label className="row gap small"><input type="checkbox" checked={!!nfr} onChange={(e) => { setNfr(e.target.checked); preview(e.target.checked); }} />
          Include non-functional requirements</label>
        <button className="small" onClick={() => preview()} disabled={busy !== ""}>{busy === "plan" ? "Preparing…" : "Preview"}</button>
        <button className="small" onClick={refresh} disabled={busy !== ""} title="Read each issue's state from GitHub">{busy === "refresh" ? "Reading…" : "Refresh state"}</button>
      </div>
      {plan && (
        <>
          <p className="small"><b>Preview (nothing sent yet):</b> <b>{plan.counts.create}</b> to open, <b>{plan.counts.update}</b> to update,
            {" "}<b>{plan.counts.close}</b> to close (removed in ArchFlow), {plan.counts.skip} unchanged</p>
          <ul className="jira-plan">
            {plan.items.map((i) => (
              <li key={i.ref} className={`jira-item ${i.action}`}>
                <button className="ghost jira-row" onClick={() => setOpen(open === i.ref ? null : i.ref)} aria-expanded={open === i.ref}>
                  <span className={`jira-action ${i.action}`}>{i.action === "skip" ? "unchanged" : i.action}</span>
                  <span className="grow">{i.title}</span>
                  {i.labels.map((l) => <span key={l} className="chip small">{l}</span>)}
                  {i.external_key && <a className="mono small" href={i.external_url || undefined} target="_blank" rel="noreferrer"
                    onClick={(e) => e.stopPropagation()}>{i.external_key}{i.status === "closed" ? " (closed)" : ""}</a>}
                </button>
                {open === i.ref && i.body && <pre className="jira-desc small">{i.body}</pre>}
              </li>
            ))}
          </ul>
        </>
      )}
      {result && (
        <p className={`small ${result.counts.failed ? "warn" : "ok"}`}>Pushed to {result.repo}: {result.counts.created} opened, {result.counts.updated} updated,
          {" "}{result.counts.closed} closed, {result.counts.unchanged} unchanged{result.counts.failed > 0 && <>, {result.counts.failed} failed. Push again to retry them.</>}</p>
      )}
      {failed.length > 0 && <ul className="small error">{failed.map((f) => <li key={f.ref}>{f.title}: {f.error}</li>)}</ul>}
      {error && <p className="error small">{error}</p>}
      <div className="row end gap">
        <button onClick={onClose}>Close</button>
        <button className="primary" onClick={push} disabled={!plan || busy !== "" || changes === 0}>
          {busy === "push" ? "Pushing…" : `Push ${changes} change${changes === 1 ? "" : "s"}`}</button>
      </div>
    </Modal>
  );
}

/** Preview, then publish this branch's architecture specification to Confluence. */
export function ConfluenceDialog({ pid, branch, space, onClose }: { pid: string; branch: string; space: string; onClose: () => void }) {
  const [dry, setDry] = useState<ConfluencePublish | null>(null);
  const [done, setDone] = useState<ConfluencePublish | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { api.confluencePublish(pid, branch, true).then(setDry).catch((e) => setError(e.message)); }, [pid, branch]);
  const publish = async () => {
    setBusy(true); setError("");
    try { setDone(await api.confluencePublish(pid, branch, false)); setDry(await api.confluencePublish(pid, branch, true)); }
    catch (e) { setError((e as Error).message); }
    setBusy(false);
  };
  const VERB = { create: "Create", update: "Update", skip: "Up to date" } as const;
  return (
    <Modal title={`Publish to Confluence · ${space}`} onClose={onClose}>
      {!dry && !error && <p className="small muted">Preparing the page…</p>}
      {dry && (
        <>
          <p className="small"><b>{dry.title}</b> · {Math.round(dry.chars / 1000)}k characters ·{" "}
            {dry.action === "skip" ? "unchanged since the last publish" : dry.action === "update" ? "updates the existing page" : `a new page in ${dry.space_key}`}
            {dry.url && <> · <a href={dry.url} target="_blank" rel="noreferrer">open the page</a></>}</p>
          {/* A sandboxed frame: the preview is HTML built from project data, never run as part of the app. */}
          {dry.preview && <iframe className="confluence-preview" title="Page preview" sandbox="" srcDoc={dry.preview} />}
        </>
      )}
      {done && done.action !== "skip" && <p className="small ok">{done.action === "create" ? "Created" : "Updated"} the page
        {done.version ? ` (version ${done.version})` : ""}: <a href={done.url} target="_blank" rel="noreferrer">{done.title}</a></p>}
      {error && <p className="error small">{error}</p>}
      <div className="row end gap">
        <button onClick={onClose}>Close</button>
        <button className="primary" onClick={publish} disabled={!dry || busy || dry.action === "skip"}>
          {busy ? "Publishing…" : dry ? VERB[dry.action] : "Publish"}</button>
      </div>
    </Modal>
  );
}
