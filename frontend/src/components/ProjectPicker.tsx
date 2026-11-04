import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { getRecent, pruneRecent, timeAgo } from "../recent";
import { can, type AISettings, type Me, type Project, type User } from "../types";
import { AISettingsDialog, AIUsageDialog, JiraSettingsDialog, MembersDialog } from "./OrgDialogs";
import { IntegrationsDialog } from "./IntegrationsDialog";
import { TracesDialog } from "./TracesDialog";
import { UserMenu } from "./UserMenu";
import { Select } from "./Select";

interface Props {
  me: Me;
  user: User;
  colors: string[];
  onColor: (c: string) => void;
  onOpen: (pid: string) => void;
  onLogout: () => void;
  onMeChanged: () => void;
  notice?: string;
  themeToggle?: React.ReactNode;
}

const TEMPLATE_HINT: Record<string, string> = {
  blank: "Start from an empty canvas, or describe your idea in the design chat.",
  "ai-customer-support": "A worked example: channels, gateway, AI agent, MCP tools, orders and helpdesk.",
};

function greeting(): string {
  const h = new Date().getHours();
  return h < 5 ? "Working late" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

/** Home: your projects (recent first), quick create, and the organization's AI and team settings. */
export function ProjectPicker({ me, user, colors, onColor, onOpen, onLogout, onMeChanged, notice, themeToggle }: Props) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [allProjects, setAllProjects] = useState<Project[]>([]);
  const [templates, setTemplates] = useState<{ key: string; name: string }[]>([]);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [template, setTemplate] = useState("blank");
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<"recent" | "name" | "created">("recent");
  const [ai, setAi] = useState<{ available: boolean; model: string; admin?: AISettings } | null>(null);
  const searchRef = useRef<HTMLInputElement>(null);
  const orgs = me.orgs;
  const [orgId, setOrgId] = useState(() => {
    let saved = "";
    try { saved = localStorage.getItem("archflow.org") ?? ""; } catch { /* ignore */ }
    return orgs.some((o) => o.id === saved) ? saved : orgs[0]?.id ?? "";
  });
  const [newOrg, setNewOrg] = useState<string | null>(null);
  const [dialog, setDialog] = useState<"members" | "ai" | "usage" | "traces" | "jira" | "integrations" | null>(null);
  const org = orgs.find((o) => o.id === orgId);
  const role = org?.role;
  const isAdmin = can(role, "admin");

  useEffect(() => { if (!org && orgs[0]) setOrgId(orgs[0].id); }, [orgs, org]);
  useEffect(() => { try { localStorage.setItem("archflow.org", orgId); } catch { /* ignore */ } }, [orgId]);
  useEffect(() => {
    if (!orgId) { setProjects([]); return; }
    setProjects(null);
    api.projects(orgId).then(setProjects).catch((e) => { setProjects([]); setError(String(e.message)); });
  }, [orgId]);
  useEffect(() => {
    api.projects().then((list) => { setAllProjects(list); pruneRecent(new Set(list.map((p) => p.id))); }).catch(() => {});
  }, [orgs.length]);
  useEffect(() => { api.templates().then(setTemplates).catch(() => {}); }, []);
  // AI status: admins see their organization's own settings, everyone else the server's.
  const loadAi = () => {
    if (isAdmin && orgId) {
      api.aiSettings(orgId).then((s) => setAi({ available: s.available, model: s.effective.reasoning[0] ?? "", admin: s }))
        .catch(() => setAi(null));
    } else {
      api.health().then((h) => setAi({ available: !!h.llm?.available, model: h.llm?.groups?.reasoning?.[0] ?? "" })).catch(() => setAi(null));
    }
  };
  useEffect(loadAi, [orgId, isAdmin]); // eslint-disable-line react-hooks/exhaustive-deps
  // "/" or Ctrl/Cmd+K focuses the search box.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target as HTMLElement)?.tagName ?? "");
      if ((e.key === "/" && !typing) || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k")) {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const orgNames = useMemo(() => new Map(orgs.map((o) => [o.id, o.name])), [orgs]);
  const recentMap = useMemo(() => new Map(getRecent().map((r) => [r.pid, r])), [allProjects]);
  const q = query.trim().toLowerCase();
  const shown = useMemo(() => {
    const base = q
      ? allProjects.filter((p) => p.name.toLowerCase().includes(q) || (p.description ?? "").toLowerCase().includes(q)
          || (orgNames.get(p.org_id ?? "") ?? "").toLowerCase().includes(q))
      : projects ?? [];
    const out = [...base];
    if (sort === "name") out.sort((a, b) => a.name.localeCompare(b.name));
    else if (sort === "created") out.sort((a, b) => b.created_at.localeCompare(a.created_at));
    else out.sort((a, b) => (recentMap.get(b.id)?.at ?? 0) - (recentMap.get(a.id)?.at ?? 0) || b.created_at.localeCompare(a.created_at));
    return out;
  }, [q, allProjects, projects, sort, recentMap, orgNames]);
  const recent = useMemo(() => {
    const byId = new Map(allProjects.map((p) => [p.id, p]));
    return getRecent().filter((r) => byId.has(r.pid)).slice(0, 4).map((r) => ({ p: byId.get(r.pid)!, r }));
  }, [allProjects]);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    try {
      const p = await api.createProject(name.trim(), template, orgId);
      onOpen(p.id);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  const card = (p: Project, meta: string) => (
    <button key={p.id} className="project-card" onClick={() => onOpen(p.id)}>
      <span className="project-avatar" aria-hidden>{p.name.slice(0, 1).toUpperCase()}</span>
      <span className="project-card-text">
        <b>{p.name}</b>
        <span className="muted tiny">{meta}</span>
      </span>
      <span className="project-go" aria-hidden>→</span>
    </button>
  );

  return (
    <div className="home">
      <header className="home-bar">
        <div className="home-brand"><span className="logo">▲</span> ArchFlow</div>
        {orgs.length > 0 && (
          <Select className="home-org" value={orgId} onChange={setOrgId} aria-label="Organization"
            options={orgs.map((o) => ({ value: o.id, label: o.name }))} />
        )}
        <input ref={searchRef} className="home-search" type="search" value={query} onChange={(e) => setQuery(e.target.value)}
          placeholder="Search all projects…   /" aria-label="Search projects" />
        <span className="spacer" />
        {themeToggle}
        <UserMenu me={me} user={user} colors={colors} onColor={onColor} onLogout={onLogout} />
      </header>

      <main className="home-main">
        <section className="home-content">
          {notice && <p className="home-notice">{notice}</p>}
          <div className="home-hero">
            <div>
              <h1>{greeting()}, {me.user.name.split(" ")[0]}</h1>
              <p className="muted">Requirements → architecture → canvas → code: one structured, versioned model your team and AI edit together.</p>
            </div>
            {org && can(role, "edit") && (
              <button className="primary home-new" onClick={() => setCreating((v) => !v)} aria-expanded={creating}>+ New project</button>
            )}
          </div>

          {creating && (
            <form className="home-create" onSubmit={create}>
              <label className="field"><span>Project name</span>
                <input autoFocus value={name} maxLength={120} placeholder="e.g. Customer support AI" onChange={(e) => setName(e.target.value)} />
              </label>
              <span className="field-label small">Start from</span>
              <div className="template-grid">
                {(templates.length ? templates : [{ key: "blank", name: "Blank" }]).map((t) => (
                  <button type="button" key={t.key} className={`template-card ${template === t.key ? "on" : ""}`} onClick={() => setTemplate(t.key)}
                    aria-pressed={template === t.key}>
                    <b>{t.name}</b><span className="muted tiny">{TEMPLATE_HINT[t.key] ?? ""}</span>
                  </button>
                ))}
              </div>
              <div className="row end gap">
                <button type="button" onClick={() => setCreating(false)}>Cancel</button>
                <button className="primary" disabled={!name.trim()}>Create and open</button>
              </div>
            </form>
          )}

          {!q && recent.length > 0 && (
            <section className="home-section">
              <h2>Continue where you left off</h2>
              <div className="project-grid">
                {recent.map(({ p, r }) => card(p, [orgs.length > 1 ? orgNames.get(p.org_id ?? "") : "", `opened ${timeAgo(r.at)}`,
                  r.branch !== "main" ? `⎇ ${r.branch}` : ""].filter(Boolean).join(" · ")))}
              </div>
            </section>
          )}

          <section className="home-section">
            <div className="row between">
              <h2>{q ? `Results for “${query}”` : org ? `Projects in ${org.name}` : "Projects"}
                <span className="count">{shown.length}</span></h2>
              <Select size="sm" value={sort} onChange={setSort} aria-label="Sort projects" className="home-sort"
                options={[{ value: "recent", label: "Recently opened" }, { value: "name", label: "Name" }, { value: "created", label: "Newest" }]} />
            </div>
            {org && !can(role, "edit") && !q && <p className="muted small">You have {role} access in {org.name}: you can open and review projects, not change them.</p>}
            {projects === null && !q && <p className="muted small">Loading…</p>}
            {shown.length === 0 && projects !== null && (
              <div className="home-empty">
                <b>{q ? "No project matches." : "No projects yet."}</b>
                {!q && org && can(role, "edit") && <span className="muted small">Create one, or start from the AI customer-support example.</span>}
                {!q && org && can(role, "edit") && <button className="primary small" onClick={() => setCreating(true)}>+ New project</button>}
              </div>
            )}
            <div className="project-grid">
              {shown.map((p) => card(p, [q && orgs.length > 1 ? orgNames.get(p.org_id ?? "") : "",
                recentMap.get(p.id) ? `opened ${timeAgo(recentMap.get(p.id)!.at)}` : `created ${new Date(p.created_at).toLocaleDateString()}`,
                p.description].filter(Boolean).join(" · ")))}
            </div>
          </section>
          {error && <p className="error">{error}</p>}
        </section>

        <aside className="home-side">
          <section className={`side-card ai ${ai?.available ? "on" : "off"}`}>
            <div className="row between"><b>AI</b><span className={`ai-dot ${ai?.available ? "on" : "off"}`}>{ai ? (ai.available ? "On" : "Off") : "…"}</span></div>
            {ai?.available
              ? <p className="small">Reasoning model <code>{ai.model}</code></p>
              : <p className="small muted">{isAdmin ? "Add a provider key and choose models to turn on the design chat, edits, specs and reviews."
                : "Ask an admin to set up AI for this organization. Built-in non-AI features still work."}</p>}
            {isAdmin && (
              <div className="row gap">
                <button className="small primary" onClick={() => setDialog("ai")}>{ai?.available ? "AI settings" : "Set up AI"}</button>
                <button className="small" onClick={() => setDialog("usage")}>Usage & budget</button>
                <button className="small" onClick={() => setDialog("traces")} title="Every AI run: input, output, model and tool calls, tokens and cost">Traces</button>
              </div>
            )}
          </section>

          {org && (
            <section className="side-card">
              <div className="row between"><b>{org.name}</b><span className="role-chip">{role}</span></div>
              <p className="small muted">{(projects ?? []).length} project{(projects ?? []).length === 1 ? "" : "s"}{orgs.length > 1 ? ` · ${orgs.length} organizations` : ""}</p>
              <div className="side-actions">
                <button className="small" onClick={() => setDialog("members")}>{isAdmin ? "Members & invites" : "Members"}</button>
                {isAdmin && <button className="small" onClick={() => setDialog("jira")} title="Connect Jira Cloud to push requirements as epics and stories">Jira</button>}
                {isAdmin && <button className="small" onClick={() => setDialog("integrations")} title="Connect GitHub, Confluence, Slack and Microsoft Teams">Integrations</button>}
                {newOrg === null ? (
                  <button className="small ghost" onClick={() => setNewOrg("")}>+ New organization</button>
                ) : (
                  <form className="row gap" onSubmit={async (e) => {
                    e.preventDefault();
                    if (!newOrg.trim()) return;
                    try { const o = await api.createOrg(newOrg.trim()); setOrgId(o.id); setNewOrg(null); onMeChanged(); }
                    catch (err) { setError((err as Error).message); }
                  }}>
                    <input autoFocus className="grow" placeholder="Organization name" value={newOrg} onChange={(e) => setNewOrg(e.target.value)} />
                    <button className="small primary" disabled={!newOrg.trim()}>Create</button>
                    <button type="button" className="small" onClick={() => setNewOrg(null)}>✕</button>
                  </form>
                )}
              </div>
            </section>
          )}
          {!orgs.length && (
            <section className="side-card">
              <b>No organization yet</b>
              <p className="small muted">Create one, or open an invite link from a teammate.</p>
              <button className="small primary" onClick={() => setNewOrg("")}>+ New organization</button>
            </section>
          )}

          <section className="side-card tips">
            <b>Shortcuts</b>
            <ul className="small muted">
              <li><kbd>Ctrl</kbd> <kbd>K</kbd> switch project (inside a project)</li>
              <li><kbd>L</kbd> tidy up the canvas · <kbd>F</kbd> full screen</li>
              <li><kbd>/</kbd> search projects here</li>
            </ul>
          </section>
        </aside>
      </main>

      {dialog === "members" && org && role && (
        <MembersDialog orgId={org.id} orgName={org.name} role={role} selfId={me.user.id}
          onClose={() => setDialog(null)} onChanged={onMeChanged} />
      )}
      {dialog === "usage" && org && role && (
        <AIUsageDialog orgId={org.id} orgName={org.name} role={role} selfId={me.user.id} onClose={() => setDialog(null)} />
      )}
      {dialog === "traces" && org && (
        <TracesDialog orgId={org.id} orgName={org.name} onClose={() => setDialog(null)} />
      )}
      {dialog === "integrations" && org && (
        <IntegrationsDialog orgId={org.id} orgName={org.name} onClose={() => setDialog(null)} />
      )}
      {dialog === "jira" && org && role && (
        <JiraSettingsDialog orgId={org.id} orgName={org.name} role={role} selfId={me.user.id} onClose={() => setDialog(null)} />
      )}
      {dialog === "ai" && org && role && (
        <AISettingsDialog orgId={org.id} orgName={org.name} role={role} selfId={me.user.id} onClose={() => { setDialog(null); loadAi(); }} />
      )}
    </div>
  );
}
