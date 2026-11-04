import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export interface SelectOption<V extends string = string> {
  value: V;
  label: string;
  /** A second, muted line under the label. */
  hint?: string;
  icon?: React.ReactNode;
  disabled?: boolean;
}

interface Props<V extends string> {
  value: V;
  onChange: (value: V) => void;
  options: readonly SelectOption<V>[];
  "aria-label"?: string;
  title?: string;
  disabled?: boolean;
  className?: string;
  /** Shown when the value matches no option (action pickers such as "Swap for…"). */
  placeholder?: string;
  size?: "sm" | "md";
  /** Width of the list, at least the trigger's width. */
  menuWidth?: number;
}

/** Options whose label is the value itself. */
export function plainOptions<V extends string>(values: readonly V[]): SelectOption<V>[] {
  return values.map((v) => ({ value: v, label: v }));
}

/** Arrow keys, Home and End move focus between the items of a role="menu" popover. */
export function onMenuKeyDown(e: React.KeyboardEvent<HTMLElement>) {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
  const items = [...e.currentTarget.querySelectorAll<HTMLElement>('[role="menuitem"]:not(:disabled)')]
    .filter((el) => el.offsetParent !== null);
  if (!items.length) return;
  e.preventDefault();
  const i = items.indexOf(document.activeElement as HTMLElement);
  const next = e.key === "Home" ? 0 : e.key === "End" ? items.length - 1
    : e.key === "ArrowDown" ? (i + 1) % items.length : (i <= 0 ? items.length - 1 : i - 1);
  items[next].focus();
}

/** Narrow screens show the list as a bottom sheet instead of a popover. */
const SHEET_QUERY = "(max-width: 640px)";
const GAP = 6;
const EDGE = 8;

/**
 * A select-only combobox: a button showing the chosen option, opening a listbox popover (a bottom sheet on phones).
 * Keyboard: arrows/Home/End move, Enter/Space choose, Esc closes, typing jumps to a matching option.
 */
