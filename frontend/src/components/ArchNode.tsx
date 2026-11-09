import { Handle, NodeToolbar, Position, type NodeProps, type Node } from "@xyflow/react";
import { CornerDownRight, Icon, MessageSquare, RotateCw, Trash2, TriangleAlert } from "../icons";
import { memo } from "react";
import {
  Bot, Box, BrainCircuit, Component, Database, Globe, HardDrive, ListOrdered, MonitorSmartphone, Network, Plug, Radio, Route, Shield,
  StickyNote, Table2, Zap, type LucideIcon,
} from "lucide-react";
import { brandIcon } from "../brandIcons";
import type { ArchNode as ArchNodeModel, JiraLink, NodeType } from "../types";
import { VENDOR_STYLE } from "./Palette";

export const TYPE_META: Record<NodeType, { label: string; color: string; glyph: string; icon: LucideIcon }> = {
  client: { label: "Client", color: "#0090ff", glyph: "▭", icon: MonitorSmartphone },
  gateway: { label: "Gateway", color: "#6e56cf", glyph: "⇶", icon: Network },
  service: { label: "Service", color: "#30a46c", glyph: "◆", icon: Box },
  database: { label: "Database", color: "#f76b15", glyph: "⛁", icon: Database },
  cache: { label: "Cache", color: "#e5484d", glyph: "⚡", icon: Zap },
  queue: { label: "Queue", color: "#ffb224", glyph: "☰", icon: ListOrdered },
  storage: { label: "Storage", color: "#ad7f58", glyph: "▤", icon: HardDrive },
  agent: { label: "AI Agent", color: "#d6409f", glyph: "✦", icon: Bot },
  llm: { label: "LLM", color: "#8e4ec6", glyph: "◎", icon: BrainCircuit },
  mcp: { label: "MCP", color: "#12a594", glyph: "⌘", icon: Plug },
  external: { label: "External", color: "#8b8d98", glyph: "↗", icon: Globe },
  security: { label: "Security", color: "#dc3e42", glyph: "⛨", icon: Shield },
  note: { label: "Note", color: "#ffc53d", glyph: "✎", icon: StickyNote },
  component: { label: "Component", color: "#3e63dd", glyph: "▣", icon: Component },
  endpoint: { label: "Endpoint", color: "#0d74ce", glyph: "⇢", icon: Route },
  table: { label: "Table", color: "#f76b15", glyph: "▦", icon: Table2 },
  event: { label: "Event", color: "#ffb224", glyph: "ϟ", icon: Radio },
};

/** The stroked icon for a component type (``glyph`` stays for plain-text contexts such as exports). */
export function TypeIcon({ type, size = 16 }: { type: NodeType; size?: number }) {
  const Cmp = (TYPE_META[type] ?? TYPE_META.service).icon;
  return <Cmp size={size} strokeWidth={1.75} aria-hidden focusable={false} className="type-icon" />;
}

/** Columns of a table node: "id uuid pk, total numeric" → [{name, type, key}]. */
export function parseColumns(text: string | undefined) {
  return (text ?? "").split(/[,\n]/).map((c) => c.trim().split(/\s+/)).filter((b) => b[0])
    .map((b) => ({ name: b[0], type: b[1] ?? "", key: (b.slice(2).find((x) => /^(pk|fk|uk)$/i.test(x)) ?? "").toUpperCase() }));
}

export type DiffMark = "added" | "removed" | "changed" | null;

export interface ArchNodeData extends Record<string, unknown> {
  model: ArchNodeModel;
  dimmed?: boolean;
  highlighted?: boolean;
  diff?: DiffMark;
  remote?: { name: string; color: string }[];
  issues?: number;
  childCount?: number; // elements in this component's internal (LLD) diagram
  comments?: number; // open comment threads on this component
  requirementIds?: string[]; // linked requirements to show (soft-deleted ones left out); default: all links
  stale?: string[]; // linked requirements that changed after this component did
  jira?: JiraLink; // the Jira epic (or sub-task) pushed for this component, with the status last read from Jira
  onComments?: () => void;
  onOpenInternals?: () => void; // open this component's internal (LLD) diagram
  onRemove?: () => void; // left out when the canvas is read-only
}

export type ArchFlowNode = Node<ArchNodeData, "arch">;

