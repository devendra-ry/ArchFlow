import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import { useEscape } from "../useEscape";
import type { ComplianceControl, ComplianceEffective, ComplianceProposal, ComplianceRemediation, ComplianceReport } from "../types";

interface Props {
  pid: string;
  branch: string;
  /** The branch's revision: the report refreshes when the architecture changes. */
  rev: number;
  open: boolean;
  canEdit: boolean;
  aiAvailable: boolean;
  /** Component id → name, to name the components an AI step changes. */
  names: Record<string, string>;
  onClose: () => void;
  /** Highlight these components on the canvas. */
  onShow: (nodeIds: string[]) => void;
  onAddRequirement: (p: ComplianceProposal) => void;
  toast: (text: string) => void;
}

const LABEL: Record<ComplianceEffective, string> = {
  pass: "Pass", fail: "Fail", needs_evidence: "Needs evidence", not_applicable: "Not applicable", met: "Met by process",
};
type Filter = "open" | "all" | "done";

/** Tools → Compliance: SOC 2, HIPAA, GDPR, PCI DSS and ISO 27001 controls checked against the architecture. */
export function ComplianceDialog({ pid, branch, rev, open, canEdit, aiAvailable, names, onClose, onShow, onAddRequirement, toast }: Props) {
  const [report, setReport] = useState<ComplianceReport | null>(null);
  const [chosen, setChosen] = useState<string[] | null>(null); // null = the project's saved / suggested frameworks
  const [active, setActive] = useState<string>("");
  const [filter, setFilter] = useState<Filter>("open");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [plan, setPlan] = useState<ComplianceRemediation | null>(null);
  const [added, setAdded] = useState<Set<string>>(new Set());
  const escRef = useEscape(onClose, open);

  const load = async (frameworks = chosen) => {
    setBusy((b) => b || "load"); setError("");
    try {
      const r = await api.compliance(pid, branch, frameworks ?? []);
      setReport(r);
      setActive((a) => (r.frameworks.some((f) => f.id === a) ? a : r.frameworks[0]?.id ?? ""));
    } catch (e) { setError((e as Error).message); }
    setBusy((b) => (b === "load" ? "" : b));
  };
  useEffect(() => { if (open) load(); }, [pid, branch, rev, open]); // eslint-disable-line react-hooks/exhaustive-deps

  const selected = chosen ?? report?.selected ?? [];
  const fw = report?.frameworks.find((f) => f.id === active) ?? report?.frameworks[0];
  const controls = useMemo(() => (fw?.controls ?? []).filter((c) =>
    filter === "all" ? true : filter === "open" ? c.effective === "fail" || c.effective === "needs_evidence"
      : c.effective === "pass" || c.effective === "met"), [fw, filter]);
  const dirty = !!report && chosen !== null && chosen.join(",") !== report.selected.join(",");

  const toggle = (id: string) => {
    const next = selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id];
    if (!next.length) return;
    const ordered = (report?.available ?? []).map((a) => a.id).filter((x) => next.includes(x));
    setChosen(ordered); setPlan(null); load(ordered);
  };
  const saveFrameworks = async () => {
    setBusy("save");
    try { await api.saveComplianceFrameworks(pid, selected); setChosen(null); await load(null); toast("Frameworks saved for this project"); }
    catch (e) { setError((e as Error).message); }
    setBusy("");
  };
  const runPlan = async () => {
    setBusy("plan"); setError("");
    try { setPlan(await api.complianceReview(pid, branch, selected, aiAvailable)); }
    catch (e) { setError((e as Error).message); }
    setBusy("");
  };
  const exportFile = async (format: "md" | "json") => {
    try { toast(`Saved ${await api.complianceExport(pid, branch, format, selected)}`); }
    catch (e) { setError((e as Error).message); }
  };
  const printPdf = async () => {
    const win = window.open("", "_blank"); // opened now, so the popup blocker allows it
    try {
      const html = await api.compliancePrintHtml(pid, branch, selected);
      const url = URL.createObjectURL(new Blob([html], { type: "text/html" }));
      if (win) win.location.href = url;
      else Object.assign(document.createElement("a"), { href: url, download: "compliance.html" }).click();
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (e) { win?.close(); setError((e as Error).message); }
  };
  const add = (p: ComplianceProposal) => {
    onAddRequirement(p);
    setAdded((s) => new Set(s).add(p.title.toLowerCase()));
  };

  const proposals = (plan?.requirements.length ? plan.requirements : report?.proposals ?? [])
    .filter((p) => !added.has(p.title.toLowerCase()));

  // Closed: render nothing but stay mounted, so reopening (e.g. after "Show on canvas") keeps the view.
  if (!open) return null;
  return (
    <div className="modal-backdrop" onClick={onClose} ref={escRef}>
      <div className="modal wide compliance" role="dialog" aria-label="Compliance" onClick={(e) => e.stopPropagation()}>
        <div className="row between wrap">
          <h3>Compliance · {branch}</h3>
          <span className="row gap wrap">
            <button className="small" onClick={() => exportFile("md")} disabled={!report}>⤓ Markdown</button>
            <button className="small" onClick={() => exportFile("json")} disabled={!report}>⤓ JSON</button>
            <button className="small" onClick={printPdf} disabled={!report}>⎙ PDF</button>
            <button className="ghost" onClick={onClose} aria-label="close">✕</button>
          </span>
        </div>

        {report && (
          <div className="cmp-frameworks" role="group" aria-label="Frameworks">
            {report.available.map((a) => (
              <label key={a.id} className={`cmp-fw-pick ${selected.includes(a.id) ? "on" : ""}`} title={`${a.version}: ${a.description}`}>
                <input type="checkbox" checked={selected.includes(a.id)} onChange={() => toggle(a.id)} />
                {a.name}{report.suggested.includes(a.id) && !selected.includes(a.id) && <span className="tiny muted"> · suggested</span>}
              </label>
            ))}
            {canEdit && (dirty || !report.saved) && (
              <button className="small" onClick={saveFrameworks} disabled={busy === "save"}
                title="Remember these frameworks for everyone on this project">Save for project</button>
            )}
          </div>
        )}
        {!report && busy && <p className="small muted"><span className="spinner" aria-hidden /> Checking the architecture…</p>}

        {report && (
          <>
            <div className="cmp-scores">
              {report.frameworks.map((f) => (
                <button key={f.id} className={`cmp-score ${f.id === fw?.id ? "active" : ""}`} onClick={() => { setActive(f.id); setExpanded(null); }}
                  aria-pressed={f.id === fw?.id}>
                  <span className="tiny muted">{f.name}</span>
                  <b>{f.score === null ? "–" : `${f.score}%`}</b>
                  <span className="cmp-bar" aria-hidden>
                    {(["pass", "met", "needs_evidence", "fail"] as const).map((k) => f.counts[k] > 0 && (
                      <span key={k} className={`s-${k}`} style={{ flexGrow: f.counts[k] }} />))}
                  </span>
                  <span className="tiny muted">{f.counts.fail} fail · {f.counts.needs_evidence} need evidence</span>
                </button>
              ))}
            </div>

            {fw && (
              <>
                <div className="row between wrap">
                  <p className="small muted cmp-desc">{fw.name} · {fw.version}. {fw.description}</p>
                  <nav className="tabs inline" aria-label="Filter controls">
                    {(["open", "all", "done"] as const).map((f) => (
                      <button key={f} className={filter === f ? "active" : ""} onClick={() => setFilter(f)}>
                        {f === "open" ? "To fix" : f === "all" ? "All" : "Done"}</button>
                    ))}
                  </nav>
                </div>
                {controls.length === 0 && <p className="small muted">{filter === "open" ? "Nothing to fix here." : "No controls."}</p>}
                <ul className="cmp-controls">
                  {controls.map((c) => (
                    <ControlRow key={c.id} c={c} framework={fw.id} pid={pid} canEdit={canEdit} open={expanded === c.id}
                      onToggle={() => setExpanded(expanded === c.id ? null : c.id)} onShow={onShow}
                      onSaved={() => load()} setError={setError} />
                  ))}
                </ul>
              </>
            )}

            <div className="row between wrap cmp-plan-head">
              <h4>{plan ? (plan.engine === "llm" ? "Fix plan (written by AI from these results)" : "Fix plan (from the checks; AI is off)")
                : "Fix plan"}</h4>
              <button className="small primary" onClick={runPlan} disabled={busy === "plan"}
                title={aiAvailable ? "The AI writes steps for this design and suggests requirements" : "AI is off: the plan comes from the checks"}>
                {busy === "plan" ? <><span className="spinner" aria-hidden /> Writing…</> : plan ? "Write again" : aiAvailable ? "Write a fix plan with AI" : "Show fix plan"}
              </button>
            </div>
            {plan && (
              <>
                {plan.summary && <p className="small">{plan.summary}</p>}
                <ol className="cmp-steps">
                  {plan.steps.map((s, i) => (
                    <li key={i}>
                      <div className="row between wrap"><b className="small">{s.title}</b><span className="tiny muted">effort {s.effort}</span></div>
                      <div className="small">{s.detail}</div>
                      <div className="tiny muted row gap wrap">
                        <span>{s.controls.join(", ")}</span>
                        {s.components.length > 0 && (
                          <button className="link" onClick={() => onShow(s.components)}>
                            Show {s.components.map((id) => names[id] ?? id).join(", ")}</button>
                        )}
                      </div>
                    </li>
                  ))}
                </ol>
                {plan.questions.length > 0 && <p className="small muted">Open questions: {plan.questions.join(" ")}</p>}
              </>
            )}

            {proposals.length > 0 && (
              <>
                <h4>Suggested requirements</h4>
                <ul className="cmp-proposals">
                  {proposals.map((p) => (
                    <li key={p.key}>
                      <div className="row between wrap">
                        <b className="small">{p.title}</b>
                        {canEdit && <button className="small" onClick={() => add(p)}>+ Add as requirement</button>}
                      </div>
                      <div className="small">{p.description}</div>
                      {p.acceptance_criteria.length > 0 && (
                        <ul className="tiny muted cmp-criteria">{p.acceptance_criteria.map((a, i) => <li key={i}>{a}</li>)}</ul>
                      )}
                      <div className="tiny muted">{p.priority} · {p.category}{p.controls.length ? ` · closes ${p.controls.join(", ")}` : ""}</div>
                    </li>
                  ))}
                </ul>
              </>
            )}
            <p className="tiny muted">{report.disclaimer}</p>
          </>
        )}
        {error && <p className="small error">{error}</p>}
      </div>
    </div>
  );
}