export function Select<V extends string>({
  value, onChange, options, "aria-label": ariaLabel, title, disabled, className = "", placeholder = "Select…", size = "md", menuWidth,
}: Props<V>) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const [sheet, setSheet] = useState(false);
  const [pos, setPos] = useState<React.CSSProperties>({ visibility: "hidden" });
  const trigger = useRef<HTMLButtonElement>(null);
  const pop = useRef<HTMLDivElement>(null);
  const typed = useRef({ text: "", at: 0 });
  const uid = useId();
  const listId = `${uid}-list`;
  const optId = (i: number) => `${uid}-opt-${i}`;
  const selectedIndex = options.findIndex((o) => o.value === value);
  const selected = selectedIndex >= 0 ? options[selectedIndex] : null;

  const enabled = (i: number) => i >= 0 && i < options.length && !options[i].disabled;
  const step = (from: number, dir: 1 | -1) => {
    for (let i = from + dir, n = 0; n < options.length; i += dir, n++) {
      const k = (i + options.length) % options.length;
      if (enabled(k)) return k;
    }
    return from;
  };

  const show = () => {
    if (disabled) return;
    setSheet(window.matchMedia?.(SHEET_QUERY).matches ?? false);
    setActive(selectedIndex >= 0 ? selectedIndex : step(-1, 1));
    setOpen(true);
  };
  const close = useCallback((refocus = true) => {
    setOpen(false);
    setPos({ visibility: "hidden" });
    if (refocus) trigger.current?.focus({ preventScroll: true });
  }, []);
  const choose = (i: number) => {
    if (!enabled(i)) return;
    close();
    if (options[i].value !== value) onChange(options[i].value);
  };

  // Place the popover under the trigger, or above it when there is more room there; keep it inside the viewport.
  const place = useCallback(() => {
    const t = trigger.current, p = pop.current;
    if (!t || !p) return;
    const r = t.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    const width = Math.min(Math.max(r.width, menuWidth ?? 0, 160), vw - EDGE * 2);
    const left = Math.min(Math.max(EDGE, r.left), vw - width - EDGE);
    const below = vh - r.bottom - GAP - EDGE, above = r.top - GAP - EDGE;
    const natural = Math.min(p.scrollHeight, 340);
    const up = below < natural && above > below;
    const maxHeight = Math.max(120, Math.min(340, up ? above : below));
    setPos(up
      ? { left, width, maxHeight, bottom: vh - r.top + GAP }
      : { left, width, maxHeight, top: r.bottom + GAP });
  }, [menuWidth]);

  useLayoutEffect(() => { if (open && !sheet) place(); }, [open, sheet, place, options.length]);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (!trigger.current?.contains(t) && !pop.current?.contains(t)) close(false);
    };
    const onMove = () => { if (!sheet) place(); };
    document.addEventListener("pointerdown", onDown, true);
    window.addEventListener("resize", onMove);
    window.addEventListener("scroll", onMove, true);
    return () => {
      document.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("resize", onMove);
      window.removeEventListener("scroll", onMove, true);
    };
  }, [open, sheet, place, close]);

  useEffect(() => {
    if (open && active >= 0) document.getElementById(optId(active))?.scrollIntoView({ block: "nearest" });
  }, [open, active]); // eslint-disable-line react-hooks/exhaustive-deps

  const typeAhead = (ch: string) => {
    const now = Date.now();
    const t = typed.current;
    t.text = now - t.at > 700 ? ch : t.text + ch;
    t.at = now;
    const q = t.text.toLowerCase();
    const start = open ? active : selectedIndex;
    // A new first letter cycles to the next match; a longer prefix refines from the current one.
    const base = Math.max(start, 0), offset = start < 0 || t.text.length > 1 ? 0 : 1;
    const plain = (s: string) => s.toLowerCase().replace(/^[^\p{L}\p{N}]+/u, "");
    for (let n = 0; n < options.length; n++) {
      const k = (base + offset + n) % options.length;
      if (enabled(k) && plain(options[k].label).startsWith(plain(q) || q)) return k;
    }
    return -1;
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    const k = e.key;
    let handled = true;
    if (!open) {
      if (k === "ArrowDown" || k === "ArrowUp" || k === "Enter" || k === " ") show();
      else if (k.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const i = typeAhead(k);
        show();
        if (i >= 0) setActive(i);
      } else handled = false;
    } else if (k === "ArrowDown") setActive((a) => step(a, 1));
    else if (k === "ArrowUp") setActive((a) => step(a, -1));
    else if (k === "Home") setActive(step(-1, 1));
    else if (k === "End") setActive(step(options.length, -1));
    else if (k === "Enter" || (k === " " && Date.now() - typed.current.at > 700)) choose(active);
    else if (k === "Escape") close();
    else if (k === "Tab") { close(false); handled = false; }
    else if (k.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) { const i = typeAhead(k); if (i >= 0) setActive(i); }
    else handled = false;
    if (handled) {
      e.preventDefault();
      e.stopPropagation(); // keep dialog Esc, canvas shortcuts (F, L, Delete) and the like out of it
    }
  };

  const list = (
    <ul className="af-listbox" role="listbox" id={listId} aria-label={ariaLabel}>
      {options.map((o, i) => (
        <li key={o.value || `_${i}`} id={optId(i)} role="option" aria-selected={o.value === value} aria-disabled={o.disabled || undefined}
          className={`af-option ${i === active ? "active" : ""} ${o.value === value ? "selected" : ""}`}
          onPointerMove={() => { if (i !== active && enabled(i)) setActive(i); }}
          onMouseDown={(e) => e.preventDefault()} onClick={() => choose(i)}>
          {o.icon && <span className="af-option-icon" aria-hidden>{o.icon}</span>}
          <span className="af-option-text">
            <span className="af-option-label">{o.label}</span>
            {o.hint && <span className="af-option-hint">{o.hint}</span>}
          </span>
          <svg className="af-check" viewBox="0 0 16 16" aria-hidden><path d="M3.5 8.5l3 3 6-7" /></svg>
        </li>
      ))}
    </ul>
  );

  return (
    <>
      <button ref={trigger} type="button" role="combobox" aria-haspopup="listbox" aria-expanded={open}
        aria-controls={open ? listId : undefined} aria-activedescendant={open && active >= 0 ? optId(active) : undefined}
        aria-label={ariaLabel} title={title} disabled={disabled} data-value={value}
        className={`af-select nokey ${size === "sm" ? "sm" : ""} ${open ? "open" : ""} ${selected ? "" : "placeholder"} ${className}`}
        onClick={() => (open ? close() : show())} onKeyDown={onKeyDown}>
        {selected?.icon && <span className="af-select-icon" aria-hidden>{selected.icon}</span>}
        <span className="af-select-value">{selected ? selected.label : placeholder}</span>
        <svg className="af-chevron" viewBox="0 0 16 16" aria-hidden><path d="M4 6l4 4 4-4" /></svg>
      </button>
      {open && createPortal(
        sheet ? (
          <div className="af-sheet-backdrop" data-af-popover onPointerDown={(e) => { if (e.target === e.currentTarget) close(false); }}
            onClick={(e) => e.stopPropagation()}>
            <div className="af-sheet" ref={pop} onKeyDown={onKeyDown}>
              <div className="af-sheet-grip" aria-hidden />
              {ariaLabel && <div className="af-sheet-title">{ariaLabel}</div>}
              {list}
            </div>
          </div>
        ) : (
          <div className="af-pop" ref={pop} style={pos} data-af-popover onClick={(e) => e.stopPropagation()}>{list}</div>
        ),
        document.body,
      )}
    </>
  );
}
