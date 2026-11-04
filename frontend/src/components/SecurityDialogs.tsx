import { useEffect, useState } from "react";
import { api } from "../api";
import { ROLES, type AuthSettings, type MfaStatus, type ProjectRole, type ProjectRoles, type Role } from "../types";
import { Copy } from "./ConnectDialog";
import { Modal } from "./OrgDialogs";

const PROJECT_ROLE_LABEL: Record<ProjectRole, string> = {
  viewer: "Viewer", reviewer: "Reviewer", developer: "Editor", admin: "Admin",
};

/** Space the secret in groups of 4 so it is easy to type into an authenticator app. */
const grouped = (s: string) => s.replace(/(.{4})/g, "$1 ").trim();

function RecoveryCodes({ codes }: { codes: string[] }) {
  return (
    <div className="token-created">
      <span className="small"><b>Save these recovery codes now.</b> Each one works once if you lose your phone. They won't be shown again.</span>
      <div className="snippet"><pre className="recovery-codes">{codes.join("\n")}</pre><Copy text={codes.join("\n")} /></div>
    </div>
  );
}

/**
 * Turn two-step sign-in on (scan or type the secret, then confirm a code) or off, and make new recovery codes.
 * Used from the account menu and, when an organization requires it, as a full-screen step.
 */
export function MfaSetup({ onDone }: { onDone?: (on: boolean) => void }) {
  const [status, setStatus] = useState<MfaStatus | null>(null);
  const [setup, setSetup] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [codes, setCodes] = useState<string[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => { api.mfaStatus().then(setStatus).catch((e) => setError(e.message)); }, []);

  const run = async (fn: () => Promise<void>) => {
    setError(""); setBusy(true);
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const start = () => run(async () => { setSetup(await api.mfaSetup()); setCode(""); });
  const enable = (e: React.FormEvent) => {
    e.preventDefault();
    run(async () => {
      const s = await api.mfaEnable(code.trim());
      setStatus(s); setCodes(s.recovery_codes ?? null); setSetup(null); setCode(""); onDone?.(true);
    });
  };
  const disable = () => run(async () => { setStatus(await api.mfaDisable(code.trim())); setCode(""); setCodes(null); onDone?.(false); });
  const newCodes = () => run(async () => { const s = await api.mfaRecoveryCodes(code.trim()); setStatus(s); setCodes(s.recovery_codes ?? null); setCode(""); });

  if (!status) return error ? <p className="error small">{error}</p> : <p className="muted small">Loading…</p>;
  if (!status.available && !status.enabled) {
    return <p className="muted small">Two-step sign-in is not available on this server yet. Ask your administrator to set ARCHFLOW_SECRET_KEY.</p>;
  }
  return (
    <div className="mfa-setup">
      {status.enabled ? (
        <>
          <p className="small"><span className="ok">● On.</span> You enter a code from your authenticator app when you sign in.
            {" "}{status.recovery_codes_left} recovery code{status.recovery_codes_left === 1 ? "" : "s"} left.</p>
          {codes && <RecoveryCodes codes={codes} />}
          <label className="field"><span>Current code from your app</span>
            <input inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.target.value)} placeholder="123456" />
          </label>
          <div className="row gap wrap">
            <button className="small" disabled={busy || !code.trim()} onClick={newCodes}>New recovery codes</button>
            {status.required_by.length === 0
              ? <button className="small danger" disabled={busy || !code.trim()} onClick={disable}>Turn off</button>
              : <span className="muted small">{status.required_by[0].name} requires it, so it stays on.</span>}
          </div>
        </>
      ) : !setup ? (
        <>
          <p className="small">Add a second step to sign-in: a 6-digit code from an authenticator app such as Google Authenticator,
            Microsoft Authenticator or 1Password.</p>
          <button className="primary" disabled={busy} onClick={start}>Set up two-step sign-in</button>
        </>
      ) : (
        <form onSubmit={enable}>
          <ol className="small mfa-steps">
            <li>In your authenticator app, add an account with this key (or <a href={setup.otpauth_uri}>open it in the app</a> on this device):
              <div className="snippet"><pre className="mfa-secret">{grouped(setup.secret)}</pre><Copy text={setup.secret} /></div>
            </li>
            <li>Enter the 6-digit code the app shows.</li>
          </ol>
          <div className="row gap">
            <input className="grow" inputMode="numeric" autoComplete="one-time-code" required autoFocus value={code}
              onChange={(e) => setCode(e.target.value)} placeholder="123456" aria-label="Code from your app" />
            <button className="primary" disabled={busy || !code.trim()}>Turn on</button>
          </div>
        </form>
      )}
      {!status.enabled && codes && <RecoveryCodes codes={codes} />}
      {error && <p className="error small" role="alert">{error}</p>}
    </div>
  );
}