function ControlRow({ c, framework, pid, canEdit, open, onToggle, onShow, onSaved, setError }: {
  c: ComplianceControl; framework: string; pid: string; canEdit: boolean; open: boolean;
  onToggle: () => void; onShow: (ids: string[]) => void; onSaved: () => void; setError: (e: string) => void;
}) {
  const [status, setStatus] = useState<"" | "met" | "not_applicable">(c.evidence?.status ?? "");
  const [note, setNote] = useState(c.evidence?.note ?? "");
  const [saving, setSaving] = useState(false);
  useEffect(() => { setStatus(c.evidence?.status ?? ""); setNote(c.evidence?.note ?? ""); }, [c.evidence]);
  const changed = status !== (c.evidence?.status ?? "") || note.trim() !== (c.evidence?.note ?? "");

  const save = async () => {
    setSaving(true);
    try { await api.setComplianceEvidence(pid, framework, c.id, status, note.trim()); onSaved(); }
    catch (e) { setError((e as Error).message); }
    setSaving(false);
  };

  return (
    <li className={`cmp-control ${open ? "open" : ""}`}>
      <button className="cmp-row" onClick={onToggle}
        aria-expanded={open}>
        <span className={`cmp-chip s-${c.effective}`}>{LABEL[c.effective]}</span>
        <span className="cmp-title"><b className="small">{c.id}</b> <span className="small">{c.title}</span>
          {c.component_names.length > 0 && <span className="tiny muted cmp-comps">{c.component_names.join(", ")}</span>}</span>
      </button>
      {open && (
        <div className="cmp-detail">
          <div className="small muted">{c.description}</div>
          <div className="small">{c.summary}</div>
          {c.effective === "met" && c.status !== "pass" && (
            <div className="tiny muted">The design check says: {LABEL[c.status]}.</div>
          )}
          {c.fixes.length > 0 && <div className="small"><b>Fix:</b> {c.fixes.join(" ")}</div>}
          <div className="tiny muted row gap wrap">
            {c.requirements.length > 0 && <span>Requirements: {c.requirements.join(", ")}</span>}
            {c.components.length > 0 && <button className="link" onClick={() => onShow(c.components)}>Show on canvas</button>}
          </div>
          {canEdit ? (
            <div className="cmp-evidence">
              <label className="small">Evidence
                <select value={status} onChange={(e) => setStatus(e.target.value as typeof status)}>
                  <option value="">Use the design check</option>
                  <option value="met">Met by process</option>
                  <option value="not_applicable">Not applicable</option>
                </select>
              </label>
              <textarea rows={2} placeholder="Note: a policy, a ticket or how this is met" value={note} maxLength={2000}
                onChange={(e) => setNote(e.target.value)} aria-label="Evidence note" />
              <div className="row gap end">
                {c.evidence?.updated_by && <span className="tiny muted">Last set by {c.evidence.updated_by}</span>}
                <button className="small primary" onClick={save} disabled={!changed || saving}>{saving ? "Saving…" : "Save"}</button>
              </div>
            </div>
          ) : c.evidence && (
            <div className="small">Evidence ({c.evidence.status ? LABEL[c.evidence.status] : "note"}, {c.evidence.updated_by}): {c.evidence.note || "–"}</div>
          )}
        </div>
      )}
    </li>
  );
}
