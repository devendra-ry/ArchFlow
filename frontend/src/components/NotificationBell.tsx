import { useCallback, useEffect, useRef, useState } from "react";
import { Bell, Icon } from "../icons";
import { api } from "../api";
import type { AppNotification } from "../types";
import { onMenuKeyDown } from "./Select";

// Changes arrive live over /ws/notifications; the poll is only a fallback (e.g. while the socket reconnects).
const POLL_MS = 120_000;
const VERB: Record<AppNotification["kind"], string> = {
  mention: "mentioned you", reply: "replied", review_requested: "asked you to review", approved: "approved",
  changes_requested: "requested changes to",
};

/** Your mentions and replies across projects. Refreshes on a timer and whenever ``refreshKey`` changes. */
export function NotificationBell({ refreshKey, onOpen }: { refreshKey: number; onOpen: (n: AppNotification) => void }) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<AppNotification[]>([]);
  const [unread, setUnread] = useState(0);
  const ref = useRef<HTMLDivElement>(null);

  const load = useCallback(() => {
    api.notifications().then((r) => { setItems(r.items); setUnread(r.unread); }).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load, refreshKey]);
  useEffect(() => {
    const t = window.setInterval(load, POLL_MS);
    return () => window.clearInterval(t);
  }, [load]);
  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry = 0;
    let timer = 0;
    let closed = false;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/notifications`);
      ws.onopen = () => { retry = 0; load(); };
      ws.onmessage = (e) => { try { if (JSON.parse(e.data).type === "notifications") load(); } catch { /* ignore */ } };
      ws.onclose = () => {
        if (closed) return;
        timer = window.setTimeout(connect, Math.min(30_000, 1000 * 2 ** retry++));
      };
    };
    connect();
    return () => { closed = true; window.clearTimeout(timer); ws?.close(); };
  }, [load]);
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false); };
    window.addEventListener("mousedown", close);
    return () => window.removeEventListener("mousedown", close);
  }, [open]);

  const markAll = () => api.readNotifications().then(() => { setUnread(0); setItems((xs) => xs.map((x) => ({ ...x, read: true }))); }).catch(() => {});
  const choose = (n: AppNotification) => {
    setOpen(false);
    if (!n.read) api.readNotifications([n.id]).then((r) => setUnread(r.unread)).catch(() => {});
    setItems((xs) => xs.map((x) => (x.id === n.id ? { ...x, read: true } : x)));
    onOpen(n);
  };

  return (
    <div className="notif" ref={ref}>
      <button className="ghost notif-btn" onClick={() => { setOpen(!open); if (!open) load(); }} aria-haspopup="menu" aria-expanded={open}
        aria-label={unread ? `Notifications, ${unread} unread` : "Notifications"} title="Mentions, replies and reviews">
        <Icon as={Bell} size={16} />
        {unread > 0 && <span className="notif-count">{unread > 99 ? "99+" : unread}</span>}
      </button>
      {open && (
        <div className="notif-pop af-menu" role="menu" onKeyDown={(e) => { if (e.key === "Escape") setOpen(false); else onMenuKeyDown(e); }}>
          <div className="row between">
            <b>Notifications</b>
            {unread > 0 && <button className="small ghost" onClick={markAll}>Mark all read</button>}
          </div>
          {items.length === 0 && <p className="muted small">Nothing yet. You'll see it here when someone mentions you or replies to you.</p>}
          <ul>
            {items.map((n) => (
              <li key={n.id}>
                <button role="menuitem" className={`notif-item ${n.read ? "" : "unread"}`} onClick={() => choose(n)}>
                  <span className="small"><b>{n.actor_name}</b> {VERB[n.kind] ?? n.kind}{n.version != null ? ` v${n.version}` : ""} in <b>{n.project_name}</b></span>
                  <span className="notif-preview small">{n.preview}</span>
                  <span className="muted small">{new Date(n.created_at).toLocaleString()}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
