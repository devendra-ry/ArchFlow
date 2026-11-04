import { useEffect, useRef, useState } from "react";
import type { User } from "./types";

interface Handlers {
  onVersions: () => void;
  onActivity: () => void;
  /** A conversation got new messages (yours or a teammate's). */
  onConversation?: (id: string, actor: string) => void;
  /** Project memory changed; ``added`` lists facts the AI just learned. */
  onMemory?: (actor: string, added: string[]) => void;
  /** Someone added, edited, resolved or deleted a comment. */
  onComments?: (actor: string) => void;
  /** Jira links or issue status changed (a push, a sync, an import, or a Jira webhook). */
  onJira?: (actor: string) => void;
}

/**
 * Server events per (project, branch): AI status, new versions, activity, conversations, project memory and comments.
 * Canvas content, presence, cursors and selections sync through Yjs (see collab.ts).
 */
export function useRealtime(pid: string, branch: string, user: User, handlers: Handlers) {
  const [aiStatus, setAiStatus] = useState<{ status: string; actor?: string; text?: string }>({ status: "idle" });
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    let closed = false;
    let retry: number | undefined;
    let ws: WebSocket | null = null;

    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const qs = new URLSearchParams({ color: user.color }); // identity comes from the session cookie
      ws = new WebSocket(`${proto}://${location.host}/ws/${pid}/${encodeURIComponent(branch)}?${qs}`);
      ws.onclose = () => { if (!closed) retry = window.setTimeout(connect, 1500); };
      ws.onmessage = (ev) => {
        const msg = JSON.parse(ev.data);
        if (msg.type === "state" || msg.type === "activity") handlersRef.current.onActivity();
        else if (msg.type === "versions") handlersRef.current.onVersions();
        else if (msg.type === "ai_status") setAiStatus(msg);
        else if (msg.type === "conversation") handlersRef.current.onConversation?.(msg.id, msg.actor ?? "");
        else if (msg.type === "memory") handlersRef.current.onMemory?.(msg.actor ?? "", msg.added ?? []);
        else if (msg.type === "comments") handlersRef.current.onComments?.(msg.actor ?? "");
        else if (msg.type === "jira") handlersRef.current.onJira?.(msg.actor ?? "");
      };
    };
    connect();
    return () => {
      closed = true;
      window.clearTimeout(retry);
      ws?.close();
    };
  }, [pid, branch, user.name, user.color]);

  return { aiStatus };
}
