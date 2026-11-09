import { getNodesBounds, getViewportForBounds, type Node } from "@xyflow/react";

const PADDING = 48;
const MAX_SIDE = 6000;

/**
 * Save the visible diagram level as a PNG or SVG image. The React Flow viewport is redrawn off-screen so the
 * whole diagram fits (not just what is on screen); live cursors are left out. PNGs are drawn at 2x for sharpness.
 */
export async function exportCanvasImage(nodes: Node[], format: "png" | "svg", filename: string): Promise<void> {
  const viewport = document.querySelector<HTMLElement>(".react-flow__viewport");
  if (!viewport || nodes.length === 0) throw new Error("There is nothing on the canvas to export.");
  const { toBlob, toSvg } = await import("html-to-image");
  const bounds = getNodesBounds(nodes);
  const width = Math.min(MAX_SIDE, Math.ceil(bounds.width + PADDING * 2));
  const height = Math.min(MAX_SIDE, Math.ceil(bounds.height + PADDING * 2));
  const { x, y, zoom } = getViewportForBounds(bounds, width, height, 0.2, 2, 0.04);
  const background = getComputedStyle(document.querySelector(".canvas") ?? document.body).backgroundColor;
  const options = {
    backgroundColor: background, width, height,
    style: { width: `${width}px`, height: `${height}px`, transform: `translate(${x}px, ${y}px) scale(${zoom})` },
    filter: (el: HTMLElement) => !el.classList?.contains("remote-cursor"),
  };
  const blob = format === "png"
    ? await toBlob(viewport, { ...options, pixelRatio: 2 })
    : await (await fetch(await toSvg(viewport, options))).blob();
  if (!blob) throw new Error("Could not draw the diagram.");
  saveBlob(blob, `${filename}.${format}`);
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
