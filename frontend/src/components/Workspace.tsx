import {
  Background, BackgroundVariant, ControlButton, Controls, MarkerType, MiniMap, ReactFlow, ReactFlowProvider, ViewportPortal,
  applyEdgeChanges, applyNodeChanges, useReactFlow,
  type Connection, type Edge, type EdgeChange, type NodeChange, type OnDelete,
} from "@xyflow/react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useCollab } from "../collab";
import { useAssistant } from "../assistant";
import { exportCanvasImage, exportName } from "../exportImage";
import { autoLayout } from "../layout";
import { touchRecent } from "../recent";
import { buildSuggestions } from "../suggestions";
import { asSuggestions, useAiSuggestions } from "../aiSuggestions";
import { useRealtime } from "../realtime";
import type {
  Activity, Analysis, ArchNode, Branch, Catalog, CatalogItem, Diff, Issue, Me, MemoryItem, Op, Project, Role, User, Version,
} from "../types";
import { activeRequirements, can } from "../types";
import { UserMenu } from "./UserMenu";
import { ArchNodeComponent, TYPE_META, type ArchFlowNode, type DiffMark } from "./ArchNode";
import { CodeDialog } from "./CodeDialog";
import { AISettingsDialog } from "./OrgDialogs";
import { CommandBar, type ChatDock, type ChatMode } from "./CommandBar";
import { CommentsPanel } from "./CommentsPanel";
import { NotificationBell } from "./NotificationBell";
import type { AppNotification, Comment, ExternalLinks, JiraLinks, Member } from "../types";
import { JiraDialog } from "./JiraDialog";
import { KnowledgeDialog } from "./KnowledgeDialog";
import { CostDialog } from "./CostDialog";
import { ConfluenceDialog, GitHubDialog } from "./IntegrationsDialog";
import { ConnectDialog } from "./ConnectDialog";
import { ProjectAccessDialog } from "./SecurityDialogs";
import { ExportDialog } from "./ExportDialog";
import { DiagramsDialog } from "./DiagramsDialog";
import { DecisionsPanel } from "./DecisionsPanel";
import { DesignChat } from "./DesignChat";
import { PromptDialog } from "./Dialogs";
import { ProjectSwitcher } from "./ProjectSwitcher";
import { Inspector } from "./Inspector";
import { IssuesPanel } from "./IssuesPanel";
import type { EntityLinks } from "./Markdown";
import { MemoryDialog } from "./MemoryDialog";
import { MergeDialog } from "./MergeDialog";
import { DRAG_MIME, Palette } from "./Palette";
import { RequirementsPanel } from "./RequirementsPanel";
import { VersionsPanel } from "./VersionsPanel";

interface Props {
  pid: string;
  branch: string;
  user: User;
  colorMode: "light" | "dark";
  themeToggle: React.ReactNode;
  onBranch: (branch: string) => void;
  onExit: () => void;
  /** Switch to another project (the switcher passes the branch you last used there). */
  onOpenProject: (pid: string, branch: string) => void;
  me: Me;
  onLogout: () => void;
}

const nodeTypes = { arch: ArchNodeComponent };
type Tab = "chat" | "requirements" | "issues" | "comments" | "versions" | "decisions";
const TAB_META: Record<Tab, { icon: string; label: string; title: string }> = {
  chat: { icon: "✦", label: "Assistant", title: "AI assistant: design, edit and ask" },
  requirements: { icon: "☰", label: "Requirements", title: "What the system must do, and how well" },
  issues: { icon: "♥", label: "Health", title: "Checks and the AI review of this design" },
  comments: { icon: "💬", label: "Comments", title: "Discuss components and the design with your team" },
  versions: { icon: "⟲", label: "History", title: "Saved versions and activity" },
  decisions: { icon: "⚖", label: "Decisions", title: "Compare drafts and record architecture decisions" },
};
const DOCK_KEY = "archflow.chatDock";
/** A comment thread to open once a workspace loads (set when a notification leads to another project). */
const OPEN_THREAD_KEY = "archflow.openThread";
const rand = () => Math.random().toString(36).slice(2, 6);

export function Workspace(props: Props) {
  return (
    <ReactFlowProvider>
      <WorkspaceInner {...props} />
    </ReactFlowProvider>
  );
}

