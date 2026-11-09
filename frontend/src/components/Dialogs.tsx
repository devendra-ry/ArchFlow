import { useCallback, useEffect, useState } from "react";
import { Icon, Sparkles, X } from "../icons";
import { api } from "../api";
import { OpenSpecPanel } from "./OpenSpecPanel";
import { Select } from "./Select";

type Mode = "openspec" | "brief";

function save(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  Object.assign(document.createElement("a"), { href: url, download: name }).click();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/**
 * Vibe coding: hand the design to a coding agent. "OpenSpec" writes and manages spec-driven changes with the
 * OpenSpec workflow (stored in the project); "Quick brief" is the instant one-file prompt.
 */
export function PromptDialog({ pid, branch, nodeIds, requirementIds, readOnly, onClose }: {
  pid: string; branch: string; nodeIds: string[]; requirementIds: string[]; readOnly: boolean; onClose: () => void;
}) {
  const [mode, setMode] = useState<Mode>("openspec");
  const [targets, setTargets] = useState<{ key: string; name: string }[]>([]);
  const [target, setTarget] = useState("claude-code");
  const [extra, setExtra] = useState("");
  const [brief, setBrief] = useState("");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const onBusy = useCallback((b: boolean) => setBusy(b), []);

  useEffect(() => { api.promptTargets().then(setTargets).catch(() => {}); }, []);
  useEffect(() => {
    if (mode !== "brief") return;
    const t = window.setTimeout(() => {
      api.prompt(pid, branch, nodeIds, requirementIds, target, extra).then((r) => setBrief(r.prompt)).catch((e) => setBrief(String(e.message)));
    }, 250);
    return () => window.clearTimeout(t);
  }, [mode, pid, branch, nodeIds, requirementIds, target, extra]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape" && !busy && !(e.target as HTMLElement)?.closest?.("textarea")) onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const copy = async () => {
    try { await navigator.clipboard.writeText(brief); setCopied(true); window.setTimeout(() => setCopied(false), 1500); } catch { /* ignore */ }
  };
  const scope = nodeIds.length || requirementIds.length
    ? `${nodeIds.length} component(s), ${requirementIds.length} requirement(s) + traced context` : "entire architecture";

  return (
    <div className="modal-backdrop" onClick={() => !busy && onClose()}>
      <div className="modal wide vibe" role="dialog" aria-label="Vibe coding" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Vibe code: hand this design to a coding agent</h3>
          <button className="ghost" onClick={onClose} disabled={busy} aria-label="close" title={busy ? "Wait until the change is written" : "Close"}><Icon as={X} size={16} /></button>
        </div>
        <div className="row between">
          <div className="seg" role="tablist" aria-label="Output">
            <button role="tab" aria-selected={mode === "openspec"} className={mode === "openspec" ? "on" : ""} onClick={() => setMode("openspec")}>
              <Icon as={Sparkles} size={14} /> OpenSpec
            </button>
            <button role="tab" aria-selected={mode === "brief"} className={mode === "brief" ? "on" : ""} disabled={busy} onClick={() => setMode("brief")}>
              Quick brief
            </button>
          </div>
          <span className="muted small">Scope: {scope}</span>
        </div>

        {mode === "openspec" ? (
          <OpenSpecPanel pid={pid} branch={branch} nodeIds={nodeIds} requirementIds={requirementIds} targets={targets}
            readOnly={readOnly} onBusy={onBusy} />
        ) : (
          <>
            <p className="muted small vibe-help">A one-file implementation brief built instantly from the design. Paste it into your coding agent.</p>
            <div className="row gap">
              <Select value={target} onChange={setTarget} aria-label="Coding agent"
                options={targets.map((t) => ({ value: t.key, label: t.name }))} />
              <input className="grow" placeholder="Extra instructions (optional)" value={extra} onChange={(e) => setExtra(e.target.value)} />
            </div>
            <pre className="prompt-out">{brief}</pre>
            <div className="row gap end">
              <button onClick={() => save(new Blob([brief], { type: "text/markdown" }), "archflow-implementation-brief.md")} disabled={!brief}>Download .md</button>
              <button className="primary" onClick={copy} disabled={!brief}>{copied ? "Copied ✓" : "Copy prompt"}</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
