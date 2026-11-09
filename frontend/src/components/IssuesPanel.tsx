import { useState } from "react";
import { CircleCheck, Icon, TriangleAlert } from "../icons";
import { api } from "../api";
import type { Analysis, Issue, ReviewResult } from "../types";

interface Props {
  pid: string;
  branch: string;
  analysis: Analysis | null;
  aiAvailable: boolean;
  onFocus: (issue: Issue) => void;
  /** Show these components (by name) on the canvas. */
  onShow: (componentNames: string[]) => void;
  /** Hand an instruction to the assistant's Edit mode (the person reviews it and presses Apply). */
  onFix: (instruction: string) => void;
  onOpenCode?: () => void;
}

const SEV_ORDER = { error: 0, warning: 1, info: 2 } as const;
const FINDING_LABEL = { critical: "Critical", high: "High", medium: "Medium", low: "Low" } as const;

function Meter({ label, value, hint }: { label: string; value: number; hint: string }) {
  const tone = value >= 80 ? "good" : value >= 50 ? "mid" : "bad";
  return (
    <div className="meter" title={hint}>
      <div className="row between small"><span>{label}</span><b>{value}%</b></div>
      <div className="meter-bar"><div className={`meter-fill ${tone}`} style={{ width: `${value}%` }} /></div>
    </div>
  );
}

/** Specialist agents reviewing the design in parallel (server: review.py); findings come with a one-click fix. */
function AIReview({ pid, branch, aiAvailable, onShow, onFix }: Pick<Props, "pid" | "branch" | "aiAvailable" | "onShow" | "onFix">) {
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<ReviewResult | null>(null);
  const [error, setError] = useState("");
  const run = async () => {
    setRunning(true); setError("");
    try { setResult(await api.review(pid, branch)); }
    catch (e) { setError((e as Error).message); }
    finally { setRunning(false); }
  };
  const specialists = result ? Object.entries(result.specialists) : [];
  return (
    <section className="ai-review">
      <div className="row between">
        <span><b>AI review</b> <span className="tiny muted">security · reliability · performance · cost, in parallel</span></span>
        <button className="small primary" disabled={!aiAvailable || running} onClick={run}
          title={aiAvailable ? "Four specialist agents review this design at the same time" : "Needs an AI model (AI settings)"}>
          {running ? "Reviewing…" : result ? "Run again" : "Review"}
        </button>
      </div>
      {running && (
        <div className="specialists">
          {["Security & privacy", "Reliability", "Performance & scale", "Cost & operations"].map((l) => (
            <span key={l} className="chip pulse">{l}</span>
          ))}
        </div>
      )}
      {error && <p className="small error">{error}</p>}
      {result && !running && (
        <>
          <div className="specialists">
            {specialists.map(([k, s]) => (
              <span key={k} className={`chip ${s.status === "ok" ? "" : "bad"}`} title={s.status === "ok" ? s.summary : s.error}>
                {s.status === "ok" ? <Icon as={CircleCheck} size={12} /> : <Icon as={TriangleAlert} size={12} />} {s.label} {s.status === "ok" ? `· ${s.findings}` : ""}
              </span>
            ))}
          </div>
          {result.findings.length === 0 && <p className="small muted">No problems found.</p>}
          <ul className="finding-list">
            {result.findings.map((f, i) => (
              <li key={i} className={`finding f-${f.severity}`}>
                <div className="row between gap">
                  <b className="small">{f.title}</b>
                  <span className={`sev-chip f-${f.severity}`}>{FINDING_LABEL[f.severity]}</span>
                </div>
                <div className="small">{f.detail}</div>
                <div className="row gap wrap">
                  {f.fix && <button className="small primary" onClick={() => onFix(f.fix)} title={f.fix}>Fix with AI</button>}
                  {f.components.length > 0 && <button className="small" onClick={() => onShow(f.components)}>Show on canvas</button>}
                  {f.requirement_ids.map((r) => <span key={r} className="chip">{r}</span>)}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}
      {!result && !running && !error && (
        <p className="tiny muted">{aiAvailable ? "Finds single points of failure, security gaps, latency risks and cost issues, each with a fix you can apply."
          : "Add an AI model in AI settings to review the design."}</p>
      )}
    </section>
  );
}

export function IssuesPanel({ pid, branch, analysis, aiAvailable, onFocus, onShow, onFix, onOpenCode }: Props) {
  const [showInfo, setShowInfo] = useState(false);
  if (!analysis) return <div className="panel"><p className="muted small pad">Checking the architecture…</p></div>;
  const h = analysis.health;
  const all = [...analysis.architecture_issues, ...analysis.requirement_issues, ...(analysis.drift_issues ?? [])]
    .sort((a, b) => SEV_ORDER[a.severity] - SEV_ORDER[b.severity]);
  const counts = { error: 0, warning: 0, info: 0 };
  all.forEach((i) => { counts[i.severity] += 1; });
  const shown = showInfo ? all : all.filter((i) => i.severity !== "info");
  const score = Math.round((h.architecture_score + h.traceability + h.requirements_complete) / 3);

  return (
    <div className="panel">
      <div className="health-summary">
        <div className={`score-ring ${score >= 80 ? "good" : score >= 50 ? "mid" : "bad"}`} title="Average of architecture checks, traceability and requirement completeness">
          <b>{score}</b><span className="tiny">health</span>
        </div>
        <div className="small">
          {counts.error + counts.warning === 0 ? <b>No problems found</b>
            : <><b>{counts.error + counts.warning}</b> to look at{counts.error ? <> · <span className="error">{counts.error} error{counts.error > 1 ? "s" : ""}</span></> : null}</>}
          <div className="muted tiny">{h.counts.requirements} requirements · {h.counts.components} components · {h.counts.connections} connections</div>
        </div>
      </div>
      <div className="health pad">
        <Meter label="Architecture checks" value={h.architecture_score} hint="Structural checks: connections, data stores, auth, single points of failure" />
        <Meter label="Traceability" value={h.traceability} hint="Requirements implemented by at least one component" />
        <Meter label="Requirements complete" value={h.requirements_complete} hint="Requirements with a description and acceptance criteria" />
        {h.code_alignment !== undefined && (
          <button className="link code-meter" onClick={onOpenCode} title="Design vs code (latest repository analysis)">
            <Meter label="Matches the code" value={h.code_alignment} hint="How much of the design the latest code analysis confirms" />
          </button>
        )}
      </div>
      <AIReview pid={pid} branch={branch} aiAvailable={aiAvailable} onShow={onShow} onFix={onFix} />
      <div className="row between pad">
        <h4>Checks</h4>
        {counts.info > 0 && <button className="link small" onClick={() => setShowInfo(!showInfo)}>{showInfo ? "Hide" : "Show"} {counts.info} tip{counts.info > 1 ? "s" : ""}</button>}
      </div>
      {shown.length === 0 && <p className="muted small pad">{all.length ? "Only tips left." : "Every check passes."}</p>}
      <ul className="issue-list">
        {shown.map((i, k) => (
          <li key={k} className={`issue ${i.severity}`} onClick={() => onFocus(i)}
            title={i.node_ids.length || i.requirement_ids.length ? "Show on the canvas" : undefined}>
            <div><span className="sev">{i.code.startsWith("drift_") ? `code · ${i.severity}`
              : i.code === "completeness" ? `missing · ${i.severity}` : i.severity}</span> {i.message}</div>
            {i.suggestion && <div className="small muted">{i.suggestion}</div>}
          </li>
        ))}
      </ul>
    </div>
  );
}
