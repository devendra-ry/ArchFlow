import { useMemo, useState } from "react";
import type { Catalog, CatalogItem } from "../types";
import { TYPE_META, TypeIcon } from "./ArchNode";

export const DRAG_MIME = "application/archflow-catalog";

export const VENDOR_STYLE: Record<string, { short: string; color: string }> = {
  aws: { short: "AWS", color: "#ff9900" },
  azure: { short: "Azure", color: "#0078d4" },
  gcp: { short: "GCP", color: "#34a853" },
  oss: { short: "OSS", color: "#6b7280" },
  saas: { short: "SaaS", color: "#8e4ec6" },
};

const VENDOR_ORDER = ["all", "generic", "aws", "azure", "gcp", "oss", "saas"];

interface Props {
  catalog: Catalog | null;
  /** Open on first use when there is no saved choice (uncontrolled). */
  startOpen?: boolean;
  /** Controlled open state; leave out to let the palette remember it itself. */
  open?: boolean;
  onOpenChange?: (open: boolean) => void;
  /** Click or tap an item to add it (touch screens cannot drag from the palette). */
  onAdd?: (item: CatalogItem) => void;
  /** Show as a bottom sheet over the canvas (narrow screens). */
  sheet?: boolean;
}

/** Searchable component catalog: generic building blocks plus concrete AWS/Azure/GCP/OSS/SaaS services. */
export function Palette({ catalog, startOpen = true, open, onOpenChange, onAdd, sheet = false }: Props) {
  const [query, setQuery] = useState("");
  const [vendor, setVendor] = useState("all");
  // Open on an empty canvas, out of the way otherwise; your last choice wins.
  const [collapsedState, setCollapsedState] = useState(() => {
    try { const v = localStorage.getItem("archflow.palette"); if (v) return v === "closed"; } catch { /* ignore */ }
    return !startOpen;
  });
  const collapsed = open === undefined ? collapsedState : !open;
  const setCollapsed = (c: boolean) => {
    if (onOpenChange) { onOpenChange(!c); return; }
    setCollapsedState(c);
    try { localStorage.setItem("archflow.palette", c ? "closed" : "open"); } catch { /* ignore */ }
  };

  const groups = useMemo(() => {
    const q = query.trim().toLowerCase();
    const items = (catalog?.items ?? []).filter((c) =>
      (vendor === "all" ? c.vendor === "generic" || q : c.vendor === vendor) &&
      (!q || `${c.name} ${c.kind_label} ${c.category} ${catalog?.vendors[c.vendor] ?? ""}`.toLowerCase().includes(q)));
    const out = new Map<string, CatalogItem[]>();
    for (const c of items) out.set(c.category, [...(out.get(c.category) ?? []), c]);
    return [...out.entries()];
  }, [catalog, query, vendor]);
  const total = groups.reduce((n, [, items]) => n + items.length, 0);

  const onDragStart = (e: React.DragEvent, item: CatalogItem) => {
    e.dataTransfer.setData(DRAG_MIME, JSON.stringify(item));
    e.dataTransfer.effectAllowed = "move";
  };

  if (collapsed) {
    if (sheet) return null; // the canvas action bar opens it
    return (
      <button className="palette-toggle" onClick={() => setCollapsed(false)} title="Show components">
        <span aria-hidden className="palette-toggle-plus">＋</span> Components
      </button>
    );
  }
  const panel = (
    <div className={`palette ${sheet ? "sheet" : ""}`} aria-label="Component catalog">
      {sheet && <div className="af-sheet-grip" aria-hidden />}
      <div className="palette-title">
        <b>Components</b>
        <span className="palette-count">{total}</span>
        <span className="grow" />
        <button className="ghost small icon-btn" onClick={() => setCollapsed(true)} aria-label="Hide components" title="Hide">
          {sheet ? "✕" : "⟨"}
        </button>
      </div>
      <div className="palette-head">
        <span className="palette-search-icon" aria-hidden>
          <svg viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.5" /><path d="M10.5 10.5L14 14" /></svg>
        </span>
        <input type="search" placeholder="Search 140+ components…" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search components" />
      </div>
      <div className="vendor-chips" role="tablist" aria-label="Vendor">
        {VENDOR_ORDER.map((v) => (
          <button key={v} role="tab" aria-selected={vendor === v} className={`vchip ${vendor === v ? "on" : ""}`}
            style={VENDOR_STYLE[v] ? { ["--vc" as string]: VENDOR_STYLE[v].color } : undefined}
            onClick={() => setVendor(v)}>
            {v === "all" ? "All" : v === "generic" ? "Generic" : VENDOR_STYLE[v]?.short ?? v}
          </button>
        ))}
      </div>
      <div className="palette-list">
        {groups.length === 0 && <p className="muted tiny pad">No matches.</p>}
        {groups.map(([category, items]) => (
          <section key={category} className="palette-group">
            <div className="palette-cat">{category}<span className="palette-cat-n">{items.length}</span></div>
            {items.map((c) => (
              <div key={c.id} className="palette-item" draggable={!sheet} onDragStart={(e) => onDragStart(e, c)}
                role={onAdd ? "button" : undefined} tabIndex={onAdd ? 0 : undefined}
                onClick={onAdd ? () => onAdd(c) : undefined}
                onKeyDown={onAdd ? (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onAdd(c); } } : undefined}
                title={`${c.kind_label ? `${c.name} · ${c.kind_label}` : c.name} · ${sheet ? "tap to add" : "drag onto the canvas, or click to add"}`}
                style={{ ["--accent" as string]: TYPE_META[c.type].color }}>
                <span className="pi-icon" aria-hidden><TypeIcon type={c.type} size={14} /></span>
                <span className="pi-text">
                  <span className="pi-name">{c.name}</span>
                  {c.kind_label && c.kind_label !== c.name && <span className="pi-kind">{c.kind_label}</span>}
                </span>
                {VENDOR_STYLE[c.vendor] && (
                  <span className="vbadge" style={{ ["--vc" as string]: VENDOR_STYLE[c.vendor].color }}>{VENDOR_STYLE[c.vendor].short}</span>
                )}
              </div>
            ))}
          </section>
        ))}
        {vendor === "all" && !query && <p className="muted tiny pad">Pick a vendor or search to see cloud services.</p>}
      </div>
      <div className="palette-foot tiny muted">{sheet ? "Tap a component to add it" : "Drag onto the canvas, or click to add"}</div>
    </div>
  );
  if (!sheet) return panel;
  return (
    <div className="palette-backdrop" onClick={(e) => { if (e.target === e.currentTarget) setCollapsed(true); }}>{panel}</div>
  );
}
