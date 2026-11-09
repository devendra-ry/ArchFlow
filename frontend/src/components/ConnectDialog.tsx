import { useEffect, useState } from "react";
import { Icon, X } from "../icons";
import { api } from "../api";
import { useEscape } from "../useEscape";
import { Select } from "./Select";
import type { ApiToken } from "../types";

interface Props { orgId: string; orgName?: string; canManage: boolean; onClose: () => void }

export function Copy({ text, label = "Copy" }: { text: string; label?: string }) {
  const [done, setDone] = useState(false);
  return (
    <button type="button" className="small" onClick={async () => {
      try { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500); } catch { /* ignore */ }
    }}>{done ? "Copied ✓" : label}</button>
  );
}

function Snippet({ code }: { code: string }) {
  return (
    <div className="snippet">
      <pre>{code}</pre>
      <Copy text={code} />
    </div>
  );
}

const ago = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "never");

/** How to connect Claude and other MCP clients, plus personal access tokens and connected apps. */
export function ConnectDialog({ orgId, orgName, canManage, onClose }: Props) {
  const escRef = useEscape(onClose);
  const [url, setUrl] = useState("");
  const [tab, setTab] = useState<"claude-code" | "claude-ai" | "other">("claude-code");
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [name, setName] = useState("");
  const [access, setAccess] = useState<"read" | "write">("write");
  const [expiry, setExpiry] = useState<string>("90");
  const [created, setCreated] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [org, setOrg] = useState(orgName ?? orgId);

  const load = () => { if (canManage) api.tokens(orgId).then(setTokens).catch((e) => setError(e.message)); };
  useEffect(() => {
    api.mcpInfo().then((i) => setUrl(i.url)).catch(() => setUrl(`${location.protocol}//${location.hostname}:8000/mcp`));
    api.orgs().then((os) => setOrg(os.find((o) => o.id === orgId)?.name ?? orgId)).catch(() => {});
    load();
  }, [orgId]); // eslint-disable-line react-hooks/exhaustive-deps

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      const t = await api.createToken(orgId, name.trim(), access, expiry ? Number(expiry) : null);
      setCreated(t.token);
      setName("");
      load();
    } catch (err) { setError((err as Error).message); }
  };
  const revoke = async (id: string) => {
    try { await api.revokeToken(orgId, id); load(); } catch (err) { setError((err as Error).message); }
  };

  const isLocal = /localhost|127\.0\.0\.1|\[::1\]/.test(url);
  const pats = tokens.filter((t) => t.kind === "pat");
  const apps = tokens.filter((t) => t.kind === "refresh");

  return (
    <div className="modal-backdrop" onClick={onClose} ref={escRef}>
      <div className="modal wide" role="dialog" aria-label="Connect AI agents" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Connect AI agents over MCP</h3>
          <button className="ghost" onClick={onClose} aria-label="Close"><Icon as={X} size={16} /></button>
        </div>
        <p className="muted small">
          Agents get the same tools as this canvas: read and edit the architecture, requirements and versions, and pull
          implementation tasks. Their edits appear live here, attributed to them. Access is limited to <b>{org}</b>.
        </p>
        <div className="row gap"><span className="small">MCP server URL</span><code className="url">{url}</code>{url && <Copy text={url} />}</div>

        <nav className="tabs inline">
          {([["claude-code", "Claude Code"], ["claude-ai", "Claude.ai / Desktop"], ["other", "Other clients"]] as const).map(([k, l]) => (
            <button key={k} className={tab === k ? "on" : ""} onClick={() => setTab(k)}>{l}</button>
          ))}
        </nav>
        {tab === "claude-code" && (
          <div className="connect-body">
            <p className="small">Add the server, then run <code>/mcp</code> in Claude Code and choose <b>Authenticate</b>. A browser window
              opens where you pick the organization and access level.</p>
            <Snippet code={`claude mcp add --transport http archflow ${url}`} />
            <p className="small muted">Prefer a token (CI, headless)? Create one below and use:</p>
            <Snippet code={`claude mcp add --transport http archflow ${url} --header "Authorization: Bearer <your token>"`} />
          </div>
        )}
        {tab === "claude-ai" && (
          <div className="connect-body">
            <p className="small">In Claude, open <b>Settings → Connectors → Add custom connector</b>, paste the URL and connect. You'll be
              sent here to approve access.</p>
            {isLocal && (
              <p className="small warn">Claude.ai and Claude Desktop connect from the internet, so they can't reach <code>localhost</code>.
                Expose the API over HTTPS (e.g. a Cloudflare Tunnel or ngrok to port 8000), set <code>ARCHFLOW_PUBLIC_URL</code> to that
                address and <code>ARCHFLOW_MCP_ALLOWED_HOSTS</code> to its host name, restart the API, then use <code>https://…/mcp</code>.</p>
            )}
          </div>
        )}
        {tab === "other" && (
          <div className="connect-body">
            <p className="small">Any MCP client that supports Streamable HTTP works. Clients with OAuth support discover everything from the URL.
              Otherwise send a personal access token as a header:</p>
            <Snippet code={`URL:    ${url}\nHeader: Authorization: Bearer <your token>`} />
          </div>
        )}

        {!canManage && (
          <p className="muted small">Personal access tokens and the list of connected apps are managed by your organization's admins.
            You can still connect through the browser sign-in above; the agent gets at most your own access level.</p>
        )}
        {canManage && (<>
        <h4>Personal access tokens</h4>
        {created && (
          <div className="token-created">
            <span className="small"><b>Copy this token now.</b> It won't be shown again.</span>
            <div className="snippet"><pre>{created}</pre><Copy text={created} /></div>
            <button className="small" onClick={() => setCreated(null)}>Done</button>
          </div>
        )}
        <form className="row gap" onSubmit={create}>
          <input className="grow" placeholder="Token name, e.g. CI pipeline" value={name} onChange={(e) => setName(e.target.value)} aria-label="Token name" />
          <Select value={access} onChange={setAccess} aria-label="Access"
            options={[{ value: "write", label: "Read & edit" }, { value: "read", label: "Read only" }]} />
          <Select value={expiry} onChange={setExpiry} aria-label="Expiry"
            options={[{ value: "30", label: "30 days" }, { value: "90", label: "90 days" }, { value: "365", label: "1 year" }, { value: "", label: "No expiry" }]} />
          <button className="primary" disabled={!name.trim()}>Create token</button>
        </form>
        <TokenTable rows={pats} empty="No tokens yet." onRevoke={revoke} kind="pat" />
        <h4>Connected apps</h4>
        <p className="muted tiny">Revoking disconnects the app immediately; it has to be approved again to reconnect.</p>
        <TokenTable rows={apps} empty="No apps connected through OAuth yet." onRevoke={revoke} kind="app" />
        </>)}
        {error && <p className="error small">{error}</p>}
      </div>
    </div>
  );
}

