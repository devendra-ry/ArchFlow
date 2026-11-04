import { useEffect, useState } from "react";
import { api } from "../api";
import type { GenerationKind, GenerationResult, GenerationTarget } from "../types";
import { useEscape } from "../useEscape";
import { Copy } from "./ConnectDialog";
import { Select } from "./Select";

interface Props {
  pid: string;
  branch: string;
  aiAvailable: boolean;
  canEdit: boolean;
  toast: (msg: string) => void;
  onClose: () => void;
}

const KINDS: { id: GenerationKind; label: string; targets: { id: GenerationTarget; label: string; hint: string }[] }[] = [
  { id: "iac", label: "Infrastructure", targets: [
    { id: "terraform", label: "Terraform", hint: "Cloud resources for each component, with variables and outputs" },
    { id: "compose", label: "Docker Compose", hint: "Run the stack locally with open-source equivalents" },
    { id: "k8s", label: "Kubernetes", hint: "Deployments and Services for each service" },
  ] },
  { id: "tests", label: "Tests", targets: [
    { id: "gherkin", label: "Gherkin", hint: "Feature files from the acceptance criteria" },
    { id: "pytest", label: "pytest", hint: "Python test skeletons from the acceptance criteria" },
    { id: "playwright", label: "Playwright", hint: "Browser test skeletons from the acceptance criteria" },
  ] },
  { id: "docs", label: "Documents", targets: [
    { id: "srs", label: "SRS", hint: "Software requirements specification (IEEE 830 style)" },
    { id: "brd", label: "BRD", hint: "Business requirements document" },
    { id: "runbook", label: "Runbooks", hint: "One per service and data store: health, scaling, failures, alerts" },
  ] },
];

/** Tools → Generate: infrastructure as code, tests and documents from the branch, previewed before download. */
export function GenerateDialog({ pid, branch, aiAvailable, canEdit, toast, onClose }: Props) {
  const escRef = useEscape(onClose);
  const [kind, setKind] = useState<GenerationKind>("iac");
  const [target, setTarget] = useState<GenerationTarget>("terraform");
  const [cloud, setCloud] = useState("");
  const [useAi, setUseAi] = useState(false);
  const [busy, setBusy] = useState(false);
  const [zipping, setZipping] = useState(false);
  const [result, setResult] = useState<GenerationResult | null>(null);
  const [file, setFile] = useState(0);
  const [error, setError] = useState("");
  const aiAllowed = aiAvailable && canEdit;
  const aiTip = !canEdit ? "Only editors can use AI generation" : !aiAvailable ? "No AI model is set up" : "";

  const run = async (k = kind, t = target, ai = useAi && aiAllowed, c = cloud) => {
    setBusy(true);
    setError("");
    try {
      const res = await api.generate(pid, branch, k, t, ai, t === "terraform" ? c : "");
      setResult(res);
      setFile(0);
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  };
  useEffect(() => { run(); }, [pid, branch]); // eslint-disable-line react-hooks/exhaustive-deps

  const pickKind = (k: GenerationKind) => {
    const t = KINDS.find((x) => x.id === k)!.targets[0].id;
    setKind(k);
    setTarget(t);
    run(k, t, false);
  };
  const pickTarget = (t: GenerationTarget) => { setTarget(t); run(kind, t, false); };

  const zip = async () => {
    if (!result) return;
    setZipping(true);
    try { toast(`Saved ${await api.generateZip(pid, branch, result.kind, result.target, result.files)}`); }
    catch (err) { toast(`⚠ ${(err as Error).message}`); }
    finally { setZipping(false); }
  };

  const targets = KINDS.find((x) => x.id === kind)!.targets;
  const current = result?.files[file];
  const failed = result?.checks.filter((c) => !c.ok) ?? [];
  return (
    <div className="modal-backdrop" onClick={onClose} ref={escRef}>
      <div className="modal wide gen-dialog" role="dialog" aria-label="Generate files" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Generate</h3>
          <button className="ghost" onClick={onClose} aria-label="close">✕</button>
        </div>
        <p className="muted small">Files built from branch <b>{branch}</b>: its components, connections and requirements.
          Check them before you use them.</p>

        <nav className="tabs inline" aria-label="What to generate">
          {KINDS.map((k) => <button key={k.id} className={kind === k.id ? "on" : ""} disabled={busy}
            onClick={() => pickKind(k.id)}>{k.label}</button>)}
        </nav>
        <div className="gen-targets" role="radiogroup" aria-label="Format">
          {targets.map((t) => (
            <button key={t.id} role="radio" aria-checked={target === t.id} className={`small ${target === t.id ? "primary" : ""}`}
              disabled={busy} title={t.hint} onClick={() => pickTarget(t.id)}>{t.label}</button>
          ))}
        </div>
        <p className="muted tiny">{targets.find((t) => t.id === target)?.hint}</p>

        <div className="row gap wrap gen-options">
          {target === "terraform" && (
            <label className="small">Cloud{" "}
              <Select value={cloud} disabled={busy} aria-label="Cloud" size="sm"
                onChange={(v) => { setCloud(v); run(kind, target, false, v); }}
                options={[{ value: "", label: `From the design${result?.cloud && !cloud ? ` (${result.cloud.toUpperCase()})` : ""}` },
                  { value: "aws", label: "AWS" }, { value: "azure", label: "Azure" }, { value: "gcp", label: "Google Cloud" }]} />
            </label>
          )}
          <label className="small" title={aiTip}>
            <input type="checkbox" checked={useAi && aiAllowed} disabled={!aiAllowed || busy} onChange={(e) => setUseAi(e.target.checked)} />
            {" "}Improve with AI
          </label>
          <button className="small" disabled={busy} onClick={() => run()}>
            {busy ? "Generating…" : useAi && aiAllowed ? "Generate with AI" : "Regenerate"}</button>
        </div>

        {error && <p className="small" role="alert">⚠ {error}</p>}
        {result && (
          <p className="tiny muted">
            {result.engine === "ai" ? "Improved by AI" : "Built-in version"} · {result.files.length} file{result.files.length === 1 ? "" : "s"} ·{" "}
            {failed.length ? <span className="gen-bad">{failed.length} check{failed.length === 1 ? "" : "s"} failed</span> : <span className="gen-ok">all checks passed</span>}
          </p>
        )}
        {result?.notes.map((n) => <p key={n} className="muted tiny">{n}</p>)}
        {failed.map((c) => <p key={c.path + c.detail} className="tiny gen-bad">⚠ {c.path}: {c.detail}</p>)}

        {result && result.files.length > 0 && (
          <div className="gen-preview">
            <div className="gen-files" role="tablist" aria-label="Files">
              {result.files.map((f, i) => (
                <button key={f.path} role="tab" aria-selected={i === file} className={i === file ? "on" : ""}
                  title={f.path} onClick={() => setFile(i)}>{f.path}</button>
              ))}
            </div>
            <pre className="gen-code" aria-label={`${current?.path ?? ""} preview`}><code>{current?.content}</code></pre>
          </div>
        )}
        {!result && !error && <p className="muted small">{busy ? "Generating…" : ""}</p>}

        <div className="row gap wrap">
          <button className="primary small" disabled={!result?.files.length || zipping} onClick={zip}>
            {zipping ? "Zipping…" : "Download zip"}</button>
          {current && <Copy text={current.content} label="Copy file" />}
        </div>
      </div>
    </div>
  );
}
