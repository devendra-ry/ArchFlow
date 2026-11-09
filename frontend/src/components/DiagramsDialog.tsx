import { useEffect, useState } from "react";
import { Download, Icon, X } from "../icons";
import { api } from "../api";
import { useEscape } from "../useEscape";
import type { Diagram, DiagramInfo } from "../types";
import { Copy } from "./ConnectDialog";
import { MermaidDiagram } from "./MermaidDiagram";

interface Props { pid: string; branch: string; projectName: string; aiAvailable: boolean; rev: number; onClose: () => void }

const KIND_LABEL: Record<DiagramInfo["kind"], string> = {
  hld: "Architecture", "c4-context": "C4 context", "c4-container": "C4 containers", sequence: "Sequence",
  er: "Data model (ER)", deployment: "Deployment", dataflow: "Data flow",
};

function saveSvg(svg: string, name: string) {
  const url = URL.createObjectURL(new Blob([svg], { type: "image/svg+xml" }));
  Object.assign(document.createElement("a"), { href: url, download: `${name}.svg` }).click();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** More diagrams from the same model: C4 context and containers, a sequence diagram per critical flow, the data model
 * across all components, deployment and data flow with trust boundaries; each with Mermaid / PlantUML text and alt text. */
export function DiagramsDialog({ pid, branch, projectName, aiAvailable, rev, onClose }: Props) {
  const escRef = useEscape(onClose);
  const [list, setList] = useState<DiagramInfo[] | null>(null);
  const [selected, setSelected] = useState("hld");
  const [diagram, setDiagram] = useState<Diagram | null>(null);
  const [svg, setSvg] = useState("");
  const [ai, setAi] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setError("");
    api.diagrams(pid, branch, ai).then((r) => {
      if (cancelled) return;
      setList(r.diagrams);
      if (!r.diagrams.some((d) => d.id === selected)) setSelected(r.diagrams[0]?.id ?? "hld");
    }).catch((e) => { if (!cancelled) setError((e as Error).message); });
    return () => { cancelled = true; };
  }, [pid, branch, ai, rev]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let cancelled = false;
    setSvg("");
    api.diagram(pid, branch, selected, ai).then((d) => { if (!cancelled) { setDiagram(d); setError(""); } })
      .catch((e) => { if (!cancelled) { setDiagram(null); setError((e as Error).message); } });
    return () => { cancelled = true; };
  }, [pid, branch, selected, ai, rev]);

  const fileName = `${projectName}-${branch}-${selected}`.replace(/[^A-Za-z0-9._-]+/g, "-");
  const shown = diagram?.id === selected ? diagram : null;

  return (
    <div className="modal-backdrop" onClick={onClose} ref={escRef}>
      <div className="modal wide diagrams" role="dialog" aria-label="Diagrams" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Diagrams</h3>
          <button className="ghost" onClick={onClose} aria-label="Close"><Icon as={X} size={16} /></button>
        </div>
        <p className="muted small">
          Drawn from this branch's model, so they always match the canvas. Copy the Mermaid or PlantUML source into docs,
          wikis or Markdown, or save the picture as SVG.
        </p>
        <div className="diagrams-body">
          <nav className="diagrams-list" aria-label="Diagram types">
            {!list && !error && <p className="muted small">Loading…</p>}
            {list?.map((d) => (
              <button key={d.id} className={`diagrams-item ${d.id === selected ? "on" : ""}`} aria-current={d.id === selected}
                onClick={() => setSelected(d.id)} title={d.alt}>
                <b>{d.kind === "sequence" && d.flow ? d.flow : KIND_LABEL[d.kind]}</b>
                <span className="muted tiny">{d.kind === "sequence" ? (d.path?.join(" → ") || "No flows yet") : d.empty ? "Nothing to draw yet" : KIND_LABEL[d.kind]}</span>
              </button>
            ))}
            {aiAvailable && (
              <label className="row gap small diagrams-ai" title="Let the AI name the critical flows after your requirements and pick their paths">
                <input type="checkbox" checked={ai} onChange={(e) => setAi(e.target.checked)} /> AI names the flows
              </label>
            )}
          </nav>
          <section className="diagrams-view">
            {error && <p className="error small">{error}</p>}
            {!shown && !error && <p className="muted small pulse">Loading…</p>}
            {shown && (
              <>
                <div className="row between gap">
                  <h4>{shown.title}</h4>
                  <div className="row gap">
                    <Copy text={shown.mermaid} label="Copy Mermaid" />
                    <Copy text={shown.plantuml} label="Copy PlantUML" />
                    <button className="small" disabled={!svg} onClick={() => saveSvg(svg, fileName)}><Icon as={Download} size={14} /> SVG</button>
                  </div>
                </div>
                <p className="small diagrams-alt"><span className="muted">Alt text: </span>{shown.alt}</p>
                {shown.empty
                  ? <p className="muted small">Nothing to draw yet.</p>
                  : <MermaidDiagram source={shown.mermaid} label={shown.alt} onRender={setSvg} />}
                {!shown.empty && (
                  <details className="view-source">
                    <summary className="muted tiny">Sources</summary>
                    <pre className="view-src mono">{shown.mermaid}</pre>
                    <pre className="view-src mono">{shown.plantuml}</pre>
                  </details>
                )}
              </>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
