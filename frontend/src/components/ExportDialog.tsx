import { useEffect, useState } from "react";
import { Icon, TriangleAlert, X } from "../icons";
import { api } from "../api";
import { useEscape } from "../useEscape";
import { Copy } from "./ConnectDialog";

interface Props {
  pid: string;
  branch: string;
  aiAvailable: boolean;
  toast: (msg: string) => void;
  onClose: () => void;
}

const GUIDES = ["CLAUDE.md", "AGENTS.md", ".cursorrules"] as const;
type Guide = (typeof GUIDES)[number];

function saveText(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: name.endsWith(".md") ? "text/markdown" : "text/plain" }));
  Object.assign(document.createElement("a"), { href: url, download: name }).click();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** Spec exports (Markdown, PDF via print, JSON, YAML) and coding-agent guide files with a preview before download. */
export function ExportDialog({ pid, branch, aiAvailable, toast, onClose }: Props) {
  const escRef = useEscape(onClose);
  const [busy, setBusy] = useState("");
  const [useAi, setUseAi] = useState(aiAvailable);
  const [guides, setGuides] = useState<{ files: Record<string, string>; engine: string; notes: string[] } | null>(null);
  const [tab, setTab] = useState<Guide>("CLAUDE.md");
  const [error, setError] = useState("");

  const loadGuides = async (ai: boolean) => {
    setBusy("guides");
    setError("");
    try { setGuides(await api.exportGuides(pid, branch, ai)); }
    catch (err) { setError((err as Error).message); }
    finally { setBusy(""); }
  };
  useEffect(() => { loadGuides(false); }, [pid, branch]); // eslint-disable-line react-hooks/exhaustive-deps

  const spec = async (format: "spec.md" | "project.json" | "project.yaml") => {
    setBusy(format);
    try { toast(`Saved ${await api.exportSpec(pid, branch, format)}`); }
    catch (err) { toast(`⚠ ${(err as Error).message}`); }
    finally { setBusy(""); }
  };
  const printPdf = async () => {
    const win = window.open("", "_blank"); // opened now, so the popup blocker allows it
    setBusy("pdf");
    try {
      const html = await api.exportPrintHtml(pid, branch);
      const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
      if (win) win.location.href = url;
      else Object.assign(document.createElement("a"), { href: url, download: "spec.html" }).click();
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (err) { win?.close(); toast(`⚠ ${(err as Error).message}`); }
    finally { setBusy(""); }
  };

  const text = guides?.files[tab] ?? "";
  return (
    <div className="modal-backdrop" onClick={onClose} ref={escRef}>
      <div className="modal wide" role="dialog" aria-label="Export specification" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Export spec &amp; agent guides</h3>
          <button className="ghost" onClick={onClose} aria-label="Close"><Icon as={X} size={16} /></button>
        </div>
        <p className="muted small">Everything is built from branch <b>{branch}</b>: requirements, components, low-level designs,
          decisions, project memory and health.</p>
        <div className="row gap wrap">
          <button className="small" disabled={!!busy} onClick={() => spec("spec.md")}>{busy === "spec.md" ? "Exporting…" : "Markdown spec"}</button>
          <button className="small" disabled={!!busy} onClick={printPdf}
            title="Opens a print-ready page; choose Save as PDF in the print dialog">{busy === "pdf" ? "Opening…" : "PDF (print)"}</button>
          <button className="small" disabled={!!busy} onClick={() => spec("project.json")}>{busy === "project.json" ? "Exporting…" : "Project JSON"}</button>
          <button className="small" disabled={!!busy} onClick={() => spec("project.yaml")}>{busy === "project.yaml" ? "Exporting…" : "Project YAML"}</button>
        </div>

        <div className="tools-sep" />
        <div className="row between wrap">
          <b className="small">Coding-agent guides</b>
          <div className="row gap">
            <label className="small" title={aiAvailable ? "" : "No AI model configured: the files hold the derived skeleton"}>
              <input type="checkbox" checked={useAi} disabled={!aiAvailable || !!busy} onChange={(e) => setUseAi(e.target.checked)} />
              {" "}AI project guidance
            </label>
            <button className="small" disabled={!!busy} onClick={() => loadGuides(useAi)}>
              {busy === "guides" ? "Writing…" : guides?.engine === "llm" ? "Regenerate" : "Generate"}</button>
          </div>
        </div>
        <nav className="tabs inline">
          {GUIDES.map((g) => <button key={g} className={tab === g ? "on" : ""} onClick={() => setTab(g)}>{g}</button>)}
        </nav>
        {error && <p className="small" role="alert"><Icon as={TriangleAlert} size={14} /> {error}</p>}
        {guides?.notes.map((n) => <p key={n} className="muted tiny">{n}</p>)}
        <div className="snippet" style={{ maxHeight: "45vh", overflow: "auto" }}>
          <pre aria-label={`${tab} preview`}>{guides ? text : "Loading…"}</pre>
        </div>
        <div className="row gap">
          <button className="primary small" disabled={!guides} onClick={() => saveText(tab, text)}>Download {tab}</button>
          <button className="small" disabled={!guides} onClick={() => GUIDES.forEach((g) => saveText(g, guides?.files[g] ?? ""))}>
            Download all three</button>
          {guides && <Copy text={text} />}
        </div>
      </div>
    </div>
  );
}
