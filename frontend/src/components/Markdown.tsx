import { useMemo, type ReactNode } from "react";

/** Component names and requirement IDs in an answer become links that focus them on the canvas. */
export interface EntityLinks {
  components: { id: string; name: string }[];
  onComponent?: (id: string) => void;
  onRequirement?: (id: string) => void;
  /** Requirement id prefixes in use (default ["REQ"]): "<prefix>-n" in text links to the requirement. */
  requirementPrefixes?: string[];
}

interface Linker {
  re: RegExp | null;
  byName: Map<string, string>;
  /** Component ids and names, for `code` spans that name exactly one component. */
  exact: Map<string, string>;
  links?: EntityLinks;
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function useLinker(links?: EntityLinks): Linker {
  return useMemo(() => {
    const byName = new Map<string, string>();
    const exact = new Map<string, string>();
    for (const c of links?.components ?? []) {
      if (c.name.trim().length >= 3) byName.set(c.name, c.id);
      exact.set(c.id, c.id);
      exact.set(c.name, c.id);
    }
    const names = [...byName.keys()].sort((a, b) => b.length - a.length).map(escapeRe);
    const prefixes = links?.requirementPrefixes?.length ? links.requirementPrefixes : ["REQ"];
    const alts = [`(?:${prefixes.map(escapeRe).join("|")})-\\d+`, ...names];
    const re = links ? new RegExp(String.raw`(?<![\w-])(${alts.join("|")})(?![\w-])`, "g") : null;
    return { re, byName, exact, links };
  }, [links]);
}

function linkEntities(text: string, linker: Linker, key: string): ReactNode[] {
  const { re, byName, links } = linker;
  if (!re || !links) return [text];
  const out: ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(re)) {
    const hit = m[1];
    const at = m.index ?? 0;
    if (at > last) out.push(text.slice(last, at));
    const id = byName.get(hit);
    if (id && links.onComponent) {
      out.push(<button key={`${key}-${at}`} type="button" className="ent" title="Show on canvas"
        onClick={() => links.onComponent!(id)}>{hit}</button>);
    } else if (!id && links.onRequirement) {
      out.push(<button key={`${key}-${at}`} type="button" className="ent req" title="Trace this requirement"
        onClick={() => links.onRequirement!(hit)}>{hit}</button>);
    } else {
      out.push(hit);
    }
    last = at + hit.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[[^\]\n]+\]\(https?:\/\/[^)\s]+\))|(\*[^*\s][^*\n]*\*|_[^_\s][^_\n]*_)/g;

function inline(text: string, linker: Linker, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push(...linkEntities(text.slice(last, at), linker, `${key}t${i}`));
    const tok = m[0];
    const k = `${key}i${i++}`;
    if (m[1]) {
      const code = tok.slice(1, -1);
      const id = linker.exact.get(code);
      out.push(id && linker.links?.onComponent
        ? <button key={k} type="button" className="ent" title="Show on canvas" onClick={() => linker.links!.onComponent!(id)}><code>{code}</code></button>
        : <code key={k}>{code}</code>);
    }
    else if (m[2]) out.push(<strong key={k}>{inline(tok.slice(2, -2), linker, k)}</strong>);
    else if (m[3]) {
      const label = tok.slice(1, tok.indexOf("]("));
      const href = tok.slice(tok.indexOf("](") + 2, -1);
      out.push(<a key={k} href={href} target="_blank" rel="noopener noreferrer">{label}</a>);
    } else out.push(<em key={k}>{inline(tok.slice(1, -1), linker, k)}</em>);
    last = at + tok.length;
  }
  if (last < text.length) out.push(...linkEntities(text.slice(last), linker, `${key}e`));
  return out;
}

const BULLET = /^(\s*)[-*+]\s+(.*)$/;
const NUMBERED = /^(\s*)\d+[.)]\s+(.*)$/;
const HEADING = /^(#{1,4})\s+(.*)$/;
const TABLE_RULE = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

const cells = (line: string) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

/** Renders the Markdown the assistant writes (paragraphs, lists, headings, code, tables, quotes). No raw HTML. */
export function Markdown({ text, links, className }: { text: string; links?: EntityLinks; className?: string }) {
  const linker = useLinker(links);
  const lines = text.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let i = 0;
  const isStart = (l: string) => /^```/.test(l) || HEADING.test(l) || BULLET.test(l) || NUMBERED.test(l) || /^>/.test(l);
  while (i < lines.length) {
    const line = lines[i];
    const k = `b${i}`;
    if (!line.trim()) { i++; continue; }
    if (/^```/.test(line)) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) body.push(lines[i++]);
      i++;
      blocks.push(<pre key={k}><code>{body.join("\n")}</code></pre>);
      continue;
    }
    const h = HEADING.exec(line);
    if (h) {
      blocks.push(<div key={k} className={`md-h md-h${h[1].length}`}>{inline(h[2], linker, k)}</div>);
      i++;
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1])) {
      const head = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
      blocks.push(
        <div key={k} className="md-table"><table>
          <thead><tr>{head.map((c, j) => <th key={j}>{inline(c, linker, `${k}h${j}`)}</th>)}</tr></thead>
          <tbody>{rows.map((r, ri) => <tr key={ri}>{r.map((c, j) => <td key={j}>{inline(c, linker, `${k}r${ri}c${j}`)}</td>)}</tr>)}</tbody>
        </table></div>);
      continue;
    }
    const listRe = BULLET.test(line) ? BULLET : NUMBERED.test(line) ? NUMBERED : null;
    if (listRe) {
      const items: { level: number; text: string }[] = [];
      while (i < lines.length) {
        const m = listRe.exec(lines[i]);
        if (m) { items.push({ level: Math.min(3, Math.floor(m[1].replace(/\t/g, "  ").length / 2)), text: m[2] }); i++; }
        else if (lines[i].trim() && /^\s{2,}\S/.test(lines[i]) && items.length) { items[items.length - 1].text += " " + lines[i].trim(); i++; }
        else break;
      }
      const Tag = listRe === BULLET ? "ul" : "ol";
      blocks.push(<Tag key={k}>{items.map((it, j) => <li key={j} style={it.level ? { marginLeft: it.level * 14 } : undefined}>
        {inline(it.text, linker, `${k}l${j}`)}</li>)}</Tag>);
      continue;
    }
    if (/^>/.test(line)) {
      const body: string[] = [];
      while (i < lines.length && /^>/.test(lines[i])) body.push(lines[i++].replace(/^>\s?/, ""));
      blocks.push(<blockquote key={k}>{inline(body.join(" "), linker, k)}</blockquote>);
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !isStart(lines[i])
      && !(lines[i].includes("|") && i + 1 < lines.length && TABLE_RULE.test(lines[i + 1]))) para.push(lines[i++]);
    blocks.push(<p key={k}>{para.flatMap((l, j): ReactNode[] => [...(j ? [<br key={`${k}br${j}`} />] : []), ...inline(l, linker, `${k}p${j}`)])}</p>);
  }
  return <div className={`md ${className ?? ""}`}>{blocks}</div>;
}
