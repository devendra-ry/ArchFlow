import { useCallback, useEffect, useMemo, useState } from "react";
import { GitBranch, Icon, Sparkles, TriangleAlert, X } from "../icons";
import { api, ApiError } from "../api";
import { Select } from "./Select";
import type { Branch, MergeChoice, MergeConflict, MergePreview, MergeResult, MergeSuggestion } from "../types";

interface Props {
  pid: string;
  branches: Branch[];
  source: string;
  target: string;
  onClose: () => void;
  onMerged: (r: MergeResult) => void;
}

interface Pick { choice: MergeChoice; text?: string }

const LIST_FIELDS = new Set(["requirement_ids", "actors", "acceptance_criteria"]);
const HIDDEN = new Set(["position", "rev", "field_revs"]);

/** A field value as short text; entity snapshots (deletion conflicts) list what differs from the base. */
function show(v: unknown, base?: unknown): string {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "(none)";
  if (typeof v === "object") {
    const o = v as Record<string, unknown>;
    const b = (base ?? {}) as Record<string, unknown>;
    const diff = Object.keys(o).filter((k) => !HIDDEN.has(k) && JSON.stringify(o[k]) !== JSON.stringify(b[k]));
    if (base === undefined) return Object.entries(o).filter(([k]) => !HIDDEN.has(k)).map(([k, x]) => `${k}: ${show(x)}`).join(" · ");
    return diff.length ? diff.map((k) => `${k}: ${show(o[k])}`).join(" · ") : "unchanged";
  }
  return String(v) === "" ? '""' : String(v);
}

function conflictTitle(c: MergeConflict, source: string, target: string) {
  if (c.kind === "deleted-in-ours") return `deleted in ${target}, changed in ${source}`;
  if (c.kind === "deleted-in-theirs") return `changed in ${target}, deleted in ${source}`;
  return c.field;
}

/**
 * Merge one branch into another: a three-way diff from their common base. Changes made on one side only are merged
 * automatically; each conflict shows base / target (ours) / source (theirs) and you pick a side or type a value.
 * Optional AI suggestions explain a choice per conflict, but you still confirm. A safety version is saved first.
 */