function ArchNodeView({ data, selected, dragging }: NodeProps<ArchFlowNode>) {
  const { model, dimmed, highlighted, diff, remote, issues, childCount, comments, onComments, stale, onOpenInternals, onRemove, jira } = data;
  const reqIds = data.requirementIds ?? model.requirement_ids;
  const meta = TYPE_META[model.type] ?? TYPE_META.service;
  const logo = brandIcon(model.technology, model.name);
  const ring = remote?.[0]?.color;
  const cls = ["arch-node", `t-${model.type}`, selected && "selected", dimmed && "dimmed", highlighted && "highlighted",
    diff && `diff-${diff}`].filter(Boolean).join(" ");

  if (model.type === "note") {
    return (
      <div className={cls} style={ring ? { boxShadow: `0 0 0 2px ${ring}` } : undefined}>
        <div className="note-text">{model.description || model.name}</div>
      </div>
    );
  }

  const subtitle = model.type === "event" && model.props?.channel ? `on ${model.props.channel}`
    : model.type !== "table" && model.type !== "event" ? model.technology : "";
  return (
    <div className={cls} style={{ ["--accent" as string]: meta.color, ...(ring ? { boxShadow: `0 0 0 2px ${ring}` } : {}) }}>
      {(onOpenInternals || onRemove) && (
        // Unset = React Flow's default: shown only while this is the one selected node (no stacks of toolbars on multi-select).
        <NodeToolbar isVisible={dragging ? false : undefined} position={Position.Top} offset={8} className="node-toolbar">
          {onOpenInternals && (
            <button onClick={onOpenInternals} title="Open the internal design (double-click)">
              <Icon as={CornerDownRight} size={14} /> {childCount ? `Internals (${childCount})` : "Design internals"}</button>
          )}
          {onComments && <button onClick={onComments} title="Read the comments on this component"><Icon as={MessageSquare} size={14} /> Comments</button>}
          {onRemove && <button className="danger" onClick={onRemove} title="Delete (Del)" aria-label={`Delete ${model.name}`}><Icon as={Trash2} size={14} /></button>}
        </NodeToolbar>
      )}
      <Handle type="target" position={Position.Top} />
      <Handle type="target" position={Position.Left} id="l" />
      <div className="an-head">
        <span className="an-icon" aria-hidden={!logo}>
          {logo ? (
            <svg className={`an-logo ${logo.dark ? "dark-mark" : ""}`} viewBox="0 0 24 24" role="img" aria-label={logo.title}
              style={{ ["--brand" as string]: logo.hex }}><title>{logo.title}</title><path d={logo.path} /></svg>
          ) : <TypeIcon type={model.type} size={16} />}
        </span>
        <div className="an-titles">
          {model.type === "endpoint" ? (
            <div className="an-endpoint">
              <span className={`http-m m-${(model.props?.method || "GET").toLowerCase()}`}>{(model.props?.method || model.name.split(" ")[0] || "GET").toUpperCase()}</span>
              <span className="mono">{model.props?.path || model.name.split(" ").slice(1).join(" ") || "/"}</span>
            </div>
          ) : (
            <div className="an-name">{model.name}</div>
          )}
          <div className="an-sub">
            <span className="an-type">{meta.label}</span>
            {subtitle && <span className="an-tech">{subtitle}</span>}
          </div>
        </div>
      </div>
      {(model.vendor && VENDOR_STYLE[model.vendor] || !!issues || !!stale?.length || !!comments || !!childCount) && (
        <div className="an-badges">
          {model.vendor && VENDOR_STYLE[model.vendor] && (
            <span className="vbadge" style={{ ["--vc" as string]: VENDOR_STYLE[model.vendor].color }}>{VENDOR_STYLE[model.vendor].short}</span>
          )}
          {!!issues && <span className="an-issues" title={`${issues} issue(s)`}><Icon as={TriangleAlert} size={12} /> {issues}</span>}
          {!!stale?.length && (
            <span className="an-stale" title={`Requirement${stale.length > 1 ? "s" : ""} changed since this component: ${stale.join(", ")}. Review it in Properties.`}>
              <Icon as={RotateCw} size={12} /> {stale.length}</span>
          )}
          {!!comments && (
            <button className="an-comments nodrag" title={`${comments} open comment thread(s) · click to read`} aria-label={`${comments} open comments`}
              onClick={(e) => { e.stopPropagation(); onComments?.(); }} onDoubleClick={(e) => e.stopPropagation()}><Icon as={MessageSquare} size={12} /> {comments}</button>
          )}
          {!!childCount && <span className="an-children" title={`${childCount} internal element(s) · double-click to open`}><Icon as={CornerDownRight} size={12} /> {childCount}</span>}
        </div>
      )}
      {model.type === "table" && (() => {
        const cols = parseColumns(model.props?.columns);
        return cols.length > 0 && (
          <ul className="an-cols mono">
            {cols.slice(0, 6).map((c) => <li key={c.name}><span>{c.key && <b>{c.key} </b>}{c.name}</span><span className="muted">{c.type}</span></li>)}
            {cols.length > 6 && <li className="muted">+{cols.length - 6} more</li>}
          </ul>
        );
      })()}
      {reqIds.length > 0 && (
        <div className="an-reqs">
          {reqIds.slice(0, 4).map((r) => <span key={r} className={`chip ${stale?.includes(r) ? "stale" : ""}`}>{r}</span>)}
          {reqIds.length > 4 && <span className="chip">+{reqIds.length - 4}</span>}
        </div>
      )}
      {jira && <JiraChip link={jira} />}
      {remote && remote.length > 0 && (
        <div className="an-remote" style={{ background: remote[0].color }}>{remote.map((r) => r.name).join(", ")}</div>
      )}
      <Handle type="source" position={Position.Bottom} />
      <Handle type="source" position={Position.Right} id="r" />
    </div>
  );
}

const JIRA_CATEGORY: Record<string, string> = { new: "To Do", indeterminate: "In Progress", done: "Done" };

/** The component's Jira issue: key and status colour (To Do / In Progress / Done), opening the issue. */
function JiraChip({ link }: { link: JiraLink }) {
  const cat = link.status_category || "unknown";
  const label = JIRA_CATEGORY[cat] ?? (link.status || "Not synced yet");
  return (
    <a className={`an-jira nodrag ${cat}`} href={link.external_url || undefined} target="_blank" rel="noreferrer"
      title={`Jira ${link.external_key}: ${link.status || "status not synced yet"}`} aria-label={`Jira ${link.external_key}, ${label}`}
      onClick={(e) => e.stopPropagation()} onDoubleClick={(e) => e.stopPropagation()}>
      <span className="an-jira-dot" aria-hidden />{link.external_key}<span className="an-jira-status">{label}</span>
    </a>
  );
}

export const ArchNodeComponent = memo(ArchNodeView);
