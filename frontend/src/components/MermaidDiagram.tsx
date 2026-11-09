import { useEffect, useId, useState } from "react";

type MermaidApi = typeof import("mermaid").default;
let loading: Promise<MermaidApi> | null = null;

/** Mermaid is large, so it is loaded the first time a diagram is shown. */
function loadMermaid(): Promise<MermaidApi> {
  loading ??= import("mermaid").then((m) => m.default);
  return loading;
}

function prefersDark(): boolean {
  const theme = document.documentElement.dataset.theme;
  return theme ? theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
}

const attr = (text: string) => text.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/** Draws Mermaid source (diagrams from the model and the low-level design views). The label becomes the SVG's own
 * alt text (aria-label), so it travels with a downloaded copy; ``onRender`` receives that SVG. */
export function MermaidDiagram({ source, label, onRender }: { source: string; label: string; onRender?: (svg: string) => void }) {
  const id = `mmd-${useId().replace(/[^a-zA-Z0-9]/g, "")}`;
  const [svg, setSvg] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setError("");
    loadMermaid().then(async (mermaid) => {
      // "strict" escapes labels and disables click handlers, so names from the model cannot inject markup.
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: prefersDark() ? "dark" : "neutral",
        fontFamily: "Geist Variable, system-ui, sans-serif" });
      const { svg: drawn } = await mermaid.render(id, source);
      // Mermaid already sets role (and <title>/<desc> from accTitle/accDescr); add the alt text as aria-label.
      const out = /^<svg [^>]*aria-label=/.test(drawn) ? drawn : drawn.replace(/^<svg /, `<svg aria-label="${attr(label)}" `);
      if (!cancelled) { setSvg(out); onRender?.(out); }
    }).catch((e) => { if (!cancelled) { setSvg(""); onRender?.(""); setError((e as Error).message || "Could not draw this diagram."); } });
    return () => { cancelled = true; };
  }, [id, source, label]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error) return <p className="error small">Could not draw the diagram: {error}</p>;
  if (!svg) return <p className="muted small pulse">Drawing…</p>;
  return <div className="mermaid-view" role="img" aria-label={label} dangerouslySetInnerHTML={{ __html: svg }} />;
}
