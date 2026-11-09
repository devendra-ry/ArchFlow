import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, CircleCheck, GitBranch, Icon, Scale, Star, TriangleAlert, X } from "../icons";
import { api } from "../api";
import { can, type Branch, type Decision, type DecisionAlternative, type DecisionStatus, type Evaluation, type Role, type User } from "../types";
import { useEscape } from "../useEscape";
import { Markdown } from "./Markdown";
import { Select, plainOptions } from "./Select";

interface Props {
  pid: string;
  branches: Branch[];
  user: User;
  role?: Role | null;
  onAdopted: (r: { version: number; branch: string; source: string; decisionId: string }) => void;
  toast: (text: string) => void;
}

const STATUSES: DecisionStatus[] = ["proposed", "accepted", "rejected", "superseded"];
const ENGINE_LABEL: Record<Evaluation["engine"], string> = {
  jev: "Jev decision API", llm: "LLM evaluation", heuristic: "Offline heuristic",
};
const TARGET = "main";

function label(ev: Evaluation, c: string) {
  return ev.criteria_labels?.[c] ?? c.replace(/_/g, " ");
}

/** How much a criterion matters to the team; the weight multiplies its score in the weighted average. */
const PRIORITIES = [
  { id: "ignore", label: "Ignore", weight: 0 },
  { id: "low", label: "Low", weight: 0.5 },
  { id: "normal", label: "Normal", weight: 1 },
  { id: "high", label: "High", weight: 2 },
  { id: "critical", label: "Critical", weight: 3 },
] as const;
type Weights = Record<string, number>;

function nearestPriority(w: number) {
  return PRIORITIES.reduce((best, p) => (Math.abs(p.weight - w) < Math.abs(best.weight - w) ? p : best), PRIORITIES[2]).weight;
}

/** Weighted averages under the given weights, and the alternative that ranks first (ties keep the listed order). */
function rank(ev: Evaluation, weights: Weights) {
  const totals: Record<string, number> = {};
  for (const a of ev.alternatives) {
    const s = ev.scores[a] ?? {};
    let sum = 0, wsum = 0;
    for (const c of ev.criteria) {
      const w = weights[c] ?? 1;
      if (typeof s[c] === "number" && w > 0) { sum += s[c] * w; wsum += w; }
    }
    totals[a] = wsum ? Math.round((sum / wsum) * 100) / 100 : 0;
  }
  const top = ev.alternatives.reduce((best, a) => (totals[a] > totals[best] ? a : best), ev.alternatives[0]);
  return { totals, top };
}

