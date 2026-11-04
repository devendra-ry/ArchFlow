import { Fragment, useEffect, useState } from "react";
import { api } from "../api";
import { useEscape } from "../useEscape";
import type { CostAssumptionKey, CostEstimate } from "../types";

interface Props {
  pid: string;
  branch: string;
  aiAvailable: boolean;
  /** The branch's revision: the estimate refreshes when the architecture changes. */
  rev: number;
  onClose: () => void;
  onSelect?: (nodeId: string) => void;
}

const usd = (n: number, digits = 0) =>
  n.toLocaleString(undefined, { style: "currency", currency: "USD", maximumFractionDigits: digits, minimumFractionDigits: digits });
const range = (r: { low: number; high: number }) => `${usd(r.low)} – ${usd(r.high)}`;

const FIELDS: { key: CostAssumptionKey; label: string; step?: number; hint?: string }[] = [
  { key: "monthly_active_users", label: "Monthly active users", step: 100 },
  { key: "peak_rps", label: "Peak requests / second", step: 1 },
  { key: "storage_gb", label: "Stored data (GB)", step: 10 },
  { key: "egress_gb", label: "Data transfer out (GB / month)", step: 10 },
  { key: "availability", label: "Availability target (%)", step: 0.01, hint: "99.95% or more doubles stateful tiers" },
  { key: "llm_calls_per_user", label: "LLM calls per user / month", step: 1 },
  { key: "llm_input_tokens", label: "Input tokens per call", step: 100 },
  { key: "llm_output_tokens", label: "Output tokens per call", step: 50 },
  { key: "emails_per_user", label: "Emails per user / month", step: 1 },
  { key: "messages_per_user", label: "SMS / chat messages per user", step: 1 },
];
const SOURCE = (s: string) => (s === "you" ? "set by you" : s === "default" ? "typical value" : s === "derived" ? "derived" : `from ${s}`);
const BUDGET: Record<string, string> = { within: "within budget", at_risk: "may exceed the budget", over: "over budget" };

