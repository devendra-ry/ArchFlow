import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { AiSpan, AiTrace, AiTraceSummary, PromptInfo } from "../types";
import { FEATURE_LABEL, Modal, tokens, usd } from "./OrgDialogs";
import { Select } from "./Select";

interface Props { orgId: string; orgName: string; onClose: () => void }

const ago = (iso: string) => {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago`
    : new Date(iso).toLocaleString();
};
const ms = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)} s` : `${n} ms`);

/** A JSON or text payload, collapsed when long. */
function Payload({ label, value }: { label: string; value: unknown }) {
  const [open, setOpen] = useState(false);
  if (value === null || value === undefined) return <div className="tiny muted">{label}: not recorded</div>;
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 1);
  const long = text.length > 600;
  return (
    <div className="trace-payload">
      <div className="row between">
        <span className="tiny muted">{label}</span>
        {long && <button className="ghost tiny-btn" onClick={() => setOpen(!open)}>{open ? "Collapse" : `Show all (${tokens(text.length)} chars)`}</button>}
      </div>
      <pre className="mono tiny">{long && !open ? text.slice(0, 600) + "…" : text}</pre>
    </div>
  );
}

function SpanRow({ s, i }: { s: AiSpan; i: number }) {
  const [open, setOpen] = useState(false);
  return (
    <li className={`trace-span ${s.kind} ${s.status}`}>
      <button className="trace-span-head" onClick={() => setOpen(!open)} aria-expanded={open}>
        <span className="mono tiny muted">{i + 1}</span>
        <span className={`trace-kind ${s.kind}`}>{s.kind === "model" ? "LLM" : s.kind === "api" ? "API" : "tool"}</span>
        <b className="mono small">{s.kind === "model" ? (s.model || s.name) : s.name}</b>
        <span className="grow" />
        {s.kind === "model" && <span className="tiny muted">{tokens(s.prompt_tokens)} in · {tokens(s.completion_tokens)} out · {usd(s.cost_usd)}{s.calls > 1 ? ` · ${s.calls} calls` : ""}</span>}
        {s.latency_ms > 0 && <span className="tiny muted">{ms(s.latency_ms)}</span>}
        {s.status === "error" && <span className="tiny error">error</span>}
        <span className="tiny muted">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <div className="trace-span-body">
          {s.error && <p className="small error">{s.error}</p>}
          <Payload label={s.kind === "tool" ? "Arguments" : "Request"} value={s.input} />
          <Payload label={s.kind === "tool" ? "Result" : "Response"} value={s.output} />
        </div>
      )}
    </li>
  );
}

function TraceDetail({ orgId, id, onBack, onOpen }: { orgId: string; id: string; onBack: () => void; onOpen: (id: string) => void }) {
  const [t, setT] = useState<AiTrace | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { setT(null); api.aiTrace(orgId, id).then(setT).catch((e) => setError((e as Error).message)); }, [orgId, id]);
  if (error) return <p className="error small">{error}</p>;
  if (!t) return <p className="muted small">Loading…</p>;
  return (
    <div className="trace-detail">
      <div className="row between">
        <button className="small" onClick={() => (t?.parent_id ? onOpen(t.parent_id) : onBack())}>{t?.parent_id ? "← Parent run" : "← All runs"}</button>
        <span className="mono tiny muted">{t.id}</span>
      </div>
      <div className="trace-who">
        <div className="small"><b>{t.user_name || "Server or MCP agent"}</b> <span className="muted">asked</span></div>
        <div className="trace-q">{t.question || <span className="muted">(no question recorded)</span>}</div>
        {t.answer && <><div className="small muted">AI answered</div><div className="trace-a">{t.answer}</div></>}
      </div>
      <div className="trace-stats">
        <div><span className="tiny muted">Prompt</span><b className="mono small">{t.prompt}</b></div>
        <div><span className="tiny muted">Feature</span><b className="small">{FEATURE_LABEL[t.feature] ?? t.feature}</b></div>
        <div><span className="tiny muted">Model</span><b className="mono small">{t.models.join(", ") || "–"}</b></div>
        <div><span className="tiny muted">Tokens</span><b className="small">{tokens(t.prompt_tokens)} in · {tokens(t.completion_tokens)} out</b></div>
        <div><span className="tiny muted">Cost</span><b className="small">{usd(t.cost_usd)}</b></div>
        <div><span className="tiny muted">Latency</span><b className="small">{ms(t.latency_ms)}</b></div>
        <div><span className="tiny muted">Steps</span><b className="small">{t.model_calls} LLM · {t.tool_calls} tool</b></div>
        <div><span className="tiny muted">Status</span><b className={`small ${t.status === "ok" ? "ok" : "error"}`}>{t.status}</b></div>
      </div>
      {t.error && <p className="small error">{t.error}</p>}
      {t.metadata?.limit_reached === true && <p className="small warn">Stopped at the request limit before the agent finished.</p>}
      <Payload label="Input" value={t.input} />
      <Payload label="Output" value={t.output} />
      {t.children.length > 0 && (
        <>
          <h4>Agents fired in parallel ({t.children.length})</h4>
          <ul className="trace-children">
            {t.children.map((c) => (
              <li key={c.id}>
                <button className="trace-child" onClick={() => onOpen(c.id)}>
                  <span className={`dot ${c.status === "ok" ? "ok" : "error"}`} />
                  <span className="mono small">{c.prompt}</span>
                  <span className="grow small muted ellipsis">{c.question}</span>
                  <span className="tiny muted">{ms(c.latency_ms)} · {usd(c.cost_usd)}</span>
                </button>
              </li>
            ))}
          </ul>
        </>
      )}
      {t.spans.length > 0 && <h4>Steps</h4>}
      <ol className="trace-spans">{t.spans.map((s, i) => <SpanRow key={i} s={s} i={i} />)}</ol>
      <p className="tiny muted">{t.project_name ? `${t.project_name} · ` : ""}{new Date(t.created_at).toLocaleString()}</p>
    </div>
  );
}