/** Criteria × alternatives score table; the best value in each row is highlighted. Click a criterion for the reasons. */
export function ScoreMatrix({ ev, totals, top }: { ev: Evaluation; totals?: Record<string, number>; top?: string }) {
  const alts = ev.alternatives;
  const best = top ?? ev.recommended;
  const shownTotals = totals ?? ev.totals;
  const [open, setOpen] = useState<string | null>(null);
  const hasReasons = alts.some((a) => Object.keys(ev.details?.[a]?.reasons ?? {}).length > 0);
  return (
    <div className="adr-matrix-wrap">
      <table className="adr-matrix">
        <thead>
          <tr>
            <th>Criterion</th>
            {alts.map((a) => (
              <th key={a} className={a === best ? "rec" : ""} title={a}>
                {a === best && <span aria-label="ranks first" className="rank-star"><Icon as={Star} size={12} /> </span>}{a}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {ev.criteria.map((c) => {
            const vals = alts.map((a) => ev.scores[a]?.[c]).filter((v): v is number => typeof v === "number");
            const max = vals.length ? Math.max(...vals) : null;
            const reasons = alts.map((a) => ev.details?.[a]?.reasons?.[c]).filter(Boolean);
            return (
              <Fragment key={c}>
                <tr className={reasons.length ? "has-why" : ""} onClick={() => reasons.length && setOpen(open === c ? null : c)}
                  title={reasons.length ? "Show why" : undefined}>
                  <td>{reasons.length > 0 && <span className="why-caret">{open === c ? <Icon as={ChevronDown} size={12} /> : <Icon as={ChevronRight} size={12} />} </span>}{label(ev, c)}</td>
                  {alts.map((a) => {
                    const v = ev.scores[a]?.[c];
                    return <td key={a} className={v !== undefined && v === max && vals.length > 1 ? "best" : ""}
                      title={ev.details?.[a]?.reasons?.[c]}>{v ?? "–"}</td>;
                  })}
                </tr>
                {open === c && (
                  <tr className="why-row">
                    <td colSpan={alts.length + 1}>
                      {alts.map((a) => ev.details?.[a]?.reasons?.[c] && (
                        <div key={a} className="small"><b className="mono">{a}</b>
                          {ev.second_opinion?.scores[a]?.[c] !== undefined && <span className="muted"> (AI: {ev.second_opinion.scores[a][c]})</span>}: {ev.details[a].reasons[c]}</div>
                      ))}
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
        <tfoot>
          <tr>
            <td>Weighted avg</td>
            {alts.map((a) => <td key={a} className={a === best ? "best" : ""}>{shownTotals?.[a] ?? "–"}</td>)}
          </tr>
        </tfoot>
      </table>
      <div className="row between small muted">
        <span className={`adr-engine e-${ev.engine}`}>{ENGINE_LABEL[ev.engine] ?? ev.engine}</span>
        <span>1–5, higher is better{hasReasons ? " · click a criterion for why" : ""}{ev.confidence != null ? ` · confidence ${Math.round(ev.confidence * 100)}%` : ""}</span>
      </div>
    </div>
  );
}

/** Alternatives for a decision record: the evaluation's strengths and risks, else strong / weak criteria. */
function alternativesFrom(ev: Evaluation, totals: Record<string, number>): DecisionAlternative[] {
  return ev.alternatives.map((b) => {
    const s = ev.scores[b] ?? {};
    const d = ev.details?.[b];
    return {
      name: b, branch: b,
      summary: [d?.summary, `Weighted score ${totals[b] ?? "–"} / 5 (${ev.engine})`, d?.best_when ? `Best when: ${d.best_when}` : ""]
        .filter(Boolean).join(" "),
      pros: d?.strengths.length ? d.strengths : Object.entries(s).filter(([, v]) => v >= 4).map(([c, v]) => `${label(ev, c)} ${v}/5`),
      cons: d?.risks.length ? d.risks : Object.entries(s).filter(([, v]) => v <= 2).map(([c, v]) => `${label(ev, c)} ${v}/5`),
    };
  });
}

interface CompareProps {
  pid: string;
  branches: Branch[];
  user: User;
  onClose: () => void;
  onRecorded: (d: Decision) => void;
  toast: (text: string) => void;
}

/** Pick 2–4 drafts, let the AI score them, tune the team's priorities, ask follow-ups, and record the decision. */
function CompareDialog({ pid, branches, user, onClose, onRecorded, toast }: CompareProps) {
  const [picked, setPicked] = useState<string[]>(() => branches.filter((b) => b.name !== TARGET).slice(0, 2).map((b) => b.name));
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<Evaluation | null>(null);
  const [weights, setWeights] = useState<Weights>({});
  const [error, setError] = useState("");
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [answers, setAnswers] = useState<{ q: string; a: string; followUps: string[] }[]>([]);
  const [recording, setRecording] = useState(false);
  const ref = useEscape(() => { if (!running && !recording) onClose(); });

  const toggle = (name: string) => {
    setPicked((p) => (p.includes(name) ? p.filter((x) => x !== name) : p.length >= 4 ? p : [...p, name]));
    setResult(null); setAnswers([]);
  };

  const run = async () => {
    setRunning(true); setError(""); setResult(null); setAnswers([]);
    try {
      const ev = await api.evaluate(pid, picked, user.name);
      setResult(ev);
      setWeights(Object.fromEntries(ev.criteria.map((c) => [c, nearestPriority(ev.weights?.[c] ?? 1)])));
    } catch (e) { setError((e as Error).message); }
    finally { setRunning(false); }
  };

  const ranked = useMemo(() => (result ? rank(result, weights) : null), [result, weights]);
  const changedPriorities = !!ranked && ranked.top !== result?.recommended;

  const ask = async (q: string) => {
    const text = q.trim();
    if (!text || !result) return;
    setAsking(true);
    try {
      const priorities = result.criteria.map((c) => `${label(result, c)}: ${PRIORITIES.find((p) => p.weight === weights[c])?.label ?? "Normal"}`).join(", ");
      const { answer, follow_ups } = await api.askComparison(pid, result.alternatives, `${text}\n\n(The team's priorities: ${priorities}.)`,
        { ...result, totals: ranked?.totals ?? result.totals });
      setAnswers((xs) => [...xs, { q: text, a: answer, followUps: follow_ups ?? [] }]);
      setQuestion("");
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
    finally { setAsking(false); }
  };

  const record = async () => {
    if (!result || !ranked) return;
    setRecording(true);
    const chosen = ranked.top;
    const factors = result.deciding_factors?.length ? ` Deciding factors: ${result.deciding_factors.join("; ")}.` : "";
    try {
      const d = await api.createDecision(pid, {
        title: `Choose an architecture: ${result.alternatives.join(" vs ")}`,
        status: "proposed",
        context: `${result.alternatives.length} candidate architectures were compared against the project's requirements. Evaluation engine: ${ENGINE_LABEL[result.engine] ?? result.engine}.`
          + (changedPriorities ? ` The team re-weighted the criteria, which moved ${chosen} ahead of the AI's pick (${result.recommended}).` : ""),
        decision: `Adopt the architecture in branch '${chosen}'. ${chosen === result.recommended ? result.rationale : ""}`.trim() + factors,
        consequences: [result.details?.[chosen]?.risks.length ? `Risks to manage: ${result.details[chosen].risks.join("; ")}.` : "",
          result.flip_conditions ? `Revisit if: ${result.flip_conditions}` : ""].filter(Boolean).join(" "),
        alternatives: alternativesFrom(result, ranked.totals),
        chosen,
        requirement_ids: result.requirement_ids ?? [],
        evaluation: { ...result, raw: undefined, weights, totals: ranked.totals, recommended: chosen },
        actor: user.name,
      });
      toast(`Recorded ${d.id}`);
      onRecorded(d);
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
    finally { setRecording(false); }
  };

  return (
    <div className="modal-backdrop" ref={ref} onClick={() => !running && !recording && onClose()}>
      <div className="modal wide compare" role="dialog" aria-label="Compare architectures" onClick={(e) => e.stopPropagation()}>
        <div className="row between">
          <h3>Compare architectures</h3>
          <button className="ghost" onClick={onClose} aria-label="Close"><Icon as={X} size={16} /></button>
        </div>

        <div className="compare-pick">
          <span className="small muted">Pick 2–4 drafts to score against the requirements:</span>
          <div className="row gap wrap">
            {branches.map((b) => (
              <button key={b.name} className={`chip toggle ${picked.includes(b.name) ? "on" : ""}`} onClick={() => toggle(b.name)}
                disabled={!picked.includes(b.name) && picked.length >= 4} aria-pressed={picked.includes(b.name)}>
                <Icon as={GitBranch} size={12} /> {b.name}
              </button>
            ))}
          </div>
          {branches.length < 2 && <p className="small warn">Create at least two branches (drafts) to compare, e.g. from the design chat.</p>}
          <div className="row gap">
            <button className="primary" disabled={running || picked.length < 2} onClick={run}>
              {running ? "Evaluating…" : result ? "Re-evaluate" : `Compare ${picked.length || ""}`.trim()}
            </button>
            {running && <span className="small muted">The AI is scoring each option on seven criteria…</span>}
          </div>
          {error && <p className="small error">{error}</p>}
        </div>

        {result && ranked && (
          <>
            <div className="compare-rec">
              <div className="row between wrap">
                <b><Icon as={Star} size={12} /> {changedPriorities ? `With your priorities: ${ranked.top}` : `Recommended: ${result.recommended}`}</b>
                {result.confidence != null && <span className="small muted">AI confidence {Math.round(result.confidence * 100)}%</span>}
              </div>
              {changedPriorities && <p className="small warn">The AI recommended <b>{result.recommended}</b> with the requirement-based weights; your priorities put <b>{ranked.top}</b> first.</p>}
              <p className="small">{result.rationale}</p>
              {!!result.deciding_factors?.length && (
                <div className="small"><span className="muted">Deciding factors: </span>{result.deciding_factors.map((f, i) => <span key={i} className="chip">{f}</span>)}</div>
              )}
              {result.second_opinion && (
                <p className={`small opinion ${result.second_opinion.agrees ? "agree" : "disagree"}`}>
                  {result.second_opinion.agrees
                    ? <><Icon as={CircleCheck} size={14} /> Jev and the AI review agree on <b>{result.recommended}</b>.</>
                    : <><Icon as={TriangleAlert} size={14} /> Jev picks <b>{result.recommended}</b>; the AI review would pick <b>{result.second_opinion.recommended}</b>. Ask below why they differ.</>}
                </p>
              )}
              {result.flip_conditions && <p className="small"><span className="muted">What would change this: </span>{result.flip_conditions}</p>}
              {result.notes.length > 0 && <p className="tiny muted">{result.notes.join(" · ")}</p>}
            </div>

            <div className="compare-grid">
              <div className="compare-col">
                <h4>Scores</h4>
                <ScoreMatrix ev={result} totals={ranked.totals} top={ranked.top} />
              </div>
              <div className="compare-col">
                <h4>Your priorities</h4>
                <p className="tiny muted">Pre-set from the non-functional requirements. Change them to see how the ranking moves.</p>
                <div className="prio-list">
                  {result.criteria.map((c) => (
                    <label key={c} className="prio-row small">
                      <span>{label(result, c)}</span>
                      <Select size="sm" value={String(weights[c] ?? 1)} aria-label={`Priority of ${label(result, c)}`}
                        onChange={(v) => setWeights((w) => ({ ...w, [c]: Number(v) }))}
                        options={PRIORITIES.map((p) => ({ value: String(p.weight), label: p.label }))} />
                    </label>
                  ))}
                </div>
              </div>
            </div>

            <div className="compare-cards">
              {result.alternatives.map((a) => {
                const d = result.details?.[a];
                return (
                  <div key={a} className={`compare-card ${a === ranked.top ? "top" : ""}`}>
                    <div className="row between"><b className="mono">{a}</b><span className="small">{ranked.totals[a]} / 5</span></div>
                    {d?.summary && <div className="small">{d.summary}</div>}
                    {!!d?.strengths.length && <ul className="pros small">{d.strengths.map((s, i) => <li key={i}>{s}</li>)}</ul>}
                    {!!d?.risks.length && <ul className="cons small">{d.risks.map((s, i) => <li key={i}>{s}</li>)}</ul>}
                    {d?.best_when && <div className="small"><span className="muted">Best when </span>{d.best_when}</div>}
                  </div>
                );
              })}
            </div>

            <div className="compare-ask">
              <h4>Ask about this comparison</h4>
              {answers.map((x, i) => (
                <div key={i} className="compare-qa">
                  <div className="small"><b>{x.q}</b></div>
                  <Markdown text={x.a} className="small" />
                </div>
              ))}
              {(() => {
                // The AI's suggested next questions: from the evaluation at first, then from the latest answer.
                const next = answers.length ? answers[answers.length - 1].followUps : result.follow_up_questions ?? [];
                const asked = new Set(answers.map((x) => x.q));
                const fresh = next.filter((q) => !asked.has(q));
                return fresh.length > 0 && (
                  <div className="row gap wrap">
                    <span className="tiny muted">{answers.length ? "Ask next" : "Suggested questions"} <span className="ai-tag">AI</span></span>
                    {fresh.map((q) => <button key={q} className="chip toggle" disabled={asking} onClick={() => ask(q)}>{q}</button>)}
                  </div>
                );
              })()}
              {result.engine !== "llm" && <p className="tiny muted">Asking about a comparison needs an AI model (AI settings).</p>}
              <form className="row gap" onSubmit={(e) => { e.preventDefault(); ask(question); }}>
                <input className="grow" value={question} onChange={(e) => setQuestion(e.target.value)} disabled={asking}
                  placeholder={result.engine === "llm" ? "e.g. Which option handles a 10× traffic spike best?" : "Needs an AI model (AI settings)"} aria-label="Question about the comparison" />
                <button disabled={asking || !question.trim()}>{asking ? "Thinking…" : "Ask"}</button>
              </form>
            </div>

            <div className="row gap end">
              <button onClick={onClose}>Close</button>
              <button className="primary" disabled={recording} onClick={record}>{recording ? "Recording…" : `Record decision: adopt ${ranked.top}`}</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export function DecisionsPanel({ pid, branches, user, role, onAdopted, toast }: Props) {
  const canEdit = role === undefined || can(role, "edit");
  const [decisions, setDecisions] = useState<Decision[]>([]);
  const [openId, setOpenId] = useState<string | null>(null);
  const [comparing, setComparing] = useState(false);
  const [error, setError] = useState("");

  const reload = useCallback(() => {
    api.decisions(pid).then(setDecisions).catch((e) => setError((e as Error).message));
  }, [pid]);
  useEffect(reload, [reload]);

  const open = decisions.find((d) => d.id === openId);
  if (open) {
    return (
      <DecisionDetail key={open.id + open.updated_at} pid={pid} decision={open} branches={branches} user={user} role={role}
        toast={toast} onBack={() => setOpenId(null)}
        onSaved={(d) => setDecisions((ds) => ds.map((x) => (x.id === d.id ? d : x)))}
        onAdopted={(r) => { reload(); onAdopted(r); }} />
    );
  }

  return (
    <div className="panel">
      <div className="panel-tools">
        <span className="grow muted small">Architecture decision records</span>
        {canEdit && <button className="primary" onClick={() => setComparing(true)} title="Score 2–4 drafts with AI and record the choice">
          <Icon as={Scale} size={14} /> Compare drafts
        </button>}
      </div>

      {comparing && (
        <CompareDialog pid={pid} branches={branches} user={user} toast={toast} onClose={() => setComparing(false)}
          onRecorded={(d) => { setComparing(false); reload(); setOpenId(d.id); }} />
      )}

      <h4 className="pad">Decisions</h4>
      {decisions.length === 0 && (
        <div className="muted small pad">
          <p>No decisions yet.</p>
          <p>Have two or more draft architectures (branches)? <b>Compare drafts</b> scores them against your requirements, explains
            the trade-offs and records the choice as an ADR you can adopt into main.</p>
        </div>
      )}
      <ul className="adr-list">
        {decisions.map((d) => (
          <li key={d.id} onClick={() => setOpenId(d.id)}>
            <div className="row between">
              <span className="mono small">{d.id}</span>
              <span className={`status adr-s-${d.status}`}>{d.status}</span>
            </div>
            <div className="adr-title">{d.title}</div>
            <div className="muted small">
              {d.chosen ? <>chosen <span className="mono">{d.chosen}</span> · </> : null}by {d.author}
              {d.evaluation ? ` · ${d.evaluation.engine}` : ""}
            </div>
          </li>
        ))}
      </ul>
      {error && <p className="small error pad">{error}</p>}
    </div>
  );
}

interface DetailProps {
  pid: string;
  decision: Decision;
  branches: Branch[];
  user: User;
  role?: Role | null;
  toast: (text: string) => void;
  onBack: () => void;
  onSaved: (d: Decision) => void;
  onAdopted: (r: { version: number; branch: string; source: string; decisionId: string }) => void;
}

function DecisionDetail({ pid, decision: d, branches, user, role, toast, onBack, onSaved, onAdopted }: DetailProps) {
  const canEdit = role === undefined || can(role, "edit");
  const canReview = role === undefined || can(role, "review"); // reviewers may change the status only
  const [form, setForm] = useState({ title: d.title, status: d.status, context: d.context, decision: d.decision, consequences: d.consequences });
  const [saving, setSaving] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [adopting, setAdopting] = useState(false);
  const dirty = (Object.keys(form) as (keyof typeof form)[]).some((k) => form[k] !== d[k]);

  const names = new Set(branches.map((b) => b.name));
  const alt = d.alternatives.find((a) => a.name === d.chosen || a.branch === d.chosen);
  const source = alt?.branch && names.has(alt.branch) ? alt.branch : names.has(d.chosen) ? d.chosen : null;
  const canAdopt = canEdit && !!source && source !== TARGET && (d.status === "proposed" || d.status === "accepted");

  const save = async () => {
    const changes = Object.fromEntries((Object.keys(form) as (keyof typeof form)[])
      .filter((k) => form[k] !== d[k]).map((k) => [k, form[k]]));
    setSaving(true);
    try {
      const out = await api.updateDecision(pid, d.id, { ...changes, actor: user.name });
      onSaved(out);
      toast(`Saved ${d.id}`);
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
    finally { setSaving(false); }
  };

  const adopt = async () => {
    if (!source) return;
    setAdopting(true);
    try {
      const r = await api.adoptDecision(pid, d.id, TARGET, user.name);
      onSaved(r.decision);
      onAdopted({ version: r.version, branch: r.branch, source: r.source_branch, decisionId: d.id });
    } catch (e) { toast(`⚠ ${(e as Error).message}`); }
    finally { setAdopting(false); setConfirm(false); }
  };

  const set = (k: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>) =>
    setForm((f) => ({ ...f, [k]: e.target.value }));

  return (
    <div className="panel adr-detail">
      <div className="panel-tools">
        <button className="small" onClick={onBack}>← All decisions</button>
        <span className="mono small grow">{d.id}</span>
        <span className={`status adr-s-${d.status}`}>{d.status}</span>
      </div>
      <div className="adr-form">
        <label className="field"><span>Title</span><input value={form.title} onChange={set("title")} disabled={!canEdit} /></label>
        <label className="field"><span>Status</span>
          <Select value={form.status} aria-label="Status" disabled={!canReview} options={plainOptions(STATUSES)}
            onChange={(v) => setForm((f) => ({ ...f, status: v }))} />
        </label>
        <label className="field"><span>Context</span><textarea rows={3} value={form.context} onChange={set("context")} disabled={!canEdit} /></label>
        <label className="field"><span>Decision</span><textarea rows={3} value={form.decision} onChange={set("decision")} disabled={!canEdit} /></label>
        <label className="field"><span>Consequences</span><textarea rows={3} value={form.consequences} onChange={set("consequences")} disabled={!canEdit} /></label>
        {canReview && <div className="row gap">
          <button className="primary" disabled={!dirty || saving || !form.title.trim()} onClick={save}>{saving ? "Saving…" : "Save"}</button>
          {dirty && <button onClick={() => setForm({ title: d.title, status: d.status, context: d.context, decision: d.decision, consequences: d.consequences })}>Discard</button>}
        </div>}
      </div>

      {d.alternatives.length > 0 && (
        <>
          <h4 className="pad">Alternatives</h4>
          <ul className="adr-alts">
            {d.alternatives.map((a) => (
              <li key={a.name} className={a === alt ? "chosen" : ""}>
                <div className="row between">
                  <b>{a.name}</b>
                  {a === alt && <span className="chip on">chosen</span>}
                </div>
                {a.branch && a.branch !== a.name && <div className="small muted mono"><Icon as={GitBranch} size={12} /> {a.branch}</div>}
                {a.summary && <div className="small">{a.summary}</div>}
                {(a.pros.length > 0 || a.cons.length > 0) && (
                  <div className="pc small">
                    <ul className="pros">{a.pros.map((p, i) => <li key={i}>{p}</li>)}</ul>
                    <ul className="cons">{a.cons.map((c, i) => <li key={i}>{c}</li>)}</ul>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      {d.evaluation && (
        <>
          <h4 className="pad">Evaluation</h4>
          <div className="pad"><ScoreMatrix ev={d.evaluation} /></div>
        </>
      )}

      {d.requirement_ids.length > 0 && (
        <div className="pad small muted adr-reqs">Requirements: {d.requirement_ids.map((r) => <span key={r} className="chip">{r}</span>)}</div>
      )}

      {canAdopt && (
        <div className="adr-adopt">
          {confirm ? (
            <>
              <p className="small">Replace <b>{TARGET}</b> with <b className="mono">{source}</b>? A safety version of {TARGET} is committed first, so you can restore it.</p>
              <div className="row gap">
                <button className="danger" disabled={adopting} onClick={adopt}>{adopting ? "Adopting…" : "Confirm adopt"}</button>
                <button onClick={() => setConfirm(false)}>Cancel</button>
              </div>
            </>
          ) : (
            <button className="primary" onClick={() => setConfirm(true)}>Adopt into {TARGET}</button>
          )}
        </div>
      )}
      <p className="tiny muted pad">by {d.author} · created {new Date(d.created_at).toLocaleString()}</p>
    </div>
  );
}