function WorkspaceInner({ pid, branch, user, me, colorMode, themeToggle, onBranch, onExit, onOpenProject, onLogout }: Props) {
  const flow = useReactFlow<ArchFlowNode, Edge>();
  const [project, setProject] = useState<Project | null>(null);
  const [branches, setBranches] = useState<Branch[]>([]);
  const [headVersion, setHeadVersion] = useState<number | null>(null);
  const [versions, setVersions] = useState<Version[]>([]);
  const [activity, setActivity] = useState<Activity[]>([]);
  const [analysis, setAnalysis] = useState<Analysis | null>(null);
  const [dock, setDockState] = useState<ChatDock>(() => {
    try { const v = localStorage.getItem(DOCK_KEY); if (v === "left" || v === "right" || v === "float") return v; } catch { /* storage unavailable */ }
    return "left";
  });
  const [tab, setTab] = useState<Tab>(dock === "left" ? "chat" : "requirements");
  const [rightTab, setRightTab] = useState<"chat" | "props">("chat");
  const [chatMode, setChatMode] = useState<ChatMode | null>(null); // null until the project loads: Design on an empty canvas
  const setDock = (d: ChatDock) => {
    setDockState(d);
    try { localStorage.setItem(DOCK_KEY, d); } catch { /* storage unavailable */ }
    if (d === "left") setTab("chat"); else if (tab === "chat") setTab("requirements");
    if (d === "right") setRightTab("chat");
  };
  const [traced, setTraced] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [diff, setDiff] = useState<{ from: number; data: Diff } | null>(null);
  const [mergeSource, setMergeSource] = useState<string | null>(null);
  const [promptFor, setPromptFor] = useState<{ nodeIds: string[]; requirementIds: string[] } | null>(null);
  const [memory, setMemory] = useState<MemoryItem[]>([]);
  const [comments, setComments] = useState<Comment[]>([]);
  const [members, setMembers] = useState<Member[]>([]);
  const [focusThread, setFocusThread] = useState<string | null>(null);
  const [notifKey, setNotifKey] = useState(0);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [accessOpen, setAccessOpen] = useState(false);
  // Latest server event about a conversation, for the design chat (the assistant hook gets it directly).
  const [designRemote, setDesignRemote] = useState<{ id: string; at: number } | null>(null);
  const [toasts, setToasts] = useState<{ id: number; text: string }[]>([]);
  const [loadError, setLoadError] = useState("");
  const [connectOpen, setConnectOpen] = useState(false);
  const [codeOpen, setCodeOpen] = useState(false);
  const [diagramsOpen, setDiagramsOpen] = useState(false);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [converting, setConverting] = useState(false);
  const [ai, setAi] = useState<{ available: boolean; model: string } | null>(null);
  const [newBranch, setNewBranch] = useState<string | null>(null);
  const [nodes, setNodes] = useState<ArchFlowNode[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  // Diagram level shown on the canvas: "" = system (HLD), else the id of the component whose internals are open.
  const [level, setLevel] = useState("");
  const nodesRef = useRef(nodes);
  nodesRef.current = nodes;
  const levelRef = useRef(level);
  levelRef.current = level;
  const [generating, setGenerating] = useState(false);
  // Canvas full screen: only the canvas is shown, and the browser goes full screen when it allows it.
  const [canvasFull, setCanvasFull] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [exportOpen, setExportOpen] = useState(false);
  const [aiSettingsOpen, setAiSettingsOpen] = useState(false);
  const [jiraOpen, setJiraOpen] = useState(false);
  const [knowledgeOpen, setKnowledgeOpen] = useState(false);
  const [kbVersion, setKbVersion] = useState(0);
  const [costsOpen, setCostsOpen] = useState(false);
  const [githubOpen, setGithubOpen] = useState(false);
  const [confluenceOpen, setConfluenceOpen] = useState(false);
  const [github, setGithub] = useState<ExternalLinks | null>(null);
  const [confluence, setConfluence] = useState<ExternalLinks | null>(null);
  const [jira, setJira] = useState<JiraLinks | null>(null);
  const toolsRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!toolsOpen) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !toolsRef.current?.contains(e.target as Node)) setToolsOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", close); };
  }, [toolsOpen]);
  // ---- auto-arrange ----
  const [autoTidy, setAutoTidy] = useState(() => { try { return localStorage.getItem("archflow.autoTidy") !== "0"; } catch { return true; } });
  const autoTidyRef = useRef(autoTidy);
  autoTidyRef.current = autoTidy;
  const tidyRef = useRef<(announce: boolean) => void>(() => {});
  const toggleAutoTidy = () => {
    const next = !autoTidy;
    setAutoTidy(next);
    try { localStorage.setItem("archflow.autoTidy", next ? "1" : "0"); } catch { /* storage unavailable */ }
    toast(next ? "Auto-arrange after AI edits: on" : "Auto-arrange after AI edits: off");
  };
  const [switcherOpen, setSwitcherOpen] = useState(false);
  // Ctrl/⌘+K opens the project switcher from anywhere in the workspace.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setSwitcherOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  const canvasFullRef = useRef(false);
  canvasFullRef.current = canvasFull;
  const toggleCanvasFull = useCallback(() => {
    const next = !canvasFullRef.current;
    if (next && !document.fullscreenElement) document.documentElement.requestFullscreen?.().catch(() => { /* not allowed: CSS only */ });
    if (!next && document.fullscreenElement) document.exitFullscreen?.().catch(() => {});
    setCanvasFull(next);
  }, []);
  useEffect(() => {
    // Leaving browser full screen (Esc, F11) also leaves canvas full screen.
    const onChange = () => { if (!document.fullscreenElement) setCanvasFull(false); };
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);
  useEffect(() => {
    // F toggles canvas full screen, Esc leaves it (when you are not typing or in a dialog).
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (document.querySelector(".modal-backdrop, .command-bar.full")) return;
      if ((e.key === "f" || e.key === "F") && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); toggleCanvasFull(); }
      else if ((e.key === "l" || e.key === "L") && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); tidyRef.current(true); }
      else if (e.key === "Escape" && canvasFull && !document.fullscreenElement) setCanvasFull(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [canvasFull, toggleCanvasFull]);
  useEffect(() => {
    const t = window.setTimeout(() => flow.fitView({ padding: 0.15, duration: 300 }), 120); // after the layout changes
    return () => window.clearTimeout(t);
  }, [canvasFull, flow]);
  // Your role in the project's org (from the API). Viewers and reviewers get a read-only workspace; the API
  // and the collab server enforce the same rules, this only shapes the UI.
  const [role, setRole] = useState<Role | null>(null);
  const readOnly = role !== null && !can(role, "edit");
  const clipboard = useRef<{ nodes: ArchNode[]; edges: { source: string; target: string; protocol: string; label: string }[] } | null>(null);
  const wrapper = useRef<HTMLDivElement>(null);

  const toast = useCallback((text: string) => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t.slice(-3), { id, text }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3500);
  }, []);

  // The canvas model is a live Yjs document shared with everyone on this branch (and with AI/MCP edits).
  const live = useCollab(pid, branch, user);
  const state = live.state;
  const stateRef = useRef(state);
  stateRef.current = state;

  // ---- HLD → LLD levels ----
  const levelOf = useCallback((n: ArchNode) => (n.parent_id && state?.nodes[n.parent_id] ? n.parent_id : ""), [state]);
  useEffect(() => { setLevel(""); }, [pid, branch]);
  useEffect(() => { if (state && level && !state.nodes[level]) setLevel(""); }, [state, level]); // opened component was deleted
  // Re-fit the view whenever you move between diagram levels (after the new level's nodes render).
  const firstLevel = useRef(true);
  useEffect(() => {
    if (firstLevel.current) { firstLevel.current = false; return; }
    const t = window.setTimeout(() => flow.fitView({ padding: 0.2, duration: 300 }), 80);
    return () => window.clearTimeout(t);
  }, [level, flow]);
  const childCount = useMemo(() => {
    const m: Record<string, number> = {};
    for (const n of Object.values(state?.nodes ?? {})) if (n.parent_id) m[n.parent_id] = (m[n.parent_id] ?? 0) + 1;
    return m;
  }, [state]);
  const crumbs = useMemo(() => {
    const out: ArchNode[] = [];
    let cur = level ? state?.nodes[level] : undefined;
    while (cur && out.length < 20) { out.unshift(cur); cur = cur.parent_id ? state?.nodes[cur.parent_id] : undefined; }
    return out;
  }, [state, level]);
  const openLevel = useCallback((id: string) => {
    setLevel(id);
    window.setTimeout(() => flow.fitView({ padding: 0.2, duration: 300, maxZoom: 1.2 }), 60);
  }, [flow]);

  const refreshMeta = useCallback(() => {
    api.versions(pid).then(setVersions).catch(() => {});
    api.activity(pid, branch).then(setActivity).catch(() => {});
    api.project(pid, branch).then((p) => { setBranches(p.branches); setHeadVersion(p.head_version); }).catch(() => {});
  }, [pid, branch]);

  // ---- initial load ----
  useEffect(() => {
    api.project(pid, branch).then((p) => {
      setProject(p.project);
      setBranches(p.branches);
      setHeadVersion(p.head_version);
      setRole(p.role);
      // AI is on when the server or the org (its own keys) has a provider configured.
      api.health().then((h) => setAi({ available: p.ai_available, model: h.llm?.groups?.reasoning?.[0] ?? h.model }))
        .catch(() => setAi({ available: p.ai_available, model: "" }));
    }).catch((e) => setLoadError(e.message));
    refreshMeta();
    api.catalog().then(setCatalog).catch(() => {});
  }, [pid, branch, refreshMeta]);

  useEffect(() => { if (project) touchRecent(project.id, project.name, project.org_id ?? "", branch); }, [project, branch]);

  // ---- AI conversations and project memory ----
  const assistant = useAssistant(pid, branch, user.name, (msg) => {
    // Auto-arrange after the AI added components (it guesses positions, which can overlap).
    if (autoTidyRef.current && (msg.data.summary ?? []).some((s) => /^added /.test(s))) {
      window.setTimeout(() => tidyRef.current(false), 1500); // after the live document and sizes update
    }
  });
  const loadMemory = useCallback(() => { api.memory(pid).then(setMemory).catch(() => {}); }, [pid]);
  const loadComments = useCallback(() => { api.comments(pid, branch).then(setComments).catch(() => {}); }, [pid, branch]);
  useEffect(() => { setComments([]); loadComments(); }, [loadComments]);
  // Jira issues pushed for this project: keys and pulled status next to requirements and components.
  const loadJira = useCallback(() => { api.jiraLinks(pid).then(setJira).catch(() => setJira(null)); }, [pid]);
  useEffect(loadJira, [loadJira]);
  const jiraByReq = Object.fromEntries((jira?.links ?? []).filter((l) => l.object_kind === "requirement").map((l) => [l.object_id, l]));
  // GitHub issues and the Confluence page published for this project (shown when the org connected them).
  const loadGithub = useCallback(() => { api.githubLinks(pid).then(setGithub).catch(() => setGithub(null)); }, [pid]);
  useEffect(loadGithub, [loadGithub]);
  useEffect(() => { api.confluenceLinks(pid).then(setConfluence).catch(() => setConfluence(null)); }, [pid]);
  const githubByReq = Object.fromEntries((github?.links ?? []).map((l) => [l.object_id, l]));
  // People you can @mention: refreshed when comments open and when someone new comes online (they may have just joined).
  const peerNames = live.peers.map((p) => p.name).sort().join("|");
  useEffect(() => {
    if (project?.org_id) api.members(project.org_id).then(setMembers).catch(() => {});
  }, [project?.org_id, tab === "comments", peerNames]); // eslint-disable-line react-hooks/exhaustive-deps
  const openThread = useCallback((threadId: string) => { setTab("comments"); setFocusThread(threadId); }, []);
  useEffect(() => {
    try {
      const pending = JSON.parse(sessionStorage.getItem(OPEN_THREAD_KEY) ?? "null");
      if (pending?.pid === pid) { sessionStorage.removeItem(OPEN_THREAD_KEY); openThread(pending.thread); }
    } catch { /* storage unavailable */ }
  }, [pid, branch, openThread]);
  const openNotification = (n: AppNotification) => {
    if (n.version != null) {  // a version review: open the versions panel (it lists every branch's versions)
      if (n.project_id === pid) { refreshMeta(); setTab("versions"); } else onOpenProject(n.project_id, n.branch);
      return;
    }
    if (n.project_id === pid && n.branch === branch) { loadComments(); openThread(n.thread_id); return; }
    try { sessionStorage.setItem(OPEN_THREAD_KEY, JSON.stringify({ pid: n.project_id, thread: n.thread_id })); } catch { /* ignore */ }
    if (n.project_id === pid) onBranch(n.branch); else onOpenProject(n.project_id, n.branch);
  };
  useEffect(() => { loadMemory(); }, [loadMemory]);

  // ---- realtime ----
  const activityTimer = useRef<number | undefined>(undefined);
  const rt = useRealtime(pid, branch, user, {
    onActivity: () => {
      window.clearTimeout(activityTimer.current);
      activityTimer.current = window.setTimeout(() => {
        api.activity(pid, branch).then(setActivity).catch(() => {});
        // server-side changes (e.g. Mark reviewed) may not touch the live document, so refresh the checks too
        api.analysis(pid, branch).then(setAnalysis).catch(() => {});
      }, 400);
    },
    onVersions: refreshMeta,
    onConversation: (id) => { assistant.onRemote(id); setDesignRemote({ id, at: Date.now() }); },
    onComments: () => { loadComments(); setNotifKey((k) => k + 1); },
    onMemory: (_actor, added) => {
      loadMemory();
      if (added.length) toast(`✦ Remembered: ${added.join(" · ")}`);
    },
  });

  // ---- analysis (debounced on every revision) ----
  useEffect(() => {
    if (!state) return;
    // The API's copy of the document trails the live one by the collab store debounce (a second or two).
    const t = window.setTimeout(() => api.analysis(pid, branch).then(setAnalysis).catch(() => {}), 2200);
    return () => window.clearTimeout(t);
  }, [state?.rev, pid, branch]);

  // ---- keep an open diff live ----
  useEffect(() => {
    if (!diff) return;
    api.diff(pid, branch, String(diff.from)).then((data) => setDiff((d) => (d ? { ...d, data } : d))).catch(() => {});
  }, [state?.rev]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- sending operations ----
  const { apply, setCursor, setSelection, undo, redo } = live;
  const send = useCallback((ops: Op[]) => {
    if (!ops.length) return;
    if (readOnly) { toast(`You have ${role} access: ask an admin for the developer role to edit.`); return; }
    try {
      apply(ops);
    } catch (e) {
      toast(`⚠ ${(e as Error).message}`);
    }
  }, [apply, toast, readOnly, role]);

  /** Arrange the diagram level on screen (system: tiers; internals: call flow). One undo step. */
  const tidy = useCallback((announce: boolean) => {
    const st = stateRef.current;
    if (!st || readOnly) return;
    const rendered = nodesRef.current.filter((n) => !n.id.startsWith("ghost:") && n.data.model.type !== "note");
    const ids = new Set(rendered.map((n) => n.id));
    const positions = autoLayout(
      rendered.map((n) => ({ id: n.id, type: n.data.model.type, x: n.position.x, y: n.position.y,
        width: n.measured?.width ?? 190, height: n.measured?.height ?? 80 })),
      Object.values(st.edges).filter((e) => ids.has(e.source) && ids.has(e.target)).map((e) => ({ source: e.source, target: e.target })),
      levelRef.current ? "flow" : "tiers");
    const ops: Op[] = [...positions].filter(([id, p]) => {
      const m = st.nodes[id];
      return m && (Math.abs(m.position.x - p.x) > 1 || Math.abs(m.position.y - p.y) > 1);
    }).map(([id, p]) => ({ op: "update_node", id, changes: { position: p } }) as Op);
    if (!ops.length) { if (announce) toast("Already tidy"); return; }
    send(ops);
    window.setTimeout(() => flow.fitView({ padding: 0.15, duration: 400 }), 150);
    toast(announce ? `Arranged ${ops.length} component(s) · Ctrl+Z to undo` : `Auto-arranged after the AI edit · Ctrl+Z to undo`);
  }, [readOnly, send, flow, toast]);
  tidyRef.current = tidy;

  // ---- derived view state ----
  const issuesByNode = useMemo(() => {
    const m: Record<string, number> = {};
    for (const i of analysis?.architecture_issues ?? []) for (const id of i.node_ids) m[id] = (m[id] ?? 0) + 1;
    return m;
  }, [analysis]);

  // Stale requirement links per component (a requirement changed after the component did).
  const staleByNode = useMemo(() => {
    const m: Record<string, string[]> = {};
    for (const l of analysis?.stale_links ?? []) (m[l.node_id] ??= []).push(l.requirement_id);
    return m;
  }, [analysis]);
  const markReviewed = useCallback((nodeId: string, requirementId?: string) => {
    api.ops(pid, branch, [{ op: "mark_reviewed", id: nodeId, ...(requirementId ? { requirement_id: requirementId } : {}) }], user.name)
      .then(() => api.analysis(pid, branch).then(setAnalysis))
      .catch((e) => toast(`⚠ ${(e as Error).message}`));
  }, [pid, branch, user.name, toast]);

  // Open comment threads per component (the canvas shows a badge; clicking it opens the newest thread).
  const commentsByNode = useMemo(() => {
    const m: Record<string, string[]> = {};
    for (const c of comments) if (!c.parent_id && !c.resolved && c.target_type === "node") (m[c.target_id] ??= []).push(c.id);
    return m;
  }, [comments]);
  const openThreadsCount = comments.filter((c) => !c.parent_id && !c.resolved).length;

  const highlight = useMemo<Set<string> | null>(() => {
    if (!state) return null;
    if (traced) return new Set(Object.values(state.nodes).filter((n) => n.requirement_ids.includes(traced)).map((n) => n.id));
    const q = search.trim().toLowerCase();
    if (q) {
      return new Set(Object.values(state.nodes).filter((n) =>
        `${n.name} ${n.technology} ${n.type} ${n.description} ${n.requirement_ids.join(" ")}`.toLowerCase().includes(q)).map((n) => n.id));
    }
    return null;
  }, [state, traced, search]);

  const remoteByNode = useMemo(() => {
    const m: Record<string, { name: string; color: string }[]> = {};
    for (const p of live.peers) {
      if (p.clientId === live.selfId) continue;
      for (const id of p.selection ?? []) (m[id] ??= []).push({ name: p.name, color: p.color });
    }
    return m;
  }, [live.peers, live.selfId]);

  // ---- server model → React Flow nodes/edges ----
  useEffect(() => {
    if (!state) return;
    const d = diff?.data;
    const mark = (id: string, sec?: Diff["nodes"]): DiffMark =>
      !sec ? null : sec.added.some((x) => x.id === id) ? "added" : sec.changed.some((x) => x.id === id) ? "changed" : null;

    const visible = new Set(Object.values(state.nodes).filter((m) => levelOf(m) === level).map((m) => m.id));
    setNodes((prev) => {
      const prevMap = new Map(prev.map((n) => [n.id, n]));
      const out: ArchFlowNode[] = Object.values(state.nodes).filter((m) => visible.has(m.id)).map((m) => {
        const p = prevMap.get(m.id);
        return {
          ...p, // keep React Flow internals (measured size, selection, dragging)
          id: m.id, type: "arch",
          position: p?.dragging ? p.position : m.position,
          selected: p?.selected ?? false,
          data: {
            model: m, dimmed: !!highlight && !highlight.has(m.id), highlighted: !!highlight?.has(m.id),
            diff: mark(m.id, d?.nodes), remote: remoteByNode[m.id], issues: issuesByNode[m.id],
            childCount: childCount[m.id], stale: staleByNode[m.id],
            requirementIds: m.requirement_ids.filter((r) => !state.requirements[r]?.deleted),
            comments: commentsByNode[m.id]?.length, onComments: commentsByNode[m.id] ? () => openThread(commentsByNode[m.id].at(-1)!) : undefined,
          },
        };
      });
      if (d) {
        for (const r of d.nodes.removed) {
          const m = d.from_state.nodes[r.id];
          if (m && (m.parent_id ?? "") === level) out.push({ id: `ghost:${m.id}`, type: "arch", position: m.position, draggable: false, selectable: false, connectable: false,
            data: { model: m, diff: "removed" } });
        }
      }
      return out;
    });

    setEdges((prev) => {
      const prevSel = new Map(prev.map((e) => [e.id, e.selected]));
      const out: Edge[] = Object.values(state.edges).filter((e) => visible.has(e.source) && visible.has(e.target)).map((e) => {
        const dm = mark(e.id, d?.edges);
        const dim = !!highlight && !(highlight.has(e.source) && highlight.has(e.target));
        return {
          id: e.id, source: e.source, target: e.target, label: e.protocol || e.label || undefined,
          type: "smoothstep", pathOptions: { borderRadius: 14, offset: 18 },
          labelBgPadding: [5, 2] as [number, number], labelBgBorderRadius: 4,
          selected: prevSel.get(e.id) ?? false, markerEnd: { type: MarkerType.ArrowClosed },
          className: ["arch-edge", dm && `diff-${dm}`, dim && "dimmed"].filter(Boolean).join(" "),
        };
      });
      if (d) {
        for (const r of d.edges.removed) {
          const e = d.from_state.edges[r.id];
          if (!e || (d.from_state.nodes[e.source]?.parent_id ?? "") !== level) continue;
          const src = state.nodes[e.source] ? e.source : `ghost:${e.source}`;
          const tgt = state.nodes[e.target] ? e.target : `ghost:${e.target}`;
          out.push({ id: `ghost:${e.id}`, source: src, target: tgt, label: e.protocol || undefined, selectable: false,
            className: "diff-removed", markerEnd: { type: MarkerType.ArrowClosed } });
        }
      }
      return out;
    });
  }, [state, diff, highlight, remoteByNode, issuesByNode, level, levelOf, childCount, commentsByNode, openThread, staleByNode]);


  const selectedNodeIds = useMemo(() => nodes.filter((n) => n.selected && !n.id.startsWith("ghost:")).map((n) => n.id), [nodes]);
  const selectedEdgeIds = useMemo(() => edges.filter((e) => e.selected && !e.id.startsWith("ghost:")).map((e) => e.id), [edges]);
  const selKey = selectedNodeIds.join(",");
  useEffect(() => { setSelection(selectedNodeIds); }, [selKey, setSelection]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- canvas handlers ----
  const onNodesChange = useCallback((changes: NodeChange<ArchFlowNode>[]) =>
    setNodes((ns) => applyNodeChanges(changes.filter((c) => c.type !== "remove"), ns)), []);
  const onEdgesChange = useCallback((changes: EdgeChange<Edge>[]) =>
    setEdges((es) => applyEdgeChanges(changes.filter((c) => c.type !== "remove"), es)), []);

  const onDelete: OnDelete<ArchFlowNode, Edge> = useCallback(({ nodes: dn, edges: de }) => {
    const ids = new Set(dn.map((n) => n.id));
    send([
      ...de.filter((e) => !ids.has(e.source) && !ids.has(e.target) && !e.id.startsWith("ghost:")).map((e) => ({ op: "remove_edge", id: e.id })),
      ...dn.filter((n) => !n.id.startsWith("ghost:")).map((n) => ({ op: "remove_node", id: n.id })),
    ]);
  }, [send]);

  const onNodeDragStop = useCallback((_: unknown, __: unknown, dragged: ArchFlowNode[]) => {
    send(dragged.map((n) => ({ op: "update_node", id: n.id, changes: { position: { x: Math.round(n.position.x), y: Math.round(n.position.y) } } })));
  }, [send]);

  const onConnect = useCallback((c: Connection) => {
    if (c.source && c.target && c.source !== c.target) send([{ op: "add_edge", edge: { source: c.source, target: c.target } }]);
  }, [send]);

  const onDrop = useCallback((e: React.DragEvent) => {
    if (readOnly) return;
    const raw = e.dataTransfer.getData(DRAG_MIME);
    if (!raw) return;
    e.preventDefault();
    const item = JSON.parse(raw) as CatalogItem;
    const type = item.type;
    const p = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    const generic = item.vendor === "generic";
    send([{ op: "add_node", node: {
      name: type === "note" ? "Note" : generic ? `New ${TYPE_META[type].label}` : item.name, type,
      technology: generic ? "" : item.name, vendor: generic ? "" : item.vendor, catalog_id: generic ? "" : item.id,
      description: type === "note" ? "Select it and edit the text in the inspector" : "",
      position: { x: Math.round(p.x - 90), y: Math.round(p.y - 30) },
      parent_id: level, // dropped into the diagram that is open
      ...(type === "endpoint" ? { name: "GET /resource", props: { method: "GET", path: "/resource" } } : {}),
      ...(type === "table" ? { name: "new_table", props: { columns: "id uuid pk, created_at timestamptz" } } : {}),
    } }]);
  }, [flow, send, level, readOnly]);

  const lastCursor = useRef(0);
  const onMouseMove = useCallback((e: React.MouseEvent) => {
    const now = performance.now();
    if (now - lastCursor.current < 50) return;
    lastCursor.current = now;
    const p = flow.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    setCursor({ x: p.x, y: p.y, level });
  }, [flow, setCursor, level]);

  // ---- copy / paste / duplicate ----
  const copySelection = useCallback(() => {
    const s = stateRef.current;
    if (!s || !selectedNodeIds.length) return false;
    const ids = new Set(selectedNodeIds);
    clipboard.current = {
      nodes: selectedNodeIds.map((id) => s.nodes[id]).filter(Boolean),
      edges: Object.values(s.edges).filter((e) => ids.has(e.source) && ids.has(e.target))
        .map(({ source, target, protocol, label }) => ({ source, target, protocol, label })),
    };
    return true;
  }, [selectedNodeIds]);

  const paste = useCallback(() => {
    const clip = clipboard.current;
    if (!clip) return;
    const idMap: Record<string, string> = {};
    const ops: Op[] = clip.nodes.map((n) => {
      idMap[n.id] = `${n.id}-${rand()}`;
      return { op: "add_node", node: {
        id: idMap[n.id], name: n.name, type: n.type, technology: n.technology, description: n.description,
        requirement_ids: n.requirement_ids, props: n.props, parent_id: level,
        position: { x: n.position.x + 60, y: n.position.y + 60 } } };
    });
    for (const e of clip.edges) ops.push({ op: "add_edge", edge: { ...e, source: idMap[e.source], target: idMap[e.target] } });
    send(ops);
    // next paste lands further offset
    clipboard.current = { ...clip, nodes: clip.nodes.map((n) => ({ ...n, position: { x: n.position.x + 60, y: n.position.y + 60 } })) };
  }, [send, level]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, select, [contenteditable]")) return;
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key === "c") { if (copySelection()) e.preventDefault(); }
      else if (mod && e.key === "v") { e.preventDefault(); paste(); }
      else if (mod && e.key === "d") { e.preventDefault(); if (copySelection()) paste(); }
      else if (mod && !e.shiftKey && e.key.toLowerCase() === "z") { e.preventDefault(); if (!readOnly) undo(); }
      else if (mod && (e.key.toLowerCase() === "y" || (e.shiftKey && e.key.toLowerCase() === "z"))) { e.preventDefault(); if (!readOnly) redo(); }
      else if (e.key === "Escape") { setTraced(null); setSearch(""); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [copySelection, paste, readOnly]); // eslint-disable-line react-hooks/exhaustive-deps

  // ---- AI ----
  // "Fix with AI": put the instruction in the assistant's Edit mode; the person reviews it and presses Apply.
  const [prefill, setPrefill] = useState<{ text: string; at: number } | null>(null);
  const fixWithAI = (instruction: string) => {
    setChatMode("command");
    if (dock === "left") setTab("chat"); else if (dock === "right") setRightTab("chat");
    setPrefill({ text: instruction, at: Date.now() });
  };

  const generateLld = async (nodeId: string) => {
    setGenerating(true);
    try {
      const r = await api.generateLld(pid, branch, nodeId, user.name);
      const parts = Object.entries(r.counts).map(([t, k]) => `${k} ${t}${k === 1 ? "" : "s"}`).join(", ");
      toast(`Generated the design of ${r.name}: ${parts}${r.engine === "offline" ? " (template)" : ""}`);
      window.setTimeout(() => flow.fitView({ padding: 0.2, duration: 400, maxZoom: 1.2 }), 500); // after the live document updates
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
    finally { setGenerating(false); }
  };

  // ---- versions ----
  const commit = async (message: string) => {
    try {
      const v = await api.commit(pid, branch, message, user.name);
      toast(`Committed v${v.number}`);
      refreshMeta();
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
  };
  const showDiff = async (v: number | null) => {
    if (v === null) { setDiff(null); return; }
    try { setDiff({ from: v, data: await api.diff(pid, branch, String(v)) }); }
    catch (e) { toast(`⚠ ${(e as Error).message}`); }
  };
  const restore = async (v: number) => {
    try {
      await api.restore(pid, branch, v, user.name);
      setDiff(null);
      toast(`Restored v${v} into ${branch} (commit to keep it as a version)`);
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
  };
  const createBranch = async (name: string, fromVersion: number | null) => {
    try {
      await api.createBranch(pid, name, branch, fromVersion);
      onBranch(name);
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
  };

  /** Select components and bring them into view (switching diagram level if they live in another one). */
  const focusNodes = (nodeIds: string[]) => {
    if (!nodeIds.length) return;
    const first = stateRef.current?.nodes[nodeIds[0]];
    if (first && levelOf(first) !== level) setLevel(levelOf(first));
    const ids = new Set(nodeIds);
    setNodes((ns) => ns.map((n) => ({ ...n, selected: ids.has(n.id) })));
    flow.fitView({ nodes: nodeIds.map((id) => ({ id })), padding: 0.6, duration: 400, maxZoom: 1.2 });
  };
  const focusIssue = (i: Issue) => {
    if (i.requirement_ids[0]) { setTraced(i.requirement_ids[0]); setTab("requirements"); }
    focusNodes(i.node_ids);
  };

  // Component names and requirement IDs in AI answers link to the canvas. focusNodes reads the latest state
  // through refs, so the links only need rebuilding when component names change.
  const focusRef = useRef(focusNodes);
  focusRef.current = focusNodes;
  const componentNames = Object.values(state?.nodes ?? {}).filter((n) => n.type !== "note")
    .map((n) => `${n.id}\u0000${n.name}`).join("\u0001");
  const entityLinks: EntityLinks = useMemo(() => ({
    components: componentNames ? componentNames.split("\u0001").map((s) => { const [id, name] = s.split("\u0000"); return { id, name }; }) : [],
    onComponent: (id: string) => focusRef.current([id]),
    onRequirement: (id: string) => { setTraced(id); setTab("requirements"); },
  }), [componentNames]);

  const onSearchEnter = () => {
    if (highlight?.size) flow.fitView({ nodes: [...highlight].map((id) => ({ id })), padding: 0.5, duration: 400, maxZoom: 1.2 });
  };

  // What to suggest in the assistant: the LLM writes starting prompts for this project, or follow-ups to the last
  // answer; without AI the offline ones from suggestions.ts are used. (Hooks stay above the early return below.)
  const canvasIsEmpty = !!state && Object.keys(state.nodes).length === 0;
  const chatModeNow: ChatMode = chatMode ?? (canvasIsEmpty && !readOnly ? "design" : readOnly ? "ask" : "command");
  const lastMsg = assistant.messages[assistant.messages.length - 1];
  const lastQuestion = lastMsg?.role === "assistant"
    ? [...assistant.messages].reverse().find((m) => m.role === "user" && m.seq < lastMsg.seq)?.content ?? "" : "";
  const followUp = lastMsg?.role === "assistant" && !lastMsg.data.error && !assistant.busy;
  const aiSuggest = useAiSuggestions(pid, branch, {
    mode: chatModeNow === "command" ? "edit" : "ask", selection: selectedNodeIds,
    conversationId: followUp ? assistant.current?.id : null,
    lastQuestion: followUp ? lastQuestion : "", lastAnswer: followUp ? lastMsg.content + (lastMsg.data.summary?.length ? "\n" + lastMsg.data.summary.join("\n") : "") : "",
  }, !!ai?.available && !!state && chatModeNow !== "design" && (!lastMsg || followUp), state?.rev);

  if (loadError) {
    return (
      <div className="picker">
        <p className="error">{loadError}</p>
        <div className="row">
          {branch !== "main" && <button className="primary" onClick={() => onBranch("main")}>Open the main branch</button>}
          <button onClick={onExit}>Back to projects</button>
        </div>
      </div>
    );
  }

  const others = live.peers.filter((p) => p.clientId !== live.selfId);
  const aiBusy = rt.aiStatus.status === "thinking" ? `AI is applying “${rt.aiStatus.text}” for ${rt.aiStatus.actor}` : null;

  const canvasEmpty = canvasIsEmpty;
  const offline = buildSuggestions(state, analysis, selectedNodeIds); // cheap; no hook after the early return above
  const chatSuggestions = aiSuggest.items ?? (aiSuggest.loading || lastMsg ? [] : asSuggestions(chatModeNow === "command" ? offline.edit : offline.ask));
  const effectiveMode = chatModeNow;
  const chatPanel = (
    <CommandBar assistant={assistant} selection={selectedNodeIds} aiBusy={aiBusy} links={entityLinks}
      memoryCount={memory.length} onOpenMemory={() => setMemoryOpen(true)} meId={me.user.id} isAdmin={can(role, "admin")}
      userName={user.name} readOnly={readOnly} mode={effectiveMode} onMode={setChatMode} dock={dock} onDock={setDock}
      suggestions={chatSuggestions} suggestionsSource={aiSuggest.items ? "ai" : "offline"} suggestionsLoading={aiSuggest.loading}
      prefill={prefill}
      designSlot={state && !readOnly ? (
        <DesignChat pid={pid} branch={branch} user={user} aiAvailable={ai?.available ?? true} canvasEmpty={canvasEmpty}
          remote={designRemote}
          context={{ name: project?.name ?? "", description: project?.description ?? "", components: Object.keys(state.nodes).length,
            requirements: activeRequirements(state).map((r) => ({ id: r.id, title: r.title })).sort((x, y) => x.id.localeCompare(y.id)) }}
          onApplied={(r) => {
            if (r.branch !== branch) { onBranch(r.branch); return; }
            refreshMeta();
            toast(`Applied architecture · v${r.version}`);
            window.setTimeout(() => flow.fitView({ padding: 0.15, duration: 400 }), 400); // after the live document receives the change
          }} />
      ) : undefined} />
  );

  return (
    <div className={`workspace ${canvasFull ? "canvas-full" : ""}`}>
      <header className="topbar">
        <div className="tb-group tb-left">
        <button className="ghost back-btn" onClick={onExit} title="Back to all projects" aria-label="Back to all projects">
          <span aria-hidden>←</span> <span className="back-label">Projects</span>
        </button>
        <ProjectSwitcher me={me} current={project} open={switcherOpen} onOpenChange={setSwitcherOpen}
          onOpen={onOpenProject} onHome={onExit} />
        {readOnly && <span className="ro-badge" title={can(role, "review") ? "Your role can view and comment, but not edit" : "Your role can view, but not edit or comment"}>Read only · {role}</span>}
        <div className="branch">
          <select value={branch} onChange={(e) => onBranch(e.target.value)} aria-label="Branch">
            {branches.map((b) => <option key={b.name} value={b.name}>⎇ {b.name}</option>)}
          </select>
          {branch !== "main" && branches.some((b) => b.name === "main") && (
            <button className="small ghost" onClick={() => onBranch("main")} title="Switch back to the main branch">↩ main</button>
          )}
          {readOnly ? null : newBranch === null ? (
            <button className="small ghost icon-btn" onClick={() => setNewBranch("experiment/")} title="New branch" aria-label="New branch">+</button>
          ) : (
            <form className="row gap" onSubmit={(e) => { e.preventDefault(); createBranch(newBranch.trim(), null); setNewBranch(null); }}>
              <input autoFocus value={newBranch} onChange={(e) => setNewBranch(e.target.value)} aria-label="New branch name" />
              <button className="small primary" disabled={!/^(?=.{1,60}$)[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*$/.test(newBranch.trim())}>Create</button>
              <button type="button" className="small" onClick={() => setNewBranch(null)}>✕</button>
            </form>
          )}
        </div>
        </div>
        <input className="search" type="search" placeholder="Search canvas…" value={search} aria-label="Search the canvas"
          onChange={(e) => { setSearch(e.target.value); setTraced(null); }}
          onKeyDown={(e) => e.key === "Enter" && onSearchEnter()} />
        <div className="spacer" />
        <div className="tb-group tb-right">
        {analysis && (
          <button className="health-pill" onClick={() => setTab("issues")}
            title={`Architecture health: ${analysis.health.issues.errors} error(s), ${analysis.health.issues.warnings} warning(s). `
              + `${analysis.health.traceability}% of requirements are linked to a component. Click for details.`}>
            {analysis.health.issues.errors > 0 && <span className="dot error" />}
            {analysis.health.issues.warnings > 0 && <span className="dot warning" />}
            {analysis.health.issues.errors + analysis.health.issues.warnings === 0
              ? "✓ No issues"
              : `${analysis.health.issues.errors + analysis.health.issues.warnings} issue${analysis.health.issues.errors + analysis.health.issues.warnings === 1 ? "" : "s"}`}
            {" · "}{analysis.health.traceability}% traced
          </button>
        )}
        {ai && (() => {
          const admin = can(role ?? undefined, "admin") && !!project?.org_id;
          const tip = ai.available ? `AI model: ${ai.model}` : admin
            ? "No AI model configured for this organization: click to add a provider key and choose models."
            : "No AI model configured: the design chat and commands use built-in templates. Ask an organization admin to set up AI.";
          return admin
            ? <button className={`ai-pill ${ai.available ? "on" : "off"}`} title={tip} onClick={() => setAiSettingsOpen(true)}>{ai.available ? "AI on" : "AI off · set up"}</button>
            : <span className={`ai-pill ${ai.available ? "on" : "off"}`} title={tip}>{ai.available ? "AI on" : "AI off · templates"}</span>;
        })()}
        <div className="tools-menu" ref={toolsRef}>
          <button className="small" aria-haspopup="menu" aria-expanded={toolsOpen} onClick={() => setToolsOpen((v) => !v)}>Tools ▾</button>
          {toolsOpen && (
            <div className="tools-pop" role="menu">
              <button role="menuitem" onClick={() => { setToolsOpen(false); setCodeOpen(true); }}>
                <b>⟨/⟩ Code analysis</b><span className="muted tiny">Reconstruct a repository, check drift and change impact</span></button>
              <button role="menuitem" onClick={() => { setToolsOpen(false); setDiagramsOpen(true); }}>
                <b>◫ Diagrams</b><span className="muted tiny">C4, sequence, ER, deployment and data flow as Mermaid / PlantUML</span></button>
              <button role="menuitem" onClick={() => { setToolsOpen(false); setKnowledgeOpen(true); }}>
                <b>❖ Knowledge base</b><span className="muted tiny">Ask and search the project documents and meetings, with citations</span></button>
              <button role="menuitem" onClick={() => { setToolsOpen(false); setCostsOpen(true); }}>
                <b>$ Cost estimate</b><span className="muted tiny">Monthly cost of this architecture, line by line, and ways to save</span></button>
              {confluence?.connected && can(role, "edit") && (
                <button role="menuitem" onClick={() => { setToolsOpen(false); setConfluenceOpen(true); }}>
                  <b>⇪ Publish to Confluence</b><span className="muted tiny">The architecture spec as a page in {confluence.space_key}</span></button>
              )}
              {github?.connected && can(role, "edit") && (
                <button role="menuitem" onClick={() => { setToolsOpen(false); setGithubOpen(true); }}>
                  <b>⎇ Push to GitHub</b><span className="muted tiny">Requirements as issues in {github.repo}</span></button>
              )}
              <button role="menuitem" onClick={() => { setToolsOpen(false); setConnectOpen(true); }}>
                <b>⌘ Connect AI agents</b><span className="muted tiny">Claude, Codex and others over MCP</span></button>
              <button role="menuitem" onClick={() => { setToolsOpen(false); setAccessOpen(true); }}>
                <b>⚿ Project access</b><span className="muted tiny">{can(role, "admin") ? "Give people a different role on this project" : "Who can do what on this project"}</span></button>
              <button role="menuitem" onClick={() => { setToolsOpen(false); toggleCanvasFull(); }}>
                <b>⛶ Full-screen canvas</b><span className="muted tiny">Shortcut: F</span></button>
              <div className="tools-sep" />
              <label className="tools-field" hidden={readOnly}><span className="tiny muted">Convert to another platform</span>
        <select className="convert" value="" disabled={converting || readOnly} hidden={readOnly} aria-label="Convert architecture to another platform"
          title="Swap every recognised component for its equivalent on another platform"
          onChange={async (e) => {
            const target = e.target.value;
            if (!target) return;
            setToolsOpen(false);
            setConverting(true);
            try {
              const r = await api.convert(pid, branch, target, user.name);
              toast(r.changed ? `Converted ${r.changed} component(s) to ${catalog?.vendors[target] ?? target}` : "Nothing to convert");
              if (r.skipped.length) toast(`No equivalent for: ${r.skipped.join(", ")}`);
            } catch (err) { toast(`⚠ ${(err as Error).message}`); }
            finally { setConverting(false); }
          }}>
          <option value="">{converting ? "Converting…" : "⇄ Convert to…"}</option>
          <option value="aws">AWS</option><option value="azure">Azure</option>
          <option value="gcp">Google Cloud</option><option value="oss">Open source (self-hosted)</option>
        </select>
              </label>
              <label className="tools-field"><span className="tiny muted">Export diagram</span>
        <select className="convert" value="" disabled={exporting} aria-label="Export this diagram"
          title="Export the diagram you are looking at (the system, or a component's internals)"
          onChange={async (e) => {
            const kind = e.target.value as "png" | "svg" | "html" | "";
            if (!kind) return;
            setToolsOpen(false);
            setExporting(true);
            const name = exportName(project?.name ?? "diagram", branch, level ? state?.nodes[level]?.name ?? level : "");
            try {
              if (kind === "html") toast(`Saved ${await api.exportDiagram(pid, branch, level)}`);
              else {
                await exportCanvasImage(nodes.filter((n) => !n.id.startsWith("ghost:")), kind, name);
                toast(`Saved ${name}.${kind}`);
              }
            } catch (err) { toast(`⚠ ${(err as Error).message}`); }
            finally { setExporting(false); }
          }}>
          <option value="">{exporting ? "Exporting…" : "⤓ Export"}</option>
          <option value="png">PNG image</option>
          <option value="svg">SVG image</option>
          <option value="html">Interactive diagram (HTML)</option>
        </select>
              </label>
              <button role="menuitem" onClick={() => { setToolsOpen(false); setExportOpen(true); }}>
                <b>⤓ Export spec &amp; agent guides</b><span className="muted tiny">Markdown, PDF, JSON, YAML, CLAUDE.md, AGENTS.md, .cursorrules</span></button>
            </div>
          )}
        </div>
        {themeToggle}
        <div className="presence">
          <span className={`conn ${live.status === "connected" && live.synced ? "on" : ""}`}
            title={live.status === "connected" ? (live.synced ? "Live: changes sync instantly" : "Syncing…") : "Reconnecting… your edits are kept and sync when back online"} />
          {others.map((u) => <span key={u.clientId} className="avatar" style={{ background: u.color }} title={u.name}>{u.name[0]?.toUpperCase()}</span>)}
        </div>
        <button className="primary" onClick={() => setPromptFor({ nodeIds: selectedNodeIds, requirementIds: [] })}
          title="Turn this design (or the selected components) into a change spec for a coding agent">
          ⚙ Vibe code{selectedNodeIds.length ? ` (${selectedNodeIds.length})` : ""}
        </button>
        <NotificationBell refreshKey={notifKey} onOpen={openNotification} />
        <UserMenu me={me} user={user} role={role} onLogout={onLogout} />
        </div>
      </header>
      {aiSettingsOpen && project?.org_id && role && (
        <AISettingsDialog orgId={project.org_id} orgName={me.orgs.find((o) => o.id === project.org_id)?.name ?? "Organization"} role={role}
          selfId={me.user.id} onClose={() => { setAiSettingsOpen(false); window.location.reload(); }} />
      )}

      <aside className="sidebar">
        <nav className="tabs side-tabs" role="tablist" aria-label="Project panels">
          {((dock === "left" ? ["chat"] : []).concat(["requirements", "issues", "comments", "versions", "decisions"]) as Tab[]).map((t) => {
            const meta = TAB_META[t];
            const problems = analysis ? analysis.health.issues.errors + analysis.health.issues.warnings : 0;
            const count = t === "requirements" && state ? activeRequirements(state).length : t === "issues" ? problems
              : t === "comments" ? openThreadsCount : 0;
            return (
              <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)} role="tab" aria-selected={tab === t} title={meta.title}>
                <span className="tab-icon" aria-hidden>{meta.icon}</span>
                <span className="tab-label">{meta.label}</span>
                {count > 0 && <span className={`tab-count ${t === "issues" ? (analysis?.health.issues.errors ? "bad" : "warn") : ""}`}>{count}</span>}
              </button>
            );
          })}
        </nav>
        {tab === "chat" && dock === "left" && chatPanel}
        {state && tab === "requirements" && (
          <RequirementsPanel state={state} issues={analysis?.requirement_issues ?? []} traced={traced} onTrace={setTraced} readOnly={readOnly}
            send={send} onPrompt={(rid) => setPromptFor({ nodeIds: [], requirementIds: [rid] })}
            pid={pid} branch={branch} isAdmin={can(role, "admin")} toast={toast} onMemoryChanged={loadMemory}
            jira={jiraByReq} onJira={jira?.connected && can(role, "edit") ? () => setJiraOpen(true) : undefined}
            github={githubByReq} onGitHub={github?.connected && can(role, "edit") ? () => setGithubOpen(true) : undefined}
            onMeetingImported={(c) => { if (c.knowledge) setKbVersion((v) => v + 1); }} />
        )}
        {tab === "issues" && (
          <IssuesPanel pid={pid} branch={branch} analysis={analysis} aiAvailable={!!ai?.available} onFocus={focusIssue}
            onShow={(names) => focusNodes(Object.values(state?.nodes ?? {}).filter((n) => names.includes(n.name)).map((n) => n.id))}
            onFix={fixWithAI} onOpenCode={() => setCodeOpen(true)} />
        )}
        {tab === "comments" && (
          <CommentsPanel pid={pid} branch={branch} comments={comments} members={members} meId={me.user.id} state={state}
            selectedNode={selectedNodeIds.length === 1 ? selectedNodeIds[0] : null} focusThread={focusThread}
            canComment={can(role, "review")} canResolveAny={can(role, "edit")} isAdmin={can(role, "admin")}
            onChanged={loadComments} onShowNode={(id) => focusNodes([id])} toast={toast} />
        )}
        {tab === "versions" && (
          <VersionsPanel pid={pid} role={role} versions={versions} activity={activity} branch={branch} headVersion={headVersion} diff={diff}
            onCommit={commit} onDiff={showDiff} onRestore={restore} onBranchFrom={(v, name) => createBranch(name, v)} onReviewed={refreshMeta}
            branches={branches} onMerge={readOnly ? undefined : setMergeSource} />
        )}
        {tab === "decisions" && (
          <DecisionsPanel pid={pid} branches={branches} user={user} role={role ?? undefined} toast={toast}
            onAdopted={(r) => { refreshMeta(); toast(`Adopted ${r.source} into ${r.branch} (${r.decisionId}) · v${r.version}`); }} />
        )}
      </aside>

      <main className="canvas" ref={wrapper} onMouseMove={onMouseMove}
        onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }} onDrop={onDrop}>
        <ReactFlow
          nodes={nodes} edges={edges} nodeTypes={nodeTypes}
          onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onDelete={onDelete}
          onNodeDragStop={onNodeDragStop} onConnect={onConnect}
          nodesDraggable={!readOnly} nodesConnectable={!readOnly} edgesReconnectable={!readOnly}
          onNodeDoubleClick={(_, n) => { if (!n.id.startsWith("ghost:") && n.data.model.type !== "note") openLevel(n.id); }}
          zoomOnDoubleClick={false}
          deleteKeyCode={readOnly ? null : ["Delete", "Backspace"]} multiSelectionKeyCode={["Shift", "Meta", "Control"]}
          selectionKeyCode="Shift" colorMode={colorMode} fitView fitViewOptions={{ padding: 0.15 }} minZoom={0.1} maxZoom={2.5} proOptions={{ hideAttribution: true }}
        >
          <Background variant={BackgroundVariant.Dots} gap={20} size={1} />
          <Controls position="center-right">
            {!readOnly && (
              <>
                <ControlButton onClick={() => tidy(true)} title="Tidy up: arrange this diagram in tiers (L) · Ctrl+Z undoes"
                  aria-label="Tidy up the layout" className="tidy-btn">
                  <svg viewBox="0 0 16 16" aria-hidden><path d="M2 2h5v4H2zM9 2h5v4H9zM5.5 10h5v4h-5zM4.5 6v2h7V6M8 8v2" fill="none" stroke="currentColor" strokeWidth="1.4" /></svg>
                </ControlButton>
                <ControlButton onClick={toggleAutoTidy} aria-pressed={autoTidy} className={`auto-tidy-btn ${autoTidy ? "on" : ""}`}
                  title={autoTidy ? "Auto-arrange after AI edits: on" : "Auto-arrange after AI edits: off"} aria-label="Auto-arrange after AI edits">
                  A
                </ControlButton>
              </>
            )}
          </Controls>
          <MiniMap position="top-right" pannable zoomable nodeColor={(n) => TYPE_META[(n.data as { model: ArchNode }).model.type]?.color ?? "#999"} />
          <ViewportPortal>
            {others.filter((c) => c.cursor && (c.cursor.level ?? "") === level).map((c) => (
              <div key={c.clientId} className="remote-cursor" style={{ transform: `translate(${c.cursor!.x}px, ${c.cursor!.y}px)`, color: c.color }}>
                <svg width="16" height="16" viewBox="0 0 16 16"><path d="M1 1 L6 15 L8 9 L14 7 Z" fill="currentColor" stroke="white" strokeWidth="1" /></svg>
                <span style={{ background: c.color }}>{c.name}</span>
              </div>
            ))}
          </ViewportPortal>
        </ReactFlow>
        {!readOnly && state && <Palette catalog={catalog} startOpen={Object.keys(state.nodes).length === 0} />}
        {canvasFull && (
          <button className="canvas-exit-full" onClick={toggleCanvasFull} title="Exit full screen (Esc or F)">✕ Exit full screen</button>
        )}
        {diff && <div className="diff-banner">Diff: v{diff.from} → working · {diff.data.total_changes} change(s) <button className="small" onClick={() => setDiff(null)}>Exit</button></div>}
        {traced && <div className="diff-banner">Tracing {traced} <button className="small" onClick={() => setTraced(null)}>Clear</button></div>}
        {level && (
          <nav className="crumbs" aria-label="Diagram level">
            <button className="link" onClick={() => openLevel("")}>System</button>
            {crumbs.map((c, i) => (
              <span key={c.id}>
                <span className="muted"> › </span>
                {i === crumbs.length - 1 ? <b>{c.name}</b> : <button className="link" onClick={() => openLevel(c.id)}>{c.name}</button>}
              </span>
            ))}
            <span className="muted tiny">internals</span>
          </nav>
        )}
        {state && level && !childCount[level] && (
          <div className="empty-canvas lld-empty">
            <h3>{state.nodes[level]?.name}: low-level design</h3>
            <p className="muted">Generate components, API endpoints, tables and events from this component's connections
              and requirements, or drag elements from the palette.</p>
            <button className="primary" disabled={generating || readOnly} onClick={() => generateLld(level)}>
              {generating ? "Generating…" : "Generate low-level design"}
            </button>
          </div>
        )}
        {state && !level && Object.keys(state.nodes).length === 0 && (
          <div className="empty-canvas">
            <h3>Empty canvas</h3>
            <p className="muted">Drag components from the palette, or describe your system in the assistant (Design mode), e.g.
              “Build an AI customer-support platform where customers use WhatsApp and web.”</p>
          </div>
        )}
        {dock === "float" && chatPanel}
        <div className="toasts">{toasts.map((t) => <div key={t.id} className="toast">{t.text}</div>)}</div>
      </main>

      <aside className={`rightbar ${dock === "right" ? "with-chat" : ""}`}>
        {dock === "right" && (
          <nav className="tabs">
            <button className={rightTab === "chat" ? "on" : ""} onClick={() => setRightTab("chat")} role="tab" aria-selected={rightTab === "chat"}>Assistant</button>
            <button className={rightTab === "props" ? "on" : ""} onClick={() => setRightTab("props")} role="tab" aria-selected={rightTab === "props"}>
              Properties{selectedNodeIds.length + selectedEdgeIds.length > 0 && <span className="tab-count">{selectedNodeIds.length + selectedEdgeIds.length}</span>}
            </button>
          </nav>
        )}
        {dock === "right" && rightTab === "chat" && chatPanel}
        {state && (dock !== "right" || rightTab === "props") && (
          <Inspector catalog={catalog} state={state} nodeIds={selectedNodeIds} edgeIds={selectedEdgeIds} send={send}
            pid={pid} branch={branch} onOpenInternals={openLevel} readOnly={readOnly} jira={jira?.links}
            stale={analysis?.stale_links} onReviewed={markReviewed}
            onPrompt={() => setPromptFor({ nodeIds: selectedNodeIds, requirementIds: [] })} />
        )}
      </aside>

      {exportOpen && (
        <ExportDialog pid={pid} branch={branch} aiAvailable={!!ai?.available} toast={toast} onClose={() => setExportOpen(false)} />
      )}
      {jiraOpen && jira && (
        <JiraDialog pid={pid} branch={branch} defaultKey={jira.project_key} aiAvailable={!!ai?.available}
          onClose={() => setJiraOpen(false)} onChanged={loadJira} />
      )}
      {knowledgeOpen && (
        <KnowledgeDialog pid={pid} canEdit={can(role, "edit")} version={kbVersion} onClose={() => setKnowledgeOpen(false)} />
      )}
      {costsOpen && (
        <CostDialog pid={pid} branch={branch} aiAvailable={!!ai?.available} rev={state?.rev ?? 0} onClose={() => setCostsOpen(false)}
          onSelect={(id) => { setCostsOpen(false); focusNodes([id]); }} />
      )}
      {githubOpen && github && (
        <GitHubDialog pid={pid} branch={branch} repo={github.repo ?? ""} onClose={() => setGithubOpen(false)} onChanged={loadGithub} />
      )}
      {confluenceOpen && confluence && (
        <ConfluenceDialog pid={pid} branch={branch} space={confluence.space_key ?? ""} onClose={() => setConfluenceOpen(false)} />
      )}
      {accessOpen && (
        <ProjectAccessDialog pid={pid} projectName={project?.name ?? "this project"} selfId={me.user.id}
          onClose={() => setAccessOpen(false)}
          onChanged={() => { api.project(pid, branch).then((p) => setRole(p.role)).catch(() => {}); }} />
      )}
      {connectOpen && project && (
        <ConnectDialog orgId={project.org_id ?? "org-default"} canManage={can(role, "admin")} onClose={() => setConnectOpen(false)} />
      )}
      {diagramsOpen && (
        <DiagramsDialog pid={pid} branch={branch} projectName={project?.name ?? "diagram"} aiAvailable={!!ai?.available}
          rev={state?.rev ?? 0} onClose={() => setDiagramsOpen(false)} />
      )}
      {codeOpen && (
        <CodeDialog pid={pid} branch={branch} readOnly={readOnly} toast={toast} onClose={() => setCodeOpen(false)}
          onOpenBranch={(name) => { if (name !== branch) onBranch(name); }}
          onFocus={(i) => { setTab("issues"); focusIssue(i); }} />
      )}
      {memoryOpen && (
        <MemoryDialog pid={pid} branch={branch} items={memory} readOnly={readOnly} onChanged={loadMemory}
          onClose={() => setMemoryOpen(false)} />
      )}
      {mergeSource && (
        <MergeDialog pid={pid} branches={branches} source={mergeSource}
          target={mergeSource !== branch ? branch : branches.some((b) => b.name === "main") && branch !== "main" ? "main" : branches.find((b) => b.name !== mergeSource)?.name ?? branch}
          onClose={() => setMergeSource(null)}
          onMerged={(r) => {
            setMergeSource(null);
            refreshMeta();
            toast(`Merged ${r.source_branch} into ${r.branch} · v${r.version}${r.conflicts_resolved ? ` (${r.conflicts_resolved} conflict(s) resolved)` : ""}`);
            // show what the merge changed on the canvas, like "Compare with now"
            if (r.branch === branch && r.safety_version !== null) showDiff(r.safety_version);
          }} />
      )}
      {promptFor && <PromptDialog pid={pid} branch={branch} nodeIds={promptFor.nodeIds} requirementIds={promptFor.requirementIds} readOnly={readOnly}
        onClose={() => setPromptFor(null)} />}
    </div>
  );
}
