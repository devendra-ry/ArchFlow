import { useEffect, useState } from "react";
import { Download, Icon } from "../icons";
import { api } from "../api";
import type { ComponentViews } from "../types";
import { MermaidDiagram } from "./MermaidDiagram";

type ViewTab = "api" | "data" | "sequence";

interface Props { pid: string; branch: string; nodeId: string; rev: number }

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button className="small" disabled={!text} onClick={() => {
      navigator.clipboard?.writeText(text).then(() => { setDone(true); window.setTimeout(() => setDone(false), 1200); }).catch(() => {});
    }}>{done ? "Copied" : "Copy"}</button>
  );
}

/** Read-only views derived from a component's design: API list, data model and the main request flow. */
export function LldViews({ pid, branch, nodeId, rev }: Props) {
  const [tab, setTab] = useState<ViewTab>("api");
  const [views, setViews] = useState<ComponentViews | null>(null);
  const [error, setError] = useState("");
  const [exporting, setExporting] = useState(false);

  useEffect(() => { setViews(null); }, [nodeId]);
  useEffect(() => {
    // The API's copy of the live document trails it slightly, so refresh shortly after edits.
    const t = window.setTimeout(() => {
      api.views(pid, branch, nodeId).then((v) => { setViews(v); setError(""); }).catch((e) => setError((e as Error).message));
    }, views ? 1500 : 0);
    return () => window.clearTimeout(t);
  }, [pid, branch, nodeId, rev]); // eslint-disable-line react-hooks/exhaustive-deps

  const source = !views ? "" : tab === "api" ? views.api.openapi : tab === "data" ? views.er.mermaid : views.sequence.mermaid;
  return (
    <div className="field lld-views">
      <span>Views</span>
      <div className="view-tabs" role="tablist">
        {([["api", "API"], ["data", "Data model"], ["sequence", "Sequence"]] as [ViewTab, string][]).map(([k, label]) => (
          <button key={k} role="tab" aria-selected={tab === k} className={`vchip ${tab === k ? "on" : ""}`} onClick={() => setTab(k)}>{label}</button>
        ))}
        <span className="spacer" />
        <CopyButton text={source} />
      </div>
      {error && <p className="error small">{error}</p>}
      {!views && !error && <p className="muted small">Loading…</p>}
      {views && tab === "api" && (views.api.endpoints.length ? (
        <ul className="view-endpoints">
          {views.api.endpoints.map((e) => (
            <li key={e.id} title={e.description}>
              <span className={`http-m m-${e.method.toLowerCase()}`}>{e.method}</span> <span className="mono">{e.path}</span>
              {e.description && <div className="muted tiny">{e.description}</div>}
            </li>
          ))}
        </ul>
      ) : <p className="muted small">No endpoints yet. Open the internals and generate a low-level design.</p>)}
      {views && tab === "data" && !views.er.mermaid && <p className="muted small">No tables yet.</p>}
      {views && source && tab !== "api" && (
        <>
          <MermaidDiagram source={source} label={tab === "data" ? `Data model of ${views.name}` : `Request flow of ${views.name}`} />
          {tab === "sequence" && (
            <div className="row gap">
              <button className="small" disabled={exporting} title="Save an interactive diagram you can share (HTML)"
                onClick={async () => {
                  setExporting(true);
                  setError("");
                  try { await api.exportSequence(pid, branch, nodeId); }
                  catch (e) { setError((e as Error).message); }
                  finally { setExporting(false); }
                }}>{exporting ? "Exporting…" : <><Icon as={Download} size={14} /> Interactive diagram</>}</button>
            </div>
          )}
          <details className="view-source">
            <summary className="muted tiny">Mermaid source (paste into any Mermaid renderer or Markdown)</summary>
            <pre className="view-src mono">{source}</pre>
          </details>
        </>
      )}
    </div>
  );
}
