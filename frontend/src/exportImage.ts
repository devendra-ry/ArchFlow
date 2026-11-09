import { getNodesBounds, getViewportForBounds, type Node } from "@xyflow/react";

const PADDING = 48;
/** The computed styles that change how the canvas looks. html-to-image otherwise copies ~300 properties onto every
 * element: 1.5 MB of a 2 MB SVG for a 10-component diagram. */
const VISUAL_STYLES = [
  "display", "position", "top", "right", "bottom", "left", "z-index", "box-sizing", "overflow", "visibility", "opacity",
  "width", "height", "min-width", "min-height", "max-width", "max-height",
  "margin-top", "margin-right", "margin-bottom", "margin-left", "padding-top", "padding-right", "padding-bottom", "padding-left",
  "border-top-width", "border-right-width", "border-bottom-width", "border-left-width",
  "border-top-style", "border-right-style", "border-bottom-style", "border-left-style",
  "border-top-color", "border-right-color", "border-bottom-color", "border-left-color",
  "border-top-left-radius", "border-top-right-radius", "border-bottom-right-radius", "border-bottom-left-radius",
  "background-color", "background-image", "background-size", "background-position", "box-shadow",
  "color", "font-family", "font-size", "font-weight", "font-style", "line-height", "letter-spacing", "text-transform",
  "text-align", "text-decoration-line", "text-overflow", "white-space", "word-break", "overflow-wrap", "vertical-align",
  "-webkit-line-clamp", "-webkit-box-orient", "transform", "transform-origin",
  "flex-direction", "flex-wrap", "flex-grow", "flex-shrink", "flex-basis", "align-items", "align-self", "justify-content",
  "gap", "row-gap", "column-gap", "grid-template-columns", "grid-template-rows",
  "fill", "fill-opacity", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin", "stroke-opacity",
  "content", // ::before / ::after text, e.g. the "·" between a component's type and technology
];
const MAX_SIDE = 6000;
/** Paint properties of the SVG inside the canvas (connections, their labels, arrowheads). html-to-image clones an
 * <svg> as it is and never copies styles onto its children, and the app's stylesheet is not part of the export, so
 * without these inline styles every connection came out invisible and every label background solid black. */
const SVG_PAINT = ["fill", "fill-opacity", "stroke", "stroke-width", "stroke-dasharray", "stroke-dashoffset", "stroke-linecap",
  "stroke-linejoin", "stroke-opacity", "opacity", "visibility", "font-family", "font-size", "font-weight", "letter-spacing"];

/**
 * Save the visible diagram level as a PNG or SVG image. The React Flow viewport is redrawn off-screen so the
 * whole diagram fits (not just what is on screen); live cursors are left out. PNGs are drawn at 2x for sharpness.
 */
export async function exportCanvasImage(nodes: Node[], format: "png" | "svg", filename: string): Promise<void> {
  const viewport = document.querySelector<HTMLElement>(".react-flow__viewport");
  if (!viewport || nodes.length === 0) throw new Error("There is nothing on the canvas to export.");
  const { toBlob, toSvg, getFontEmbedCSS } = await import("html-to-image");
  const bounds = getNodesBounds(nodes);
  const width = Math.min(MAX_SIDE, Math.ceil(bounds.width + PADDING * 2));
  const height = Math.min(MAX_SIDE, Math.ceil(bounds.height + PADDING * 2));
  const { x, y, zoom } = getViewportForBounds(bounds, width, height, 0.2, 2, 0.04);
  const background = getComputedStyle(document.querySelector(".canvas") ?? document.body).backgroundColor;
  const options = {
    backgroundColor: background, width, height,
    style: { width: `${width}px`, height: `${height}px`, transform: `translate(${x}px, ${y}px) scale(${zoom})` },
    filter: (el: HTMLElement) => !el.classList?.contains("remote-cursor") && !el.classList?.contains("react-flow__handle"),
    includeStyleProperties: VISUAL_STYLES,
    // Only the Latin font subsets the labels use, not all 11 Geist files. With the style list above this keeps a
    // 10-component SVG far below the ~2 MB at which Chromium on Windows cancelled the download.
    fontEmbedCSS: latinFontsOnly(await getFontEmbedCSS(viewport)),
  };
  const restore = inlineSvgPaint(viewport);
  let blob: Blob | null;
  try {
    blob = format === "png"
      ? await toBlob(viewport, { ...options, pixelRatio: 2 })
      : svgWithBackground(await (await fetch(await toSvg(viewport, { ...options, backgroundColor: undefined }))).text(), background);
  } finally {
    restore();
  }
  if (!blob) throw new Error("Could not draw the diagram.");
  saveBlob(blob, `${filename}.${format}`);
}

/** The SVG with a full-size background. html-to-image paints ``backgroundColor`` on the exported root, which also
 * carries the framing transform, so in an SVG the background came out as a shifted panel over part of the diagram. */
function svgWithBackground(svg: string, color: string): Blob {
  const withRect = svg.replace(/^(<svg[^>]*>)/, `$1<rect width="100%" height="100%" fill="${color}"/>`);
  return new Blob([withRect], { type: "image/svg+xml" });
}

/** Copy the computed paint of every SVG element under ``root`` into its inline style; returns an undo. */
function inlineSvgPaint(root: HTMLElement): () => void {
  const saved: [Element, string | null][] = [];
  root.querySelectorAll<SVGElement>("svg *").forEach((el) => {
    const computed = getComputedStyle(el);
    saved.push([el, el.getAttribute("style")]);
    for (const prop of SVG_PAINT) el.style.setProperty(prop, computed.getPropertyValue(prop));
  });
  return () => saved.forEach(([el, style]) => (style === null ? el.removeAttribute("style") : el.setAttribute("style", style)));
}

/** Keep the @font-face rules that cover basic Latin (or declare no unicode-range): the diagram's labels. */
function latinFontsOnly(css: string): string {
  return (css.match(/@font-face\s*{[^}]*}/g) ?? [])
    .filter((rule) => !/unicode-range/i.test(rule) || /U\+0+-0*FF\b/i.test(rule))
    .join("\n");
}

/** Download a blob through an object URL on a link in the document. A 2x PNG as a data: URL runs to megabytes,
 * which browsers may refuse as a download (Chromium cancelled it); Firefox only follows a link that is attached. */
function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

export function exportName(...parts: string[]): string {
  return parts.filter(Boolean).join("-").replace(/[^A-Za-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 80) || "diagram";
}