function PromptsTab({ orgId }: { orgId: string }) {
  const [data, setData] = useState<{ prompts: PromptInfo[]; problems: string[] } | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { api.aiPrompts(orgId).then(setData).catch((e) => setError((e as Error).message)); }, [orgId]);
  if (error) return <p className="error small">{error}</p>;
  if (!data) return <p className="muted small">Loading…</p>;
  return (
    <div className="prompt-list">
      <p className="muted small">Every AI feature runs a versioned prompt (server: <code>archflow/prompts/*.yaml</code>). Released versions are
        locked; a change ships as a new version, is compared with <code>archflow-eval</code>, and can be pinned or rolled back with
        {" "}<code>ARCHFLOW_PROMPTS</code>.</p>
      {data.problems.length > 0 && <div className="small warn">{data.problems.map((p) => <div key={p}>⚠ {p}</div>)}</div>}
      <table className="trace-table">
        <thead><tr><th>Prompt</th><th>Feature</th><th>Active</th><th>Versions</th></tr></thead>
        <tbody>
          {data.prompts.map((p) => (
            <tr key={p.id} title={p.description}>
              <td><span className="mono small">{p.id}</span><div className="tiny muted">{p.description}</div></td>
              <td className="small">{FEATURE_LABEL[p.feature] ?? p.feature}</td>
              <td><span className="mono small">{p.active}</span>{p.active !== p.default && <div className="tiny warn">pinned (default {p.default})</div>}</td>
              <td className="tiny">{p.versions.map((v) => (
                <div key={v.version} title={v.notes}><span className="mono">{v.version}</span> · {v.date} · <span className="mono muted">{v.sha.slice(0, 8)}</span></div>
              ))}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Observability for an organization's AI (admins): every agent run with its input, output, model calls, tool
 * calls, tokens, cost and latency, and the versioned prompts behind them. */
export function TracesDialog({ orgId, orgName, onClose }: Props) {
  const [tab, setTab] = useState<"runs" | "prompts">("runs");
  const [rows, setRows] = useState<AiTraceSummary[]>([]);
  const [meta, setMeta] = useState<{ content: string; retention_days: number } | null>(null);
  const [feature, setFeature] = useState("");
  const [status, setStatus] = useState("");
  const [person, setPerson] = useState("");
  const [people, setPeople] = useState<{ id: string; name: string }[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [more, setMore] = useState(false);

  const load = useCallback(async (before?: string) => {
    setError("");
    try {
      const filters: Record<string, string> = { limit: "50" };
      if (feature) filters.feature = feature;
      if (status) filters.status = status;
      if (person) filters.user_id = person;
      if (before) filters.before = before;
      const r = await api.aiTraces(orgId, filters);
      setMeta({ content: r.content, retention_days: r.retention_days });
      setPeople(r.people ?? []);
      setRows((xs) => (before ? [...xs, ...r.traces] : r.traces));
      setMore(r.traces.length === 50);
    } catch (e) { setError((e as Error).message); }
  }, [orgId, feature, status, person]);
  useEffect(() => { load(); }, [load]);

  const totals = rows.reduce((t, r) => ({ cost: t.cost + r.cost_usd, tokens: t.tokens + r.prompt_tokens + r.completion_tokens,
    errors: t.errors + (r.status === "error" ? 1 : 0) }), { cost: 0, tokens: 0, errors: 0 });

  return (
    <Modal title={`AI traces · ${orgName}`} onClose={onClose}>
      <div className="seg ai-tabs" role="tablist">
        <button role="tab" aria-selected={tab === "runs"} className={tab === "runs" ? "on" : ""} onClick={() => setTab("runs")}>Runs</button>
        <button role="tab" aria-selected={tab === "prompts"} className={tab === "prompts" ? "on" : ""} onClick={() => setTab("prompts")}>Prompts</button>
      </div>
      {tab === "prompts" && <PromptsTab orgId={orgId} />}
      {tab === "runs" && openId && <TraceDetail orgId={orgId} id={openId} onBack={() => setOpenId(null)} onOpen={setOpenId} />}
      {tab === "runs" && !openId && (
        <>
          <div className="row gap wrap">
            <Select size="sm" value={feature} onChange={setFeature} aria-label="Feature"
              options={[{ value: "", label: "All features" }, ...Object.entries(FEATURE_LABEL).map(([k, v]) => ({ value: k, label: v }))]} />
            <Select size="sm" value={person} onChange={setPerson} aria-label="Person"
              options={[{ value: "", label: "Everyone" }, ...people.map((p) => ({ value: p.id, label: p.name }))]} />
            <Select size="sm" value={status} onChange={setStatus} aria-label="Status"
              options={[{ value: "", label: "Any status" }, { value: "ok", label: "OK" }, { value: "error", label: "Errors" }]} />
            <button className="small" onClick={() => load()}>Refresh</button>
            <span className="grow" />
            <span className="tiny muted">{rows.length} runs · {tokens(totals.tokens)} tokens · {usd(totals.cost)}{totals.errors ? ` · ${totals.errors} errors` : ""}</span>
          </div>
          {meta?.content === "metadata" && <p className="tiny muted">Prompt and response text is not stored (ARCHFLOW_TRACES=metadata).</p>}
          {meta?.content === "off" && <p className="tiny warn">Tracing is off on this server (ARCHFLOW_TRACES=off).</p>}
          {error && <p className="error small">{error}</p>}
          <div className="trace-table-wrap">
            <table className="trace-table">
              <thead><tr><th>When</th><th>Who</th><th>Asked</th><th>AI answered</th><th>Feature</th><th>Prompt</th><th>Model</th><th>Tokens</th><th>Cost</th><th>Time</th><th>Steps</th></tr></thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id} className={r.status === "error" ? "bad" : ""} onClick={() => setOpenId(r.id)} title={r.error || "Show the run"}>
                    <td className="tiny">{ago(r.created_at)}</td>
                    <td className="small">{r.user_name || <span className="muted">server</span>}</td>
                    <td className="small trace-cell" title={r.question}>{r.question || "–"}</td>
                    <td className="small trace-cell muted" title={r.answer}>{r.answer || "–"}</td>
                    <td className="small">{FEATURE_LABEL[r.feature] ?? r.feature}{r.project_name ? <div className="tiny muted">{r.project_name}</div> : null}</td>
                    <td className="mono tiny">{r.prompt.replace("@parallel", " · agents")}</td>
                    <td className="mono tiny">{r.models[0] ?? "–"}</td>
                    <td className="tiny">{tokens(r.prompt_tokens)} / {tokens(r.completion_tokens)}</td>
                    <td className="tiny">{usd(r.cost_usd)}</td>
                    <td className="tiny">{ms(r.latency_ms)}</td>
                    <td className="tiny">{r.model_calls} LLM{r.tool_calls ? ` · ${r.tool_calls} tool` : ""}{r.status === "error" ? " · ⚠" : ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {rows.length === 0 && !error && <p className="muted small">No AI runs recorded yet. Every assistant answer, canvas edit, design turn,
            comparison and suggestion appears here with its input, output, model calls, tool calls and cost.</p>}
          {more && <button className="small" onClick={() => load(rows[rows.length - 1]?.created_at)}>Load older runs</button>}
          {meta && <p className="tiny muted">Kept for {meta.retention_days} days. Export to Langfuse, Phoenix or any OpenTelemetry backend by setting
            {" "}<code>OTEL_EXPORTER_OTLP_ENDPOINT</code> on the server.</p>}
        </>
      )}
    </Modal>
  );
}
