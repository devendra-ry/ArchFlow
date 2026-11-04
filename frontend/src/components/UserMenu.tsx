import { useEffect, useRef, useState } from "react";
import type { Me, Role, User } from "../types";
import { onMenuKeyDown } from "./Select";

interface Props {
  me: Me;
  user: User;
  colors?: string[];
  onColor?: (c: string) => void;
  onLogout: () => void;
  role?: Role | null; // role in the current project's org, if any
}

/** Avatar button with account details, cursor colour and sign out. */
export function UserMenu({ me, user, colors, onColor, onLogout, role }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);

  return (
    <div className="user-menu" ref={ref}>
      <button className="avatar-btn" onClick={() => setOpen(!open)} aria-haspopup="menu" aria-expanded={open}
        title={`${me.user.name} (${me.user.email})`}>
        <span className="avatar" style={{ background: user.color }}>{me.user.name[0]?.toUpperCase()}</span>
      </button>
      {open && (
        <div className="user-menu-pop af-menu" role="menu" onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); else onMenuKeyDown(e); }}>
          <div><b>{me.user.name}</b></div>
          <div className="muted small">{me.user.email}</div>
          {role && <div className="small">Role here: <b>{role}</b></div>}
          {colors && onColor && (
            <div className="field">
              <span>Cursor colour</span>
              <div className="swatches">
                {colors.map((c) => (
                  <button key={c} aria-label={`colour ${c}`} className={`swatch ${user.color === c ? "on" : ""}`}
                    style={{ background: c }} onClick={() => onColor(c)} />
                ))}
              </div>
            </div>
          )}
          <button role="menuitem" onClick={onLogout}>Sign out</button>
        </div>
      )}
    </div>
  );
}
