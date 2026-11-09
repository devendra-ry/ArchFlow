import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { ArrowUp, Icon, X } from "../icons";
import { ApiError, api } from "../api";
import { useEscape } from "../useEscape";
import { ROLES, ROLE_HELP, can, type AIConfig, type ApprovalPolicy, type AIGroup, type AISettings, type AITestResult, type AIUsageReport, type AIUsageRow, type Invite, type Member, type Role, type JiraSettings, type JiraTestResult } from "../types";
import { Copy } from "./ConnectDialog";
import { Select } from "./Select";

interface Base { orgId: string; orgName: string; role: Role; selfId: string; onClose: () => void; onChanged?: () => void }

export function Modal({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const escRef = useEscape(onClose);
  // Portalled to <body>: a dialog opened from a menu inside a bar with backdrop-filter (the home header) would otherwise
  // be positioned inside that bar, because backdrop-filter makes it the containing block for position: fixed.
  return createPortal(
    <div className="modal-backdrop" onClick={onClose} ref={escRef}>
      <div className="modal wide" role="dialog" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>{title}</h3>
          <button className="ghost" onClick={onClose} aria-label="Close"><Icon as={X} size={16} /></button>
        </div>
        {children}
      </div>
    </div>,
    document.body,
  );
}

/** Roles the current user may hand out: admins up to admin, owners anything. */
const grantable = (mine: Role) => ROLES.filter((r) => r !== "owner" || mine === "owner");

/** Members and invites. Admins and owners manage; everyone else sees the list read-only. */
export function MembersDialog({ orgId, orgName, role, selfId, onClose, onChanged }: Base) {
  const manage = can(role, "admin");
  const [members, setMembers] = useState<Member[]>([]);
  const [invites, setInvites] = useState<Invite[]>([]);
  const [inviteRole, setInviteRole] = useState<Role>("developer");
  const [inviteEmail, setInviteEmail] = useState("");
  const [link, setLink] = useState<string | null>(null);
  const [error, setError] = useState("");

  const load = () => {
    api.members(orgId).then(setMembers).catch((e) => setError(e.message));
    if (manage) api.invites(orgId).then(setInvites).catch(() => {});
  };
  useEffect(load, [orgId]); // eslint-disable-line react-hooks/exhaustive-deps

  const run = async (fn: () => Promise<unknown>) => {
    setError("");
    try { await fn(); load(); onChanged?.(); } catch (e) { setError((e as ApiError).message); }
  };
  const createInvite = (e: React.FormEvent) => {
    e.preventDefault();
    run(async () => {
      const inv = await api.createInvite(orgId, inviteRole, inviteEmail.trim() || undefined);
      setLink(inv.link);
      setInviteEmail("");
    });
  };

  return (
    <Modal title={`Members of ${orgName}`} onClose={onClose}>
      <table className="tokens members">
        <thead><tr><th>Name</th><th>Email</th><th>Role</th><th /></tr></thead>
        <tbody>
          {members.map((m) => {
            const editable = manage && (role === "owner" || m.role !== "owner");
            return (
              <tr key={m.user_id}>
                <td>{m.name}{m.user_id === selfId && <span className="muted small"> (you)</span>}</td>
                <td className="small">{m.email}</td>
                <td>
                  {editable ? (
                    <Select size="sm" value={m.role} aria-label={`Role of ${m.name}`} title={ROLE_HELP[m.role]}
                      onChange={(v) => run(() => api.updateMember(orgId, m.user_id, v))}
                      options={grantable(role).map((r) => ({ value: r, label: r, hint: ROLE_HELP[r] }))} menuWidth={280} />
                  ) : <span title={ROLE_HELP[m.role]}>{m.role}</span>}
                </td>
                <td>
                  {(editable || m.user_id === selfId) && (
                    <button className="small danger" onClick={() => {
                      const leaving = m.user_id === selfId;
                      if (window.confirm(leaving ? `Leave ${orgName}?` : `Remove ${m.name} from ${orgName}? Their API tokens for it stop working.`)) {
                        run(() => api.removeMember(orgId, m.user_id));
                      }
                    }}>{m.user_id === selfId ? "Leave" : "Remove"}</button>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {manage ? (
        <>
          <h4>Invite people</h4>
          <p className="muted small">Creates a single-use link valid for 7 days. Send it to the person; they sign up (or sign in) to join.</p>
          <form className="row gap" onSubmit={createInvite}>
            <input className="grow" type="email" placeholder="Email (optional: restricts who can use it)" value={inviteEmail}
              onChange={(e) => setInviteEmail(e.target.value)} aria-label="Invitee email" />
            <Select value={inviteRole} onChange={setInviteRole} aria-label="Role" title={ROLE_HELP[inviteRole]}
              options={grantable(role).map((r) => ({ value: r, label: r, hint: ROLE_HELP[r] }))} menuWidth={280} />
            <button className="primary">Create invite link</button>
          </form>
          {link && (
            <div className="token-created">
              <span className="small"><b>Copy this link now.</b> It won't be shown again.</span>
              <div className="snippet"><pre>{link}</pre><Copy text={link} /></div>
            </div>
          )}
          {invites.length > 0 && (
            <table className="tokens">
              <thead><tr><th>Pending invite</th><th>Role</th><th>Expires</th><th /></tr></thead>
              <tbody>
                {invites.map((i) => (
                  <tr key={i.id}>
                    <td className="small">{i.email ?? "anyone with the link"}</td>
                    <td>{i.role}</td>
                    <td className="small">{new Date(i.expires_at * 1000).toLocaleDateString()}</td>
                    <td><button className="small" onClick={() => run(() => api.revokeInvite(orgId, i.id))}>Revoke</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      ) : <p className="muted small">Only admins and owners can invite people or change roles.</p>}
      <ApprovalPolicySection orgId={orgId} manage={manage} />
      <details className="small muted roles-help">
        <summary>What each role can do</summary>
        <ul>{ROLES.map((r) => <li key={r}><b>{r}</b>: {ROLE_HELP[r]}</li>)}</ul>
      </details>
      {error && <p className="error small">{error}</p>}
    </Modal>
  );
}

/** Version approvals: how many approvals a version needs, and whether adopting a draft requires one. Admins edit. */
function ApprovalPolicySection({ orgId, manage }: { orgId: string; manage: boolean }) {
  const [policy, setPolicy] = useState<ApprovalPolicy | null>(null);
  const [required, setRequired] = useState(1);
  const [forAdopt, setForAdopt] = useState(false);
  const [protectMain, setProtectMain] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const show = (p: ApprovalPolicy) => {
    setPolicy(p); setRequired(p.required_approvals); setForAdopt(p.require_for_adopt); setProtectMain(p.protect_main);
  };
  useEffect(() => { api.approvalPolicy(orgId).then(show).catch((e) => setError(e.message)); }, [orgId]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(""); setSaved(false);
    try { show(await api.setApprovalPolicy(orgId, required, forAdopt, protectMain)); setSaved(true); }
    catch (err) { setError((err as Error).message); }
  };

  if (!policy) return error ? <p className="error small">{error}</p> : null;
  return (
    <form className="approval-policy" onSubmit={save}>
      <h4>Version approvals</h4>
      <p className="muted small">Reviewers, architects, admins and owners approve saved versions. Authors cannot approve
        their own version unless they are the only owner.</p>
      <label className="field"><span>Approvals required per version</span>
        <input type="number" min={1} max={10} value={required} disabled={!manage}
          onChange={(e) => setRequired(Math.max(1, Math.min(10, Number(e.target.value) || 1)))} />
      </label>
      <label className="small row gap">
        <input type="checkbox" checked={forAdopt} disabled={!manage} onChange={(e) => setForAdopt(e.target.checked)} />
        Require an approved version before a draft is adopted into main
      </label>
      <label className="small row gap">
        <input type="checkbox" checked={protectMain} disabled={!manage} onChange={(e) => setProtectMain(e.target.checked)} />
        Protect main: change it only by merging or adopting an approved branch (no direct edits, AI changes, imports or restores)
      </label>
      {manage && (
        <div className="row gap">
          <button className="primary small">Save approval settings</button>
          {saved && <span className="small ok">Saved</span>}
        </div>
      )}
      {error && <p className="error small">{error}</p>}
    </form>
  );
}

const GROUP_INFO: Record<AIGroup, { title: string; hint: string }> = {
  reasoning: { title: "Reasoning model", hint: "Design, edits, specs, reviews: use your strongest model" },
  fast: { title: "Fast model", hint: "Summaries, memory, naming: a cheaper, quicker model" },
};

/**
 * AI settings for an organization (admins): provider keys (write-only, encrypted), which models to use for each
 * group in fallback order (any LiteLLM provider), endpoints for Azure / self-hosted servers, and a live test.
 */
export function AISettingsDialog({ orgId, orgName, onClose }: Base) {
  const [s, setS] = useState<AISettings | null>(null);
  const [keyDraft, setKeyDraft] = useState<Record<string, string>>({});
  const [config, setConfig] = useState<AIConfig>({ reasoning: [], fast: [], params: {} });
  const [tab, setTab] = useState<"models" | "keys">("models");
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");
  const [tests, setTests] = useState<Partial<Record<AIGroup, AITestResult | "running">>>({});

  const load = (next: AISettings) => {
    setS(next);
    setConfig({ reasoning: next.config.reasoning ?? [], fast: next.config.fast ?? [], params: next.config.params ?? {} });
  };
  useEffect(() => { api.aiSettings(orgId).then(load).catch((e) => setError(e.message)); }, [orgId]);

  const catalog = s?.catalog ?? [];
  const info = (id: string) => catalog.find((p) => p.id === id);
  const flash = (msg: string) => { setSaved(msg); window.setTimeout(() => setSaved(""), 2500); };

  const saveKeys = async (changes: Record<string, string | null>) => {
    setError("");
    try { load(await api.saveAiSettings(orgId, changes)); setKeyDraft({}); flash("Keys saved"); setTests({}); }
    catch (e) { setError((e as Error).message); }
  };
  const saveConfig = async () => {
    setError("");
    try { load(await api.saveAiConfig(orgId, config)); flash("Models saved"); setTests({}); }
    catch (e) { setError((e as Error).message); }
  };
  const runTest = async (group: AIGroup) => {
    setTests((t) => ({ ...t, [group]: "running" }));
    try { const r = await api.testAi(orgId, group); setTests((t) => ({ ...t, [group]: r })); }
    catch (e) { setTests((t) => ({ ...t, [group]: { ok: false, group, error: (e as Error).message } })); }
  };

  const setChain = (group: AIGroup, chain: AIConfig[AIGroup]) => setConfig((c) => ({ ...c, [group]: chain }));
  const usedProviders = [...new Set([...config.reasoning, ...config.fast].map((e) => e.provider))];
  const pendingKeys = Object.fromEntries(Object.entries(keyDraft).filter(([, v]) => v.trim()));
  const keyless = usedProviders.filter((p) => info(p)?.key === "required" && !s?.providers[p]?.configured && !info(p)?.server_key);

  return (
    <Modal title={`AI settings · ${orgName}`} onClose={onClose}>
      {!s && !error && <p className="muted small">Loading…</p>}
      {s && (
        <>
          <div className={`ai-status ${s.available ? "on" : "off"}`}>
            <b>{s.available ? "AI is on" : "AI is off"}</b>
            <span className="small">
              {s.available
                ? <> Reasoning: <code>{s.effective.reasoning[0]}</code>{s.effective.fast[0] && <> · Fast: <code>{s.effective.fast[0]}</code></>}</>
                : " Add a provider key and choose models below; until then ArchFlow uses its built-in non-AI features."}
            </span>
          </div>
          <div className="seg ai-tabs" role="tablist">
            <button role="tab" aria-selected={tab === "models"} className={tab === "models" ? "on" : ""} onClick={() => setTab("models")}>Models</button>
            <button role="tab" aria-selected={tab === "keys"} className={tab === "keys" ? "on" : ""} onClick={() => setTab("keys")}>
              Provider keys {Object.values(s.providers).filter((p) => p.configured).length ? `(${Object.values(s.providers).filter((p) => p.configured).length})` : ""}
            </button>
          </div>

          {tab === "models" && (
            <div className="ai-models">
              <p className="muted small">Choose the models this organization uses, in fallback order (if the first fails or is rate
                limited, the next is tried). Any model the provider offers works: they run through LiteLLM. Leave a group empty to use
                the server's defaults ({s.defaults.reasoning.join(" → ") || "none"}).</p>
              {(["reasoning", "fast"] as AIGroup[]).map((group) => (
                <section key={group} className="ai-group">
                  <div className="row between">
                    <span><b>{GROUP_INFO[group].title}</b> <span className="muted tiny">{GROUP_INFO[group].hint}</span></span>
                    <span className="row gap">
                      {(() => { const t = tests[group]; return t === "running" ? <span className="muted small pulse">Testing…</span>
                        : t ? <span className={`small ${t.ok ? "ok" : "error"}`} title={t.chain?.join(" → ")}>
                          {t.ok ? `✓ ${t.model} answered in ${t.latency_ms} ms` : `✗ ${t.error}`}</span> : null; })()}
                      <button type="button" className="small" onClick={() => runTest(group)} disabled={tests[group] === "running"}
                        title="Save first: the test uses the saved settings">Test</button>
                    </span>
                  </div>
                  {config[group].length === 0 && <p className="muted small">Server defaults: {s.defaults[group].join(" → ") || "none"}</p>}
                  {config[group].map((entry, i) => {
                    const p = info(entry.provider);
                    return (
                      <div key={i} className="ai-model-row">
                        <span className="ai-order">{i + 1}</span>
                        <Select value={entry.provider} aria-label="Provider" className="ai-provider"
                          onChange={(v) => setChain(group, config[group].map((x, k) => (k === i ? { provider: v, model: "" } : x)))}
                          options={catalog.map((c) => ({ value: c.id, label: c.label }))} />
                        <input className="grow mono" value={entry.model} aria-label="Model id" placeholder={p?.examples[group] ?? "model id"}
                          onChange={(e) => setChain(group, config[group].map((x, k) => (k === i ? { ...x, model: e.target.value } : x)))} />
                        <button type="button" className="ghost tiny-btn" disabled={i === 0} aria-label="Move up"
                          onClick={() => { const c = [...config[group]]; [c[i - 1], c[i]] = [c[i], c[i - 1]]; setChain(group, c); }}><Icon as={ArrowUp} size={12} /></button>
                        <button type="button" className="ghost tiny-btn" aria-label="Remove"
                          onClick={() => setChain(group, config[group].filter((_, k) => k !== i))}><Icon as={X} size={12} /></button>
                      </div>
                    );
                  })}
                  {config[group].length < 4 && (
                    <button type="button" className="small" onClick={() => setChain(group, [...config[group],
                      { provider: config[group][0]?.provider ?? "gemini", model: "" }])}>+ Add {config[group].length ? "fallback" : "model"}</button>
                  )}
                </section>
              ))}
              {usedProviders.filter((p) => Object.keys(info(p)?.params ?? {}).length).map((p) => (
                <section key={p} className="ai-group">
                  <b>{info(p)?.label} settings</b>
                  {Object.entries(info(p)!.params).map(([name, hint]) => (
                    <label key={name} className="field"><span>{name === "api_base" ? "Endpoint" : "API version"} <span className="muted tiny">{hint}</span></span>
                      <input value={config.params[p]?.[name] ?? ""} placeholder={hint.split("e.g. ")[1] ?? ""}
                        onChange={(e) => setConfig((c) => ({ ...c, params: { ...c.params, [p]: { ...(c.params[p] ?? {}), [name]: e.target.value } } }))} />
                    </label>
                  ))}
                </section>
              ))}
              {keyless.length > 0 && (
                <p className="warn small">No key yet for {keyless.map((p) => info(p)?.label).join(", ")}: add it under Provider keys.</p>
              )}
              <div className="row end gap">
                {saved && <span className="small ok">{saved}</span>}
                <button className="primary" onClick={saveConfig}>Save models</button>
              </div>
            </div>
          )}

          {tab === "keys" && (
            <form className="ai-keys" onSubmit={(e) => { e.preventDefault(); saveKeys(pendingKeys); }}>
              {!s.enabled && (
                <p className="warn small">Keys cannot be stored: this server has no ARCHFLOW_SECRET_KEY and no writable data directory.
                  Set ARCHFLOW_SECRET_KEY in the API's environment and restart it.</p>
              )}
              <p className="muted small">Keys are encrypted at rest and never shown again (only their last 4 characters). An
                organization key replaces the server's key for that provider. Leave a field empty to keep the current key.</p>
              {catalog.map((p) => {
                const cur = s.providers[p.id];
                return (
                  <div key={p.id} className="ai-key-row">
                    <span className="ai-key-label">
                      <b>{p.label}</b>
                      <span className="muted tiny">
                        {cur?.configured ? `Organization key …${cur.last4}` : p.server_key ? "Using the server's key" : p.key === "optional" ? "Key optional" : "No key"}
                        {p.docs && <> · <a href={p.docs} target="_blank" rel="noopener noreferrer">get a key</a></>}
                      </span>
                    </span>
                    <input type="password" autoComplete="off" aria-label={`${p.label} API key`} disabled={!s.enabled}
                      placeholder={cur?.configured ? "Enter a new key to replace it" : "Paste API key"}
                      value={keyDraft[p.id] ?? ""} onChange={(e) => setKeyDraft({ ...keyDraft, [p.id]: e.target.value })} />
                    {cur?.configured
                      ? <button type="button" className="small danger" onClick={() => saveKeys({ [p.id]: null })}>Remove</button>
                      : <span className="ai-key-spacer" />}
                  </div>
                );
              })}
              <div className="row end gap">
                {saved && <span className="small ok">{saved}</span>}
                <button className="primary" disabled={!Object.keys(pendingKeys).length || !s.enabled}>Save keys</button>
              </div>
            </form>
          )}
        </>
      )}
      {error && <p className="error small">{error}</p>}
    </Modal>
  );
}

/**
 * Jira Cloud connection for an organization (admins): site, account email, API token (write-only, encrypted) and the
 * default project. Developers then preview and push requirements from a project's Requirements panel.
 */
export function JiraSettingsDialog({ orgId, orgName, onClose }: Base) {
  const [s, setS] = useState<JiraSettings | null>(null);
  const [form, setForm] = useState({ site_url: "", email: "", api_token: "", project_key: "" });
  const [test, setTest] = useState<JiraTestResult | "running" | null>(null);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState("");

  const load = (next: JiraSettings) => {
    setS(next);
    setForm({ site_url: next.site_url, email: next.email, api_token: "", project_key: next.project_key });
  };
  useEffect(() => { api.jiraSettings(orgId).then(load).catch((e) => setError(e.message)); }, [orgId]);
  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) => setForm({ ...form, [k]: e.target.value });

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(""); setTest(null);
    try {
      load(await api.saveJiraSettings(orgId, { ...form, api_token: form.api_token.trim() || null }));
      setSaved("Saved"); window.setTimeout(() => setSaved(""), 2500);
    } catch (err) { setError((err as Error).message); }
  };
  const runTest = async () => {
    setTest("running");
    try { setTest(await api.testJira(orgId)); } catch (err) { setTest({ ok: false, error: (err as Error).message }); }
  };
  const disconnect = async () => {
    if (!window.confirm(`Disconnect Jira from ${orgName}? Issues already pushed stay in Jira and stay linked.`)) return;
    try { await api.deleteJiraSettings(orgId); load(await api.jiraSettings(orgId)); } catch (err) { setError((err as Error).message); }
  };

  return (
    <Modal title={`Jira · ${orgName}`} onClose={onClose}>
      {!s && !error && <p className="muted small">Loading…</p>}
      {s && (
        <form onSubmit={save}>
          {!s.enabled && <p className="warn small">The token cannot be stored: set ARCHFLOW_SECRET_KEY in the API's environment and restart it.</p>}
          <p className="muted small">Requirements become stories under one epic per component, with Given/When/Then acceptance criteria, story
            points and sub-tasks from the low-level design. Pushes always show a preview first and update existing issues instead of
            duplicating them. The API token is encrypted at rest and never shown again (only its last 4 characters).</p>
          <label className="field"><span>Site URL</span>
            <input value={form.site_url} onChange={set("site_url")} placeholder="https://your-team.atlassian.net" required />
          </label>
          <label className="field"><span>Account email</span>
            <input type="email" value={form.email} onChange={set("email")} placeholder="jira-bot@your-company.com" required />
          </label>
          <label className="field"><span>API token <span className="muted tiny">
            {s.configured ? `Stored token …${s.last4}. Leave empty to keep it.` : <>Create one at <a href="https://id.atlassian.com/manage-profile/security/api-tokens" target="_blank" rel="noopener noreferrer">id.atlassian.com</a></>}</span></span>
            <input type="password" autoComplete="off" value={form.api_token} onChange={set("api_token")}
              placeholder={s.configured ? "Enter a new token to replace it" : "Paste API token"} disabled={!s.enabled} />
          </label>
          <label className="field"><span>Default project key <span className="muted tiny">e.g. SHOP; a push can pick another</span></span>
            <input className="mono" value={form.project_key} onChange={(e) => setForm({ ...form, project_key: e.target.value.toUpperCase() })} maxLength={10} />
          </label>
          <div className="row between">
            <span className="row gap">
              {s.configured && <button type="button" className="small" onClick={runTest} disabled={test === "running"}
                title="Uses the saved settings">Test connection</button>}
              {test === "running" ? <span className="muted small pulse">Testing…</span>
                : test ? <span className={`small ${test.ok ? "ok" : "error"}`}>
                  {test.ok ? `✓ Signed in as ${test.account}${test.project ? ` · ${test.project.key} (${test.project.name}): ${test.project.issue_types.join(", ")}${test.project.story_points ? ", story points" : ""}` : ""}`
                    : `✗ ${test.error}`}</span> : null}
            </span>
            <span className="row gap">
              {saved && <span className="small ok">{saved}</span>}
              {s.configured && <button type="button" className="small danger" onClick={disconnect}>Disconnect</button>}
              <button className="primary" disabled={!s.enabled}>Save</button>
            </span>
          </div>
        </form>
      )}
      {error && <p className="error small">{error}</p>}
    </Modal>
  );
}

export const FEATURE_LABEL: Record<string, string> = {
  assistant: "Assistant (Ask)", "canvas-edit": "Canvas edits", "design-chat": "Design chat", openspec: "Vibe code (OpenSpec)",
  "low-level-design": "Low-level design", decisions: "Decisions", "code-analysis": "Code analysis", memory: "Memory & summaries",
  mcp: "MCP agents", suggestions: "Suggestions & follow-ups", review: "AI review", intake: "Document import", eval: "Evals", jira: "Jira stories", other: "Other",
};
export const usd = (n: number) => (n < 0.01 && n > 0 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`);
export const tokens = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n));

function UsageTable({ title, rows, label }: { title: string; rows: AIUsageRow[]; label: (r: AIUsageRow) => string }) {
  if (!rows.length) return null;
  const max = Math.max(...rows.map((r) => r.cost_usd), 0.000001);
  return (
    <div className="usage-table">
      <b className="small">{title}</b>
      <table>
        <thead><tr><th>Name</th><th>Calls</th><th>Tokens in / out</th><th>Cost</th></tr></thead>
        <tbody>
          {rows.slice(0, 8).map((r) => (
            <tr key={r.key || "none"}>
              <td><span className="usage-bar" style={{ width: `${Math.max(2, (r.cost_usd / max) * 100)}%` }} />{label(r)}</td>
              <td>{r.calls}</td><td>{tokens(r.prompt_tokens)} / {tokens(r.completion_tokens)}</td><td>{usd(r.cost_usd)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** AI spend for an organization (estimated from tokens) and its monthly budget. Admins only. */
export function AIUsageDialog({ orgId, orgName, onClose }: Base) {
  const [report, setReport] = useState<AIUsageReport | null>(null);
  const [limit, setLimit] = useState("");
  const [alert, setAlert] = useState(80);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);

  const show = (r: AIUsageReport) => {
    setReport(r);
    setLimit(r.budget.monthly_limit_usd == null ? "" : String(r.budget.monthly_limit_usd));
    setAlert(r.budget.alert_percent);
  };
  useEffect(() => { api.aiUsage(orgId).then(show).catch((e) => setError(e.message)); }, [orgId]);

  const save = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(""); setSaved(false);
    const value = limit.trim() === "" ? null : Number(limit);
    if (value !== null && (Number.isNaN(value) || value < 0)) { setError("Enter a positive amount, or leave it empty for no limit."); return; }
    try { show(await api.setAiBudget(orgId, value, alert)); setSaved(true); }
    catch (err) { setError((err as Error).message); }
  };

  const r = report;
  const maxDay = r ? Math.max(...r.daily.map((d) => d.cost_usd), 0.000001) : 1;
  return (
    <Modal title={`AI usage & budget · ${orgName}`} onClose={onClose}>
      {!r && !error && <p className="muted small">Loading…</p>}
      {error && <p className="error small">{error}</p>}
      {r && (
        <>
          <div className={`usage-month ${r.status}`}>
            <div className="row between">
              <span><b>{usd(r.month_spend_usd)}</b> spent this month
                {r.budget.monthly_limit_usd != null && <> of <b>{usd(r.budget.monthly_limit_usd)}</b></>}</span>
              <span className={`usage-status ${r.status}`}>
                {r.status === "blocked" ? "Budget used up: AI paused" : r.status === "warning" ? `${r.used_percent}% of budget` : r.used_percent != null ? `${r.used_percent}% used` : "No limit"}
              </span>
            </div>
            {r.used_percent != null && <div className="usage-meter"><span style={{ width: `${Math.min(100, r.used_percent)}%` }} /></div>}
            {r.status === "blocked" && <p className="small">AI features use their built-in non-AI behaviour until the budget is raised or the month ends.</p>}
          </div>

          <form className="usage-budget" onSubmit={save}>
            <label className="field"><span>Monthly budget (USD)</span>
              <input type="number" min="0" step="0.01" placeholder="No limit" value={limit} onChange={(e) => setLimit(e.target.value)} />
            </label>
            <label className="field"><span>Warn at (% of budget)</span>
              <input type="number" min="1" max="100" value={alert} onChange={(e) => setAlert(Number(e.target.value))} />
            </label>
            <div className="row gap">
              <button className="primary">Save budget</button>
              {saved && <span className="small ok">Saved</span>}
            </div>
          </form>

          <p className="muted small">Last {r.days} days: {r.total.calls} AI calls, {tokens(r.total.prompt_tokens)} tokens in,
            {" "}{tokens(r.total.completion_tokens)} out, {usd(r.total.cost_usd)}. Costs are estimates from token counts and
            published prices (streamed answers are estimated from their length).</p>
          {r.daily.length > 0 && (
            <div className="usage-days" aria-label="Daily AI cost">
              {[...r.daily].sort((a, b) => a.key.localeCompare(b.key)).map((d) => (
                <span key={d.key} title={`${d.key}: ${usd(d.cost_usd)} · ${d.calls} calls`}
                  style={{ height: `${Math.max(4, (d.cost_usd / maxDay) * 100)}%` }} />
              ))}
            </div>
          )}
          <div className="usage-grid">
            <UsageTable title="By feature" rows={r.by_feature} label={(x) => FEATURE_LABEL[x.key] ?? x.key} />
            <UsageTable title="By model" rows={r.by_model} label={(x) => x.key || "unknown"} />
            <UsageTable title="By project" rows={r.by_project} label={(x) => x.name ?? x.key} />
            {r.by_user && r.by_user.length > 0 && <UsageTable title="By person" rows={r.by_user} label={(x) => x.name ?? x.key} />}
            {r.by_prompt && r.by_prompt.some((x) => x.key) && (
              <UsageTable title="By prompt version" rows={r.by_prompt.filter((x) => x.key)} label={(x) => x.key} />
            )}
          </div>
          {r.total.calls === 0 && <p className="muted small">No AI calls recorded yet.</p>}
        </>
      )}
    </Modal>
  );
}