export function MfaDialog({ onClose }: { onClose: () => void }) {
  return <Modal title="Two-step sign-in" onClose={onClose}><MfaSetup /></Modal>;
}

/** Full-screen step when an organization requires two-step sign-in and this account has not turned it on. */
export function MfaRequiredScreen({ onDone, onLogout, themeToggle }: { onDone: () => void; onLogout: () => void; themeToggle?: React.ReactNode }) {
  const [enabled, setEnabled] = useState(false);
  return (
    <div className="picker auth">
      <header className="picker-head">
        <div className="row between"><span className="brand"><span className="logo">▲</span> ArchFlow</span>{themeToggle}</div>
      </header>
      <div className="card auth-card">
        <h2>Turn on two-step sign-in</h2>
        <p className="small muted">Your organization requires it. It takes about a minute.</p>
        <MfaSetup onDone={setEnabled} />
        <div className="row between">
          <button type="button" className="link" onClick={onLogout}>Sign out</button>
          <button className="primary" disabled={!enabled} onClick={onDone}>Continue</button>
        </div>
      </div>
    </div>
  );
}

/** Org admins: single sign-on (OIDC) and the require-MFA rule. The client secret is write-only. */
export function SignInSettingsDialog({ orgId, orgName, role, onClose }: { orgId: string; orgName: string; role: Role; onClose: () => void }) {
  const [s, setS] = useState<AuthSettings | null>(null);
  const [enabled, setEnabled] = useState(true);
  const [issuer, setIssuer] = useState("");
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [domains, setDomains] = useState("");
  const [defaultRole, setDefaultRole] = useState<Role>("developer");
  const [requireSso, setRequireSso] = useState(false);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);

  const show = (a: AuthSettings) => {
    setS(a);
    setEnabled(a.sso.enabled || !a.sso.issuer); setIssuer(a.sso.issuer); setClientId(a.sso.client_id);
    setDomains(a.sso.domains.join(", ")); setDefaultRole(a.sso.default_role); setRequireSso(a.sso.require_sso); setSecret("");
  };
  useEffect(() => { api.authSettings(orgId).then(show).catch((e) => setError(e.message)); }, [orgId]);

  const run = async (fn: () => Promise<void>) => {
    setError(""); setNote(""); setBusy(true);
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(false); }
  };
  const save = (e: React.FormEvent) => {
    e.preventDefault();
    run(async () => {
      show(await api.saveSso(orgId, {
        enabled, issuer: issuer.trim(), client_id: clientId.trim(), client_secret: secret.trim() || undefined,
        domains: domains.split(/[\s,]+/).filter(Boolean), default_role: defaultRole, require_sso: requireSso,
      }));
      setNote("Saved");
    });
  };

  return (
    <Modal title={`Sign-in & security · ${orgName}`} onClose={onClose}>
      {!s ? (error ? <p className="error small">{error}</p> : <p className="muted small">Loading…</p>) : (
        <div className="signin-settings">
          <section>
            <h4>Two-step sign-in</h4>
            <label className="small row gap">
              <input type="checkbox" checked={s.require_mfa} disabled={busy}
                onChange={(e) => run(async () => show(await api.setRequireMfa(orgId, e.target.checked)))} />
              Require two-step sign-in for every member
            </label>
            <p className="muted small">Members without it are asked to turn it on before they can open this organization.</p>
          </section>

          <form onSubmit={save}>
            <h4>Single sign-on (OpenID Connect)</h4>
            <p className="muted small">Works with Okta, Microsoft Entra ID, Google Workspace, Auth0, Keycloak and other OIDC providers.
              Create an app there and use this redirect URL:</p>
            <div className="snippet"><pre>{s.callback_url}</pre><Copy text={s.callback_url} /></div>
            <label className="small row gap"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Turn on single sign-on</label>
            <label className="field"><span>Issuer URL</span>
              <input type="url" placeholder="https://login.example.com" value={issuer} onChange={(e) => setIssuer(e.target.value)} />
            </label>
            <div className="sso-grid">
              <label className="field"><span>Client ID</span>
                <input value={clientId} onChange={(e) => setClientId(e.target.value)} autoComplete="off" />
              </label>
              <label className="field"><span>Client secret{s.sso.secret_set ? ` (saved, ends in ${s.sso.secret_last4})` : ""}</span>
                <input type="password" value={secret} onChange={(e) => setSecret(e.target.value)} autoComplete="new-password"
                  placeholder={s.sso.secret_set ? "Leave empty to keep it" : ""} />
              </label>
            </div>
            <label className="field"><span>Email domains that use SSO</span>
              <input placeholder="acme.com, acme.io" value={domains} onChange={(e) => setDomains(e.target.value)} />
            </label>
            <label className="field"><span>Role for people who sign in for the first time</span>
              <select value={defaultRole} onChange={(e) => setDefaultRole(e.target.value as Role)}>
                {ROLES.filter((r) => r !== "owner" && (r !== "admin" || role === "owner" || s.sso.default_role === "admin"))
                  .map((r) => <option key={r} value={r}>{r}</option>)}
              </select>
            </label>
            <label className="small row gap">
              <input type="checkbox" checked={requireSso} disabled={!enabled} onChange={(e) => setRequireSso(e.target.checked)} />
              Require SSO (no password sign-in for this organization; owners can still use a password)
            </label>
            {!s.secrets_enabled && <p className="error small">The server cannot store secrets yet: set ARCHFLOW_SECRET_KEY.</p>}
            <div className="row gap wrap">
              <button className="primary small" disabled={busy}>Save</button>
              {s.sso.issuer && <button type="button" className="small" disabled={busy}
                onClick={() => run(async () => { const r = await api.testSso(orgId); if (r.ok) setNote(r.detail); else setError(r.detail); })}>Test connection</button>}
              {s.sso.issuer && <button type="button" className="small danger" disabled={busy}
                onClick={() => { if (window.confirm("Remove single sign-on for this organization?")) run(async () => { await api.deleteSso(orgId); show(await api.authSettings(orgId)); }); }}>Remove</button>}
              {note && <span className="small ok">{note}</span>}
            </div>
          </form>
        </div>
      )}
      {error && <p className="error small" role="alert">{error}</p>}
    </Modal>
  );
}

