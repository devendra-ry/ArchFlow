import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, Icon } from "../icons";
import { api } from "../api";
import { getRecent, lastBranch, pruneRecent, timeAgo } from "../recent";
import { can, type Me, type Project } from "../types";
import { Select } from "./Select";

interface Props {
  me: Me;
  current: Project | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Open a project (on the branch you last used there). */
  onOpen: (pid: string, branch: string) => void;
  onHome: () => void;
}

interface Item { project: Project; orgName: string; recentAt?: number; branch: string }

const isMac = typeof navigator !== "undefined" && /Mac|iPhone|iPad/.test(navigator.platform);
export const SWITCHER_SHORTCUT = isMac ? "⌘K" : "Ctrl K";

/** The project name in the top bar: a searchable menu of recent and all projects, plus quick create. */
export function ProjectSwitcher({ me, current, open, onOpenChange, onOpen, onHome }: Props) {
  const [projects, setProjects] = useState<Project[] | null>(null);
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState<{ name: string; orgId: string; template: string } | null>(null);
  const [templates, setTemplates] = useState<{ key: string; name: string }[]>([]);
  const wrapRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);
  // The top bar scrolls horizontally (which clips overflow), so the menu is positioned against the window.
  const [pos, setPos] = useState<{ top: number; left: number }>({ top: 52, left: 8 });

  const orgName = useMemo(() => new Map(me.orgs.map((o) => [o.id, o.name])), [me.orgs]);
  const editableOrgs = me.orgs.filter((o) => can(o.role, "edit"));

  // Load (and refresh) the list each time the menu opens.
  useEffect(() => {
    if (!open) return;
    setQuery("");
    setActive(0);
    setError("");
    setCreating(null);
    const r = triggerRef.current?.getBoundingClientRect();
    if (r) setPos({ top: r.bottom + 6, left: Math.max(8, Math.min(r.left, window.innerWidth - 432)) });
    api.projects().then((list) => { setProjects(list); pruneRecent(new Set(list.map((p) => p.id))); })
      .catch((e) => setError((e as Error).message));
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }, [open]);

  // Close on a click outside.
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      const t = e.target as Element;
      if (!wrapRef.current?.contains(t) && !t.closest?.("[data-af-popover]")) onOpenChange(false); // a Select list is portaled out
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open, onOpenChange]);

  const sections = useMemo(() => {
    if (!projects) return [] as { title: string; items: Item[] }[];
    const byId = new Map(projects.map((p) => [p.id, p]));
    const item = (p: Project, recentAt?: number): Item =>
      ({ project: p, orgName: orgName.get(p.org_id ?? "") ?? "", recentAt, branch: lastBranch(p.id) });
    const q = query.trim().toLowerCase();
    if (q) {
      const hits = projects
        .map((p) => {
          const name = p.name.toLowerCase();
          const org = (orgName.get(p.org_id ?? "") ?? "").toLowerCase();
          const score = name.startsWith(q) ? 0 : name.includes(q) ? 1 : org.includes(q) || (p.description ?? "").toLowerCase().includes(q) ? 2 : -1;
          return { p, score };
        })
        .filter((x) => x.score >= 0)
        .sort((a, b) => a.score - b.score || a.p.name.localeCompare(b.p.name));
      return [{ title: `${hits.length} match${hits.length === 1 ? "" : "es"}`, items: hits.map((x) => item(x.p)) }];
    }
    const recent = getRecent().filter((r) => byId.has(r.pid) && r.pid !== current?.id).slice(0, 5)
      .map((r) => item(byId.get(r.pid)!, r.at));
    const out: { title: string; items: Item[] }[] = [];
    if (recent.length) out.push({ title: "Recent", items: recent });
    const multiOrg = me.orgs.length > 1;
    for (const o of me.orgs) {
      const items = projects.filter((p) => (p.org_id ?? "org-default") === o.id).sort((a, b) => a.name.localeCompare(b.name)).map((p) => item(p));
      if (items.length) out.push({ title: multiOrg ? o.name : "All projects", items });
    }
    return out;
  }, [projects, query, current?.id, orgName, me.orgs]);

  const flat = sections.flatMap((s) => s.items);
  useEffect(() => { setActive(0); }, [query]);
  useEffect(() => {
    listRef.current?.querySelector<HTMLElement>(`[data-index="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const close = () => { onOpenChange(false); triggerRef.current?.focus(); };
  const choose = (it: Item) => {
    onOpenChange(false);
    if (it.project.id !== current?.id) onOpen(it.project.id, it.branch);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") { e.preventDefault(); if (creating) setCreating(null); else close(); }
    else if (creating) return;
    else if (e.key === "ArrowDown") { e.preventDefault(); setActive((i) => Math.min(flat.length - 1, i + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((i) => Math.max(0, i - 1)); }
    else if (e.key === "Enter" && flat[active]) { e.preventDefault(); choose(flat[active]); }
  };

  const startCreate = () => {
    const orgId = editableOrgs.some((o) => o.id === current?.org_id) ? current!.org_id! : editableOrgs[0]?.id ?? "";
    setCreating({ name: query.trim(), orgId, template: "blank" });
    if (!templates.length) api.templates().then(setTemplates).catch(() => {});
  };
  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!creating?.name.trim() || !creating.orgId) return;
    try {
      const p = await api.createProject(creating.name.trim(), creating.template, creating.orgId);
      onOpenChange(false);
      onOpen(p.id, "main");
    } catch (err) { setError((err as Error).message); }
  };

  let index = -1;
  return (
    <div className="switcher" ref={wrapRef} onKeyDown={onKeyDown}>
      <button ref={triggerRef} className="switcher-trigger" onClick={() => onOpenChange(!open)} aria-haspopup="dialog"
        aria-expanded={open} title={`Switch project (${SWITCHER_SHORTCUT})`}>
        <span className="switcher-name">{current?.name ?? "…"}</span>
        {current?.org_id && me.orgs.length > 1 && <span className="switcher-org">{orgName.get(current.org_id)}</span>}
        <span className="switcher-chevron" aria-hidden><Icon as={ChevronDown} size={14} /></span>
      </button>
      {open && (
        <div className="switcher-pop" role="dialog" aria-label="Switch project" style={{ top: pos.top, left: pos.left }}>
          {creating ? (
            <form className="switcher-create" onSubmit={create}>
              <b className="small">New project</b>
              <input autoFocus placeholder="Project name" value={creating.name} maxLength={120}
                onChange={(e) => setCreating({ ...creating, name: e.target.value })} aria-label="Project name" />
              <div className="row gap">
                {editableOrgs.length > 1 && (
                  <Select value={creating.orgId} onChange={(v) => setCreating({ ...creating, orgId: v })} aria-label="Organization"
                    options={editableOrgs.map((o) => ({ value: o.id, label: o.name }))} />
                )}
                <Select value={creating.template} onChange={(v) => setCreating({ ...creating, template: v })} aria-label="Template"
                  options={(templates.length ? templates : [{ key: "blank", name: "Blank" }]).map((t) => ({ value: t.key, label: t.name }))} />
              </div>
              <div className="row gap end">
                <button type="button" className="small" onClick={() => setCreating(null)}>Back</button>
                <button className="small primary" disabled={!creating.name.trim()}>Create and open</button>
              </div>
            </form>
          ) : (
            <>
              <input ref={inputRef} className="switcher-search" value={query} onChange={(e) => setQuery(e.target.value)}
                placeholder="Search projects…" aria-label="Search projects" role="combobox" aria-expanded="true"
                aria-controls="switcher-list" aria-activedescendant={flat[active] ? `sw-${flat[active].project.id}` : undefined} />
              <ul className="switcher-list" id="switcher-list" role="listbox" ref={listRef}>
                {projects === null && !error && <li className="muted small switcher-note">Loading…</li>}
                {projects && flat.length === 0 && (
                  <li className="muted small switcher-note">{query ? `No project matches “${query}”.` : "No projects yet."}</li>
                )}
                {sections.map((s) => (
                  <li key={s.title} role="presentation">
                    <div className="switcher-section">{s.title}</div>
                    <ul role="presentation">
                      {s.items.map((it) => {
                        index += 1;
                        const i = index;
                        const isCurrent = it.project.id === current?.id;
                        return (
                          <li key={`${s.title}-${it.project.id}`} id={`sw-${it.project.id}`} role="option" aria-selected={i === active}
                            data-index={i} className={`switcher-item ${i === active ? "active" : ""}`}
                            onMouseMove={() => setActive(i)} onClick={() => choose(it)}>
                            <span className="switcher-avatar" aria-hidden>{it.project.name.slice(0, 1).toUpperCase()}</span>
                            <span className="switcher-text">
                              <span className="switcher-title">{it.project.name}</span>
                              <span className="muted tiny">
                                {[me.orgs.length > 1 && s.title !== it.orgName ? it.orgName : "",
                                  it.recentAt ? `opened ${timeAgo(it.recentAt)}` : "",
                                  it.branch !== "main" ? `⎇ ${it.branch}` : ""].filter(Boolean).join(" · ") || it.project.description || " "}
                              </span>
                            </span>
                            {isCurrent && <span className="switcher-current">current</span>}
                          </li>
                        );
                      })}
                    </ul>
                  </li>
                ))}
              </ul>
              {error && <p className="error small switcher-note">{error}</p>}
              <div className="switcher-foot">
                {editableOrgs.length > 0 && <button className="small" onClick={startCreate}>+ New project</button>}
                <button className="small ghost" onClick={() => { onOpenChange(false); onHome(); }}>All projects & organizations</button>
                <span className="spacer" />
                <span className="muted tiny"><kbd>↑</kbd><kbd>↓</kbd> <kbd>↵</kbd> · <kbd>{SWITCHER_SHORTCUT}</kbd></span>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
