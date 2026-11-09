/**
 * ArchFlow collaboration server.
 *
 * One Yjs document per project branch (name `<projectId>:<branch>`), synced to every browser over
 * WebSocket by Hocuspocus. Presence, cursors and selections travel as Yjs awareness.
 *
 *  - Load:   binary Yjs state from the API; if none yet, seed it from the branch's structured state.
 *  - Store:  debounced — saves the binary state and pushes the plain-JSON projection to the API, which
 *            makes it the state used by AI, MCP, analysis and versioning (attributed to who edited).
 *  - Patch:  POST /internal/docs/:name/patch — the API applies server-side changes (AI, MCP, restore)
 *            as field-level patches, merged into the live document so they never overwrite people's edits.
 *
 * Scaling out: add @hocuspocus/extension-redis so several instances share documents.
 */
import { Server } from "@hocuspocus/server";

import { timingSafeEqual } from "node:crypto";
import type { IncomingMessage, ServerResponse } from "node:http";
import { fileURLToPath } from "node:url";
import * as Y from "yjs";
import { verifyTicket } from "./ticket.js";
import { applyPatch, docToState, ORIGIN_SERVER, stateToPatch, type Patch } from "./ydoc.js";

const PORT = Number(process.env.COLLAB_PORT ?? 1234);
const API = (process.env.ARCHFLOW_API ?? "http://localhost:8000").replace(/\/$/, "");
// Shared settings come from the repo-root .env (git-ignored), unless already set in the environment.
for (const file of [new URL("../../.env", import.meta.url), new URL("../.env", import.meta.url)]) {
  try { process.loadEnvFile(fileURLToPath(file)); } catch { /* file not present */ }
}
const INTERNAL_TOKEN = process.env.ARCHFLOW_INTERNAL_TOKEN ?? "";
if (!INTERNAL_TOKEN) {
  console.error("[collab] ARCHFLOW_INTERNAL_TOKEN is not set. Copy .env.example to .env and set it (same value for the API).");
  process.exit(1);
}

interface Ctx { user: { id?: string; name: string; color: string }; server?: boolean }


const actors = new Map<string, Set<string>>(); // documentName → people who edited since the last store

const internal = { "x-internal-token": INTERNAL_TOKEN };
const INTERNAL_TOKEN_BYTES = Buffer.from(INTERNAL_TOKEN, "utf8");

/** Constant-time check of the shared secret (the patch endpoint is reachable through the public /collab route). */
function internalTokenOk(given: string | string[] | undefined): boolean {
  if (typeof given !== "string") return false;
  const g = Buffer.from(given, "utf8");
  return g.length === INTERNAL_TOKEN_BYTES.length && timingSafeEqual(g, INTERNAL_TOKEN_BYTES);
}
const enc = (name: string) => name.split("/").map(encodeURIComponent).join("/");

async function api(path: string, init: RequestInit = {}) {
  return fetch(`${API}${path}`, { ...init, headers: { ...internal, ...(init.headers ?? {}) } });
}

async function readBody(req: IncomingMessage): Promise<string> {
  const chunks: Buffer[] = [];
  for await (const c of req) chunks.push(c as Buffer);
  return Buffer.concat(chunks).toString("utf8");
}

function send(res: ServerResponse, status: number, body: unknown) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

const server = new Server<Ctx>({
  name: "archflow-collab",
  port: PORT,
  debounce: 1000,
  maxDebounce: 5000,
  quiet: true,

  async onAuthenticate({ token, documentName, connectionConfig }) {
    // The browser presents a short-lived ticket from GET /api/projects/:pid/collab-ticket (signed by the API).
    const claims = verifyTicket(INTERNAL_TOKEN, token, documentName);
    if (claims.ro) connectionConfig.readOnly = true; // viewers and reviewers: document updates are rejected
    return { user: { id: claims.uid, name: String(claims.name).slice(0, 40), color: String(claims.color).slice(0, 20) } };
  },

  async onLoadDocument({ document, documentName }) {
    const r = await api(`/api/internal/ydocs/${enc(documentName)}`);
    if (r.ok) {
      Y.applyUpdate(document, new Uint8Array(await r.arrayBuffer()));
      return;
    }
    // First time this branch is opened collaboratively: seed from the API's structured state.
    const s = await api(`/api/internal/state/${enc(documentName)}`);
    if (!s.ok) throw new Error(`cannot load ${documentName}: ${s.status}`);
    const { state } = await s.json();
    applyPatch(document, stateToPatch(state), ORIGIN_SERVER);
  },

  async onChange({ documentName, context }) {
    const name = context?.user?.name;
    if (!name || context?.server) return;
    if (!actors.has(documentName)) actors.set(documentName, new Set());
    actors.get(documentName)!.add(name);
  },

  async onStoreDocument({ document, documentName }) {
    const who = [...(actors.get(documentName) ?? [])];
    actors.delete(documentName);
    const bin = Y.encodeStateAsUpdate(document);
    const [saveDoc, saveState] = await Promise.all([
      api(`/api/internal/ydocs/${enc(documentName)}`, { method: "PUT", body: Buffer.from(bin), headers: { "Content-Type": "application/octet-stream" } }),
      api(`/api/internal/projection/${enc(documentName)}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ state: docToState(document), actors: who }),
      }),
    ]);
    if (!saveDoc.ok || !saveState.ok) {
      console.error(`[collab] store ${documentName} failed: ydoc ${saveDoc.status}, projection ${saveState.status} ${await saveState.text()}`);
    }
  },

  async onRequest({ request, response, instance }) {
    const url = new URL(request.url ?? "/", "http://localhost");
    if (url.pathname === "/health") {
      send(response, 200, { ok: true, documents: instance.getDocumentsCount(), connections: instance.getConnectionsCount() });
      throw null; // handled — stop Hocuspocus' default response
    }
    const m = /^\/internal\/docs\/(.+)\/patch$/.exec(url.pathname);
    if (!m || request.method !== "POST") return;
    if (!internalTokenOk(request.headers["x-internal-token"])) {
      send(response, 401, { error: "invalid internal token" });
      throw null;
    }
    try {
      const name = decodeURIComponent(m[1]);
      const { patch, actor } = JSON.parse(await readBody(request)) as { patch: Patch; actor?: string };
      const conn = await instance.openDirectConnection(name, { user: { name: actor ?? "server", color: "#888" }, server: true });
      await conn.transact((doc) => applyPatch(doc, patch, ORIGIN_SERVER));
      await conn.disconnect();
      send(response, 200, { ok: true });
    } catch (err) {
      console.error("[collab] patch failed", err);
      send(response, 500, { error: String(err) });
    }
    throw null;
  },
});

server.listen().then(() => console.log(`[collab] listening on :${PORT}, API ${API}`));
