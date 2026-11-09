import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, GitBranch, Icon } from "../icons";
import { api } from "../api";
import { APPROVER_ROLES, can, type Activity, type Branch, type Diff, type ReviewStatus, type Role, type Version, type VersionReview } from "../types";

interface Props {
  pid: string;
  role: Role | null;
  versions: Version[];
  activity: Activity[];
  branch: string;
  headVersion: number | null;
  diff: { from: number; data: Diff } | null;
  onCommit: (message: string) => void;
  onDiff: (v: number | null) => void;
  onRestore: (v: number) => void;
  onBranchFrom: (v: number, name: string) => void;
  /** A review action changed a version's status (refresh the list). */
  onReviewed: () => void;
  branches?: Branch[];
  /** Open the merge dialog for this source branch (absent when the role cannot edit). */
  onMerge?: (source: string) => void;
}

const REVIEW_LABEL: Record<ReviewStatus, string> = {
  draft: "Draft", in_review: "In review", approved: "Approved", changes_requested: "Changes requested",
};
const ACTION_LABEL = { request: "requested review", approve: "approved", request_changes: "requested changes" };

const BRANCH_RE = /^(?=.{1,60}$)[A-Za-z0-9._-]+(?:\/[A-Za-z0-9._-]+)*$/;

function ago(iso: string) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}