function TokenTable({ rows, empty, onRevoke, kind }: { rows: ApiToken[]; empty: string; onRevoke: (id: string) => void; kind: "pat" | "app" }) {
  const [confirm, setConfirm] = useState<string | null>(null);
  if (!rows.length) return <p className="muted small">{empty}</p>;
  return (
    <table className="tokens">
      <thead><tr><th>{kind === "pat" ? "Name" : "App"}</th><th>User</th><th>Access</th><th>Last used</th><th>Expires</th><th /></tr></thead>
      <tbody>
        {rows.map((t) => (
          <tr key={t.id}>
            <td>{kind === "pat" ? t.name : t.client_name}</td>
            <td>{t.user_name}</td>
            <td>{t.access === "write" ? "Read & edit" : "Read only"}</td>
            <td>{ago(t.last_used_at)}</td>
            <td>{t.expires_at ? new Date(t.expires_at * 1000).toLocaleDateString() : "never"}</td>
            <td>
              {confirm === t.id ? (
                <span className="row gap"><button className="small danger" onClick={() => { onRevoke(t.id); setConfirm(null); }}>Revoke</button>
                  <button className="small" onClick={() => setConfirm(null)}>Cancel</button></span>
              ) : <button className="small" onClick={() => setConfirm(t.id)}>Revoke…</button>}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