/** Give a member a different role on this project only. Org owners always keep full access. */
export function ProjectAccessDialog({ pid, projectName, selfId, onClose, onChanged }: {
  pid: string; projectName: string; selfId: string; onClose: () => void; onChanged?: () => void;
}) {
  const [data, setData] = useState<ProjectRoles | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { api.projectRoles(pid).then(setData).catch((e) => setError(e.message)); }, [pid]);

  const change = async (userId: string, value: string) => {
    setError("");
    try {
      setData(value ? await api.setProjectRole(pid, userId, value as ProjectRole) : await api.clearProjectRole(pid, userId));
      if (userId === selfId) onChanged?.();
    } catch (e) { setError((e as Error).message); }
  };

  return (
    <Modal title={`Access to ${projectName}`} onClose={onClose}>
      <p className="muted small">By default people have their organization role. Pick a different role to change it for this project only.</p>
      {data && (
        <table className="tokens members project-access">
          <thead><tr><th>Name</th><th>Organization role</th><th>On this project</th></tr></thead>
          <tbody>
            {data.members.map((m) => {
              const locked = !data.can_manage || m.org_role === "owner";
              return (
                <tr key={m.user_id}>
                  <td>{m.name}{m.user_id === selfId && <span className="muted small"> (you)</span>}<div className="muted tiny">{m.email}</div></td>
                  <td className="small">{m.org_role}</td>
                  <td>
                    {locked ? (
                      <span className={m.project_role ? "role-chip" : "small"}>
                        {m.project_role ? PROJECT_ROLE_LABEL[m.project_role] : m.org_role === "owner" ? "owner (always)" : "same as organization"}
                      </span>
                    ) : (
                      <select value={m.project_role ?? ""} aria-label={`Project role of ${m.name}`} onChange={(e) => change(m.user_id, e.target.value)}>
                        <option value="">Same as organization</option>
                        {data.roles.map((r) => <option key={r} value={r}>{PROJECT_ROLE_LABEL[r]}</option>)}
                      </select>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {data && !data.can_manage && <p className="muted small">Only organization admins and project admins can change access.</p>}
      {error && <p className="error small" role="alert">{error}</p>}
    </Modal>
  );
}