/** Monthly cost range of the architecture, line by line, with the assumptions behind it and ways to save. */
export function CostDialog({ pid, branch, aiAvailable, rev, onClose, onSelect }: Props) {
  const [est, setEst] = useState<CostEstimate | null>(null);
  const [overrides, setOverrides] = useState<Record<string, unknown>>({});
  const [useAi, setUseAi] = useState(aiAvailable);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const escRef = useEscape(onClose);

  const run = async (o = overrides, ai = useAi) => {
    setBusy(true); setError("");
    try { setEst(await api.costs(pid, branch, o, ai)); } catch (e) { setError((e as Error).message); }
    setBusy(false);
  };
  useEffect(() => { run(); }, [pid, branch, rev]); // eslint-disable-line react-hooks/exhaustive-deps

  const set = (key: string, value: unknown) => setOverrides((o) => ({ ...o, [key]: value }));
  const a = est?.assumptions;
  const val = (k: CostAssumptionKey) => (k in overrides ? overrides[k] : a?.[k]?.value) as string | number | boolean;

  return (
    <div className="modal-backdrop" onClick={onClose} ref={escRef}>
      <div className="modal wide costs" role="dialog" aria-label="Cost estimate" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Cost estimate · {branch}</h3>
          <span className="row gap">
            <button className="small" onClick={() => api.costsCsv(pid, branch).catch((e) => setError(e.message))}>⤓ CSV</button>
            <button className="ghost" onClick={onClose} aria-label="close">✕</button>
          </span>
        </div>
        {!est && busy && <p className="small muted"><span className="spinner" aria-hidden /> Pricing the architecture…</p>}
        {est && (
          <>
            <div className="cost-totals">
              <div><div className="tiny muted">Production / month</div><b>{usd(est.production.monthly)}</b><div className="tiny muted">{range(est.production)}</div></div>
              {est.staging.monthly > 0 && <div><div className="tiny muted">Staging / month</div><b>{usd(est.staging.monthly)}</b>
                <div className="tiny muted">{Math.round(Number(val("staging_share")) * 100)}% of production</div></div>}
              <div><div className="tiny muted">Total / month</div><b>{usd(est.total.monthly)}</b><div className="tiny muted">{range(est.total)}</div></div>
              <div><div className="tiny muted">Per year</div><b>{usd(est.annual.monthly)}</b></div>
              {est.budget && (
                <div className={`cost-budget ${est.budget.status}`} title={est.budget.text}>
                  <div className="tiny muted">Budget ({est.budget.source})</div><b>{usd(est.budget.monthly)}</b>
                  <div className="tiny">{BUDGET[est.budget.status]}</div>
                </div>
              )}
            </div>
            {est.drivers.length > 0 && (
              <p className="small">Biggest costs: {est.drivers.map((d) => `${d.name} (${Math.round(d.share * 100)}%)`).join(", ")}
                {est.high_availability && <> · <span className="muted">high availability: stateful tiers doubled</span></>}</p>
            )}

            <details className="cost-assumptions">
              <summary className="small"><b>Assumptions</b> <span className="muted">(edit and re-estimate)</span></summary>
              <div className="grid2">
                {FIELDS.map((f) => (
                  <label key={f.key} className="small" title={a?.[f.key]?.text || f.hint}>{f.label}
                    <input type="number" min={0} step={f.step} value={String(val(f.key) ?? "")}
                      onChange={(e) => set(f.key, e.target.value === "" ? null : Number(e.target.value))} />
                    <span className="tiny muted">{f.key in overrides ? "set by you" : SOURCE(a?.[f.key]?.source ?? "default")}</span>
                  </label>
                ))}
                <label className="small">Region
                  <select value={String(val("region"))} onChange={(e) => set("region", e.target.value)}>
                    <option value="us">US</option><option value="eu">EU</option><option value="uk">UK</option>
                    <option value="asia">Asia</option><option value="other">Other</option>
                  </select></label>
                <label className="small row gap"><input type="checkbox" checked={Boolean(val("include_staging"))}
                  onChange={(e) => set("include_staging", e.target.checked)} /> Include a staging environment</label>
              </div>
              <div className="row gap end">
                <label className="row gap small" title={aiAvailable ? "Let the AI write savings recommendations" : "AI is off: rules suggest savings"}>
                  <input type="checkbox" checked={useAi} disabled={!aiAvailable} onChange={(e) => setUseAi(e.target.checked)} /> AI recommendations</label>
                <button className="small" onClick={() => { setOverrides({}); run({}); }} disabled={busy}>Reset</button>
                <button className="primary small" onClick={() => run()} disabled={busy}>{busy ? "Estimating…" : "Re-estimate"}</button>
              </div>
            </details>

            <table className="tokens cost-lines">
              <thead><tr><th>Component</th><th>Service</th><th className="num">Monthly</th><th className="num">Range</th><th>Confidence</th></tr></thead>
              <tbody>
                {est.lines.map((ln) => (
                  <Fragment key={ln.node_id || ln.name}>
                    <tr className="cost-line" onClick={() => setOpen(open === ln.name ? null : ln.name)}>
                      <td>{ln.node_id && onSelect
                        ? <button className="link" onClick={(e) => { e.stopPropagation(); onSelect(ln.node_id); }}>{ln.name}</button> : ln.name}</td>
                      <td className="muted">{ln.technology || ln.kind}{ln.vendor ? ` · ${ln.vendor}` : ""}</td>
                      <td className="num"><b>{usd(ln.monthly)}</b></td>
                      <td className="num muted">{range(ln)}</td>
                      <td className={`muted conf-${ln.confidence}`}>{ln.confidence}</td>
                    </tr>
                    {open === ln.name && (
                      <tr><td colSpan={5} className="small muted">{ln.basis}{ln.notes.length ? ` · ${ln.notes.join(" · ")}` : ""}</td></tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
            {est.unpriced.length > 0 && <p className="tiny muted">Not priced: {est.unpriced.join(", ")}.</p>}

            {est.recommendations.length > 0 && (
              <>
                <h4>Ways to save {est.engine === "llm" ? <span className="tiny muted">(written by AI from these numbers)</span>
                  : <span className="tiny muted">(standard rules; AI is off)</span>}</h4>
                <ul className="cost-recs">
                  {est.recommendations.map((r, i) => (
                    <li key={i}>
                      <div className="row between"><b className="small">{r.title}</b>
                        {r.saving_monthly > 0 && <span className="chip small">save ~{usd(r.saving_monthly)}/mo</span>}</div>
                      <div className="small">{r.detail}</div>
                      <div className="tiny muted">{r.components.join(", ")}{r.tradeoff ? ` · Trade-off: ${r.tradeoff}` : ""} · effort {r.effort}</div>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {est.questions.length > 0 && (
              <p className="small muted">To sharpen the estimate: {est.questions.join(" ")}</p>
            )}
            <p className="tiny muted">{est.disclaimer}</p>
          </>
        )}
        {error && <p className="small error">{error}</p>}
      </div>
    </div>
  );
}