/** Saved versions of the architecture (snapshots you can compare, restore or branch from) and the activity log. */
export function VersionsPanel({ pid, role, versions, activity, branch, headVersion, diff, onCommit, onDiff, onRestore, onBranchFrom, onReviewed, branches = [], onMerge }: Props) {
  const [msg, setMsg] = useState("");
  const [open, setOpen] = useState<number | null>(null);
  const [branchName, setBranchName] = useState("experiment/");
  const [confirmRestore, setConfirmRestore] = useState<number | null>(null);
  const [showActivity, setShowActivity] = useState(false);
  const [showBranches, setShowBranches] = useState(false);

  return (
    <div className="panel">
      <form className="panel-head" onSubmit={(e) => { e.preventDefault(); if (msg.trim()) { onCommit(msg.trim()); setMsg(""); } }}>
        <div className="panel-summary small">Save a version of <b>{branch}</b> to compare or roll back later.</div>
        <div className="row gap">
          <input className="grow" placeholder="What changed? e.g. Added caching" value={msg} onChange={(e) => setMsg(e.target.value)} aria-label="Version description" />
          <button className="primary small" disabled={!msg.trim()}>Save version</button>
        </div>
      </form>

      {diff && (
        <div className="diff-box">
          <div className="row between">
            <b>v{diff.from} → now</b>
            <button className="small" onClick={() => onDiff(null)}>Close</button>
          </div>
          <div className="legend small"><span className="lg added">added</span><span className="lg changed">changed</span><span className="lg removed">removed</span></div>
          <pre className="diff-text">{diff.data.text}</pre>
        </div>
      )}

      {versions.length === 0 && <div className="empty-state"><p className="small">No versions yet. Applying a design option or adopting a decision saves one automatically; you can also save one above.</p></div>}
      <ul className="version-list">
        {versions.map((v) => (
          <li key={v.number} className={`${diff?.from === v.number ? "active" : ""} ${open === v.number ? "open" : ""}`}>
            <button className="version-row" onClick={() => setOpen(open === v.number ? null : v.number)} aria-expanded={open === v.number}>
              <b>v{v.number}</b>
              <span className="grow small version-msg">{v.message}</span>
              {v.number === headVersion && <span className="chip on">latest</span>}
              {v.branch !== branch && <span className="chip mono">{v.branch}</span>}
              {v.review && (
                <span className={`status rv-${v.review.status}`} title={`${v.review.approvals} of ${v.review.required} approvals`}>
                  {REVIEW_LABEL[v.review.status]}{v.review.status === "in_review" && v.review.required > 1 ? ` ${v.review.approvals}/${v.review.required}` : ""}
                </span>
              )}
            </button>
            <div className="muted tiny">{v.author} · {ago(v.created_at)}</div>
            {open === v.number && (
              <div className="version-actions">
                <div className="row gap wrap">
                  <button className="small" onClick={() => onDiff(v.number)}>Compare with now</button>
                  {confirmRestore === v.number ? (
                    <>
                      <button className="small danger" onClick={() => { onRestore(v.number); setConfirmRestore(null); }}>Yes, restore v{v.number}</button>
                      <button className="small" onClick={() => setConfirmRestore(null)}>Cancel</button>
                    </>
                  ) : (
                    <button className="small" onClick={() => setConfirmRestore(v.number)} title="Your current state is saved as a version first">Restore</button>
                  )}
                </div>
                <form className="row gap" onSubmit={(e) => { e.preventDefault(); onBranchFrom(v.number, branchName.trim()); setOpen(null); }}>
                  <input value={branchName} onChange={(e) => setBranchName(e.target.value)} aria-label="New branch name" />
                  <button className="small" disabled={!BRANCH_RE.test(branchName.trim())}>New branch from v{v.number}</button>
                </form>
                <ReviewBox pid={pid} role={role} version={v} onReviewed={onReviewed} />
              </div>
            )}
          </li>
        ))}
      </ul>

      {branches.length > 1 && (
        <>
          <button className="section-toggle" onClick={() => setShowBranches(!showBranches)} aria-expanded={showBranches}>
            {showBranches ? <Icon as={ChevronDown} size={12} /> : <Icon as={ChevronRight} size={12} />} Branches <span className="muted small">{branches.length}</span>
          </button>
          {showBranches && (
            <ul className="branch-list">
              {branches.map((b) => (
                <li key={b.name}>
                  <span className="grow small mono"><Icon as={GitBranch} size={12} /> {b.name}</span>
                  {b.name === branch && <span className="chip on">current</span>}
                  {b.base_version !== null && <span className="muted tiny">from v{b.base_version}</span>}
                  {onMerge && <button className="small" onClick={() => onMerge(b.name)} title={`Three-way merge ${b.name} into another branch`}>Merge into…</button>}
                </li>
              ))}
            </ul>
          )}
        </>
      )}

      <button className="section-toggle" onClick={() => setShowActivity(!showActivity)} aria-expanded={showActivity}>
        {showActivity ? <Icon as={ChevronDown} size={12} /> : <Icon as={ChevronRight} size={12} />} Activity <span className="muted small">{activity.length}</span>
      </button>
      {showActivity && (
        <ul className="activity-list">
          {activity.map((a, i) => (
            <li key={i}>
              <span className="small"><b>{a.actor}</b> {a.summary.join("; ")}</span>
              <span className="muted small"> · {ago(a.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * A version's review: its history, and the actions your role allows. Editors request a review (again after changes
 * were requested); reviewers, architects, admins and owners approve or request changes. The server enforces the
 * rules (e.g. you cannot approve a version you saved) and its message is shown here.
 */
function ReviewBox({ pid, role, version, onReviewed }: { pid: string; role: Role | null; version: Version; onReviewed: () => void }) {
  const [review, setReview] = useState<VersionReview | null>(null);
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api.versionReview(pid, version.number).then(setReview).catch((e) => setError(e.message));
  }, [pid, version.number, version.review?.status, version.review?.approvals]);

  const run = async (action: "request-review" | "approve" | "request-changes") => {
    if (action === "request-changes" && !comment.trim()) { setError("Say what needs to change."); return; }
    setBusy(true); setError("");
    try { setReview(await api.reviewVersion(pid, version.number, action, comment.trim())); setComment(""); onReviewed(); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };

  const r = review;
  const canRequest = can(role, "edit") && !!r && (r.status === "draft" || r.status === "changes_requested");
  const canDecide = !!role && APPROVER_ROLES.includes(role) && r?.status === "in_review";
  return (
    <div className="review-box">
      <div className="row between">
        <b className="small">Review</b>
        {r && <span className="muted tiny">{r.approvals.length}/{r.required} approval{r.required === 1 ? "" : "s"}
          {r.approvals.length > 0 && <> · {r.approvals.map((x) => x.name).join(", ")}</>}</span>}
      </div>
      {r && r.history.length > 0 && (
        <ul className="review-history">
          {r.history.map((h) => (
            <li key={h.id} className="small">
              <b>{h.actor_name}</b> {ACTION_LABEL[h.action]} <span className="muted tiny">· {ago(h.created_at)}</span>
              {h.comment && <div className="review-comment">{h.comment}</div>}
            </li>
          ))}
        </ul>
      )}
      {(canRequest || canDecide) && (
        <>
          <textarea rows={2} placeholder={canDecide ? "Comment (required to request changes)" : "Note for reviewers (optional)"}
            value={comment} onChange={(e) => setComment(e.target.value)} aria-label="Review comment" />
          <div className="row gap wrap">
            {canRequest && <button className="small primary" disabled={busy} onClick={() => run("request-review")}>Request review</button>}
            {canDecide && <button className="small primary" disabled={busy} onClick={() => run("approve")}>Approve</button>}
            {canDecide && <button className="small" disabled={busy} onClick={() => run("request-changes")}>Request changes</button>}
          </div>
        </>
      )}
      {r && !canRequest && !canDecide && r.status === "in_review" && <p className="muted tiny">Waiting for a reviewer.</p>}
      {error && <p className="error small">{error}</p>}
    </div>
  );
}