export function MergeDialog({ pid, branches, source: initialSource, target: initialTarget, onClose, onMerged }: Props) {
  const [source, setSource] = useState(initialSource);
  const [target, setTarget] = useState(initialTarget);
  const [preview, setPreview] = useState<MergePreview | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [picks, setPicks] = useState<Record<string, Pick>>({});
  const [suggestions, setSuggestions] = useState<Record<string, MergeSuggestion>>({});
  const [suggesting, setSuggesting] = useState(false);
  const [aiNote, setAiNote] = useState("");
  const [applying, setApplying] = useState(false);

  const load = useCallback(async () => {
    setPreview(null); setPicks({}); setSuggestions({}); setAiNote(""); setError("");
    if (source === target) { setError("Choose two different branches."); return; }
    setLoading(true);
    try { setPreview(await api.mergePreview(pid, source, target)); }
    catch (e) { setError((e as Error).message); }
    finally { setLoading(false); }
  }, [pid, source, target]);
  useEffect(() => { load(); }, [load]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !applying) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [applying, onClose]);

  const conflicts = preview?.conflicts ?? [];
  const unresolved = conflicts.filter((c) => !picks[c.id] || (picks[c.id].choice === "custom" && !(picks[c.id].text ?? "").trim())).length;
  const changes = preview?.changes;
  const autoCount = changes?.total_changes ?? 0;

  const pick = (id: string, p: Pick) => setPicks((cur) => ({ ...cur, [id]: p }));
  const pickAll = (choice: "ours" | "theirs") => setPicks(Object.fromEntries(conflicts.map((c) => [c.id, { choice }])));

  const suggest = async () => {
    setSuggesting(true); setAiNote("");
    try {
      const r = await api.mergeSuggest(pid, source, target);
      if (r.engine === "none") { setAiNote("No AI model is configured, so there are no suggestions. Pick each side yourself."); return; }
      setSuggestions(Object.fromEntries(r.suggestions.map((s) => [s.id, s])));
      if (!r.suggestions.length) setAiNote("The AI had no suggestion for these conflicts.");
    } catch (e) { setAiNote((e as Error).message); }
    finally { setSuggesting(false); }
  };
  const useSuggestion = (c: MergeConflict, s: MergeSuggestion) =>
    pick(c.id, s.choice === "custom" ? { choice: "custom", text: Array.isArray(s.value) ? s.value.join(", ") : String(s.value ?? "") } : { choice: s.choice });

  const apply = async () => {
    if (!preview) return;
    setApplying(true); setError("");
    const resolutions = conflicts.map((c) => {
      const p = picks[c.id];
      if (p.choice !== "custom") return { id: c.id, choice: p.choice };
      const text = (p.text ?? "").trim();
      const listy = LIST_FIELDS.has(c.field) || Array.isArray(c.ours) || Array.isArray(c.theirs);
      return { id: c.id, choice: "custom" as const, value: listy ? text.split(",").map((x) => x.trim()).filter(Boolean) : text };
    });
    try {
      onMerged(await api.mergeApply(pid, source, target, resolutions, preview.source_rev, preview.target_rev));
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError(`${e.message}. The preview was refreshed: check the conflicts again.`);
        await load();
      } else setError((e as Error).message);
    } finally { setApplying(false); }
  };

  const baseLabel = useMemo(() => {
    const b = preview?.base;
    if (!b) return "";
    if (b.how === "common-ancestor") return `from v${b.version}, the last version both branches share`;
    if (b.how === "branch-point") return `from v${b.version}, where ${source} was created`;
    return "with no shared history: everything that differs is compared directly";
  }, [preview, source]);
  const branchOptions = branches.map((b) => ({ value: b.name, label: b.name, icon: <Icon as={GitBranch} size={14} /> }));

  return (
    <div className="modal-backdrop" onClick={() => !applying && onClose()}>
      <div className="modal wide merge" role="dialog" aria-label="Merge branches" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Merge branches</h3>
          <button className="ghost" onClick={onClose} disabled={applying} aria-label="Close"><Icon as={X} size={16} /></button>
        </div>
        <div className="row gap wrap merge-pick">
          <label className="small">Merge
            <Select value={source} onChange={setSource} aria-label="Source branch" options={branchOptions} />
          </label>
          <label className="small">into
            <Select value={target} onChange={setTarget} aria-label="Target branch" options={branchOptions} />
          </label>
          {preview && <span className="muted small">Three-way diff {baseLabel}.</span>}
        </div>

        {loading && <p className="muted small">Comparing branches…</p>}
        {error && <p className="error small" role="alert"><Icon as={TriangleAlert} size={14} /> {error}</p>}

        {preview && changes && (
          <>
            <section>
              <h4>Merged automatically <span className="muted small">{autoCount}</span></h4>
              {autoCount === 0 && !conflicts.length && <p className="muted small">{target} already has everything from {source}.</p>}
              {autoCount === 0 && conflicts.length > 0 && <p className="muted small">Nothing else changes; resolve the conflicts below.</p>}
              <ul className="merge-changes">
                {([["nodes", "component"], ["edges", "connection"], ["requirements", "requirement"]] as const).flatMap(([k, label]) => [
                  ...changes[k].added.map((x) => <li key={`a${k}${x.id}`} className="added"><span className="lg added">added</span> {label} <b>{x.name}</b></li>),
                  ...changes[k].removed.map((x) => <li key={`r${k}${x.id}`} className="removed"><span className="lg removed">removed</span> {label} <b>{x.name}</b></li>),
                  ...changes[k].changed.map((x) => (
                    <li key={`c${k}${x.id}`}><span className="lg changed">changed</span> {label} <b>{x.name}</b>
                      <span className="muted small"> {x.fields.map((f) => f.field === "position" ? "moved" : `${f.field}: ${show(f.from)} → ${show(f.to)}`).join(" · ")}</span>
                    </li>
                  )),
                ])}
              </ul>
              {preview.renumbered.map((r) => <p key={r.from} className="muted small">Both branches added {r.from}: “{r.title}” from {source} becomes {r.to}.</p>)}
              {preview.dropped.map((d) => <p key={`${d.collection}${d.id}`} className="warn small">Drops {d.entity} <b>{d.name}</b>: {d.reason}.</p>)}
            </section>

            {conflicts.length > 0 && (
              <section>
                <div className="row between wrap">
                  <h4>Conflicts <span className="muted small">{conflicts.length - unresolved}/{conflicts.length} resolved</span></h4>
                  <div className="row gap">
                    <button className="small" onClick={() => pickAll("ours")}>All {target}</button>
                    <button className="small" onClick={() => pickAll("theirs")}>All {source}</button>
                    <button className="small" onClick={suggest} disabled={suggesting}>{suggesting ? "Thinking…" : <><Icon as={Sparkles} size={14} /> Suggest with AI</>}</button>
                  </div>
                </div>
                {aiNote && <p className="muted small">{aiNote}</p>}
                <ul className="merge-conflicts">
                  {conflicts.map((c) => {
                    const p = picks[c.id];
                    const s = suggestions[c.id];
                    const isDelete = c.kind !== "field";
                    return (
                      <li key={c.id} className={p ? "resolved" : ""}>
                        <div className="row between wrap">
                          <span><span className="chip">{c.entity}</span> <b>{c.name}</b> <span className="muted small">· {conflictTitle(c, source, target)}</span></span>
                        </div>
                        <div className="merge-sides">
                          <div className="merge-side base"><div className="tiny muted">base</div><div className="small">{show(c.base)}</div></div>
                          <button className={`merge-side ${p?.choice === "ours" ? "on" : ""}`} onClick={() => pick(c.id, { choice: "ours" })} aria-pressed={p?.choice === "ours"}>
                            <div className="tiny muted">{target} (ours){isDelete ? (c.ours === null ? " · deleted" : " · keep") : ""}</div>
                            <div className="small">{c.ours === null ? "deleted" : show(c.ours, isDelete ? c.base : undefined)}</div>
                          </button>
                          <button className={`merge-side ${p?.choice === "theirs" ? "on" : ""}`} onClick={() => pick(c.id, { choice: "theirs" })} aria-pressed={p?.choice === "theirs"}>
                            <div className="tiny muted">{source} (theirs){isDelete ? (c.theirs === null ? " · deleted" : " · keep") : ""}</div>
                            <div className="small">{c.theirs === null ? "deleted" : show(c.theirs, isDelete ? c.base : undefined)}</div>
                          </button>
                        </div>
                        {!isDelete && (
                          <div className="row gap">
                            <button className={`small ${p?.choice === "custom" ? "on" : ""}`} onClick={() => pick(c.id, { choice: "custom", text: p?.text ?? show(c.ours).replace(/^—$|^""$/, "") })}>Custom value</button>
                            {p?.choice === "custom" && (
                              <input className="grow" autoFocus value={p.text ?? ""} onChange={(e) => pick(c.id, { choice: "custom", text: e.target.value })}
                                aria-label={`Custom value for ${c.name} ${c.field}`} placeholder={LIST_FIELDS.has(c.field) ? "comma-separated" : "value"} />
                            )}
                          </div>
                        )}
                        {s && (
                          <div className="merge-ai small">
                            <span><Icon as={Sparkles} size={12} /> AI suggests <b>{s.choice === "ours" ? target : s.choice === "theirs" ? source : `“${show(s.value)}”`}</b>: {s.rationale}</span>
                            <button className="small" onClick={() => useSuggestion(c, s)}>Use</button>
                          </div>
                        )}
                      </li>
                    );
                  })}
                </ul>
              </section>
            )}

            <div className="row between wrap">
              <span className="muted small">A version of {target} is saved first, so the merge can be undone by restoring it.</span>
              <div className="row gap">
                <button onClick={onClose} disabled={applying}>Cancel</button>
                <button className="primary" onClick={apply} disabled={applying || unresolved > 0 || (autoCount === 0 && !conflicts.length)}
                  title={unresolved ? `Resolve ${unresolved} conflict(s) first` : undefined}>
                  {applying ? "Merging…" : `Merge into ${target}`}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
