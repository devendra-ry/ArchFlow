import type { Activity, Analysis, ArchOption, AuthImpact, Branch, ChatCard, ChatTurn, ApiToken, Catalog, Diff, Op, Org, Project, ProjectState, Version } from "./types";
import type { AppNotification, Comment, CommentTarget, Decision, Evaluation } from "./types";
import type { AIConfig, AIGroup, AIProvider, AISettings, AITestResult, Invite, InviteInfo, Me, Member, Role } from "./types";
import type { ComponentViews, IntakeNote, IntakeResult, IntakeStep, LldResult, RequirementRevision, MergePreview, MergeResolution, MergeResult, MergeSuggestion, Diagram, DiagramInfo } from "./types";
import type { CodeGraphRecord, CodeJob, CodeOverview, DriftResult, ImpactResult, ReconstructResult } from "./types";
import type { Conversation, ConversationKind, ConversationMessage, MemoryCategory, MemoryItem } from "./types";
import type { OpenSpecChange, OpenSpecJob, OpenSpecOverview } from "./types";
import type { ApprovalPolicy, VersionReview } from "./types";
import type { AIUsageReport, AiSuggestion, AiTrace, AiTraceList, PromptInfo, ReviewResult, SuggestionMode } from "./types";
import type { JiraImportResult, JiraIssuePage, JiraLinks, JiraPlan, JiraPushResult, JiraSettings, JiraTestResult, JiraWebhookSetup } from "./types";
import type { ComplianceRemediation, ComplianceReport } from "./types";
import type { ConfluencePublish, CostEstimate, ExternalLinks, GitHubPlan, GitHubPushResult, IntegrationKind, IntegrationSettings } from "./types";
import type { KbAnswer, KbDocument, KbPassage, KbStatus, MeetingDecision, MeetingResult, MeetingStep } from "./types";
import type { AuthSettings, MfaChallenge, MfaStatus, ProjectRole, ProjectRoles } from "./types";
import type { GeneratedFile, GenerationKind, GenerationResult, GenerationTarget } from "./types";

/** POST a JSON body and save the file the server answers with (zip downloads). */
async function downloadPost(path: string, body: unknown, fallbackName: string): Promise<string> {
  const res = await fetch(`/api${path}`, {
    method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    throw new ApiError(res.status, data);
  }
  const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") ?? "")?.[1] ?? fallbackName;
  const url = URL.createObjectURL(await res.blob());
  Object.assign(document.createElement("a"), { href: url, download: name }).click();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
  return name;
}

export class ApiError extends Error {
  constructor(public status: number, public data: any) {
    super(typeof data?.detail === "string" ? data.detail : defaultMessage(status));
  }
}

/** A plain message when the server sent no detail (502/503/504: the API is down or restarting behind the proxy). */
function defaultMessage(status: number): string {
  if (status === 502 || status === 503 || status === 504) return "Can't reach the ArchFlow server. Check that it is running and try again.";
  if (status === 429) return "Too many requests. Wait a moment and try again.";
  if (status >= 500) return `The server hit an error (${status}). Try again.`;
  return `Request failed (${status})`;
}

/** Fired on any 401 so the app can show the sign-in screen (session expired or signed out elsewhere). */
export const UNAUTHORIZED_EVENT = "archflow:unauthorized";

/** Upload a file as the raw request body (the code-graph JSON can be large; no parsing in the browser). */
async function upload<T>(path: string, file: Blob, params?: Record<string, string>, type = "application/json"): Promise<T> {
  const qs = params && Object.keys(params).length ? "?" + new URLSearchParams(params).toString() : "";
  const res = await fetch(`/api${path}${qs}`, {
    method: "POST", credentials: "include", headers: { "Content-Type": type }, body: file,
  });
  const data = await res.json().catch(() => null);
  if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}

async function call<T>(method: string, path: string, body?: unknown, params?: Record<string, string>): Promise<T> {
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  const res = await fetch(`/api${path}${qs}`, {
    method,
    credentials: "include", // the session is an HttpOnly cookie
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (res.status === 401 && !path.startsWith("/auth/")) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
  if (!res.ok) throw new ApiError(res.status, data);
  return data as T;
}

const b = (branch: string) => ({ branch });

/** GET a file (the session cookie is sent) and save it under the server's suggested name. */
async function download(path: string, params: Record<string, string>, fallbackName: string): Promise<string> {
  const res = await fetch(`/api${path}?${new URLSearchParams(params)}`, { credentials: "include" });
  if (!res.ok) {
    const data = await res.json().catch(() => null);
    if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    throw new ApiError(res.status, data);
  }
  const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") ?? "")?.[1] ?? fallbackName;
  const url = URL.createObjectURL(await res.blob());
  Object.assign(document.createElement("a"), { href: url, download: name }).click();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
  return name;
}

export interface StreamHandlers {
  onStart?: (userMessage: ConversationMessage) => void;
  onDelta: (text: string) => void;
}

/**
 * POST that answers with server-sent events (start → delta… → done). Resolves with the saved assistant message.
 * Aborting stops reading; the server still finishes and saves the answer.
 */
async function stream(path: string, body: unknown, params: Record<string, string>, handlers: StreamHandlers,
  signal?: AbortSignal): Promise<ConversationMessage> {
  const res = await fetch(`/api${path}?${new URLSearchParams(params)}`, {
    method: "POST", credentials: "include", signal,
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  for await (const { event, data } of sseEvents(res)) {
    if (event === "start") handlers.onStart?.(data.user_message);
    else if (event === "delta") handlers.onDelta(data.text);
    else if (event === "done") return data.message as ConversationMessage;
  }
  throw new Error("The answer stream ended early. Reload the conversation to see the saved answer.");
}

/** The server-sent events of a response; throws ApiError when the request itself failed. */
async function* sseEvents(res: Response): AsyncGenerator<{ event: string; data: any }> {
  if (!res.ok || !res.body) {
    const data = await res.json().catch(() => null);
    if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
    throw new ApiError(res.status, data);
  }
  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buf += value;
    let cut: number;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const block = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      const event = /^event: (.*)$/m.exec(block)?.[1];
      const raw = /^data: (.*)$/m.exec(block)?.[1];
      if (event && raw !== undefined) yield { event, data: JSON.parse(raw) };
    }
  }
}

/**
 * Upload a document and follow the server's progress events until the proposal arrives.
 * Aborting closes the connection, which cancels the import on the server.
 */
async function importStream(pid: string, branch: string, file: File, onProgress: (step: IntakeStep) => void,
  signal?: AbortSignal): Promise<IntakeResult> {
  return uploadStream<IntakeResult, IntakeStep>(`/api/projects/${pid}/requirements/import`, { filename: file.name, branch },
    file, onProgress, signal);
}

/** Upload a meeting transcript or recording and follow the progress until the proposal arrives (abort cancels). */
async function meetingStream(pid: string, branch: string, file: File, knowledge: boolean,
  onProgress: (step: MeetingStep) => void, signal?: AbortSignal): Promise<MeetingResult> {
  return uploadStream<MeetingResult, MeetingStep>(`/api/projects/${pid}/meetings/import`,
    { filename: file.name, branch, knowledge: String(knowledge) }, file, onProgress, signal);
}

async function uploadStream<T, S>(url: string, params: Record<string, string>, file: File, onProgress: (step: S) => void,
  signal?: AbortSignal): Promise<T> {
  const res = await fetch(`${url}?${new URLSearchParams(params)}`, {
    method: "POST", credentials: "include", signal, body: file,
    headers: { "Content-Type": "application/octet-stream", Accept: "text/event-stream" },
  });
  // A server that does not stream answers with the proposal as plain JSON.
  if (res.ok && res.headers.get("Content-Type")?.includes("application/json")) return await res.json() as T;
  for await (const { event, data } of sseEvents(res)) {
    if (event === "progress") onProgress(data as S);
    else if (event === "done") return data as T;
    else if (event === "error") throw new ApiError(data.status, { detail: data.message });
    else if (event === "cancelled") throw new DOMException("The import was cancelled.", "AbortError");
  }
  throw new Error("The connection closed before the document was read. Try again.");
}

export const api = {
  // ---- accounts and organizations ----
  me: () => call<Me>("GET", "/me"),
  signup: (email: string, name: string, password: string, invite?: string) =>
    call<Me>("POST", "/auth/signup", { email, name, password, invite }),
  /** A session, or (two-step sign-in on) a challenge to answer with mfaVerify. */
  login: (email: string, password: string) => call<Me | MfaChallenge>("POST", "/auth/login", { email, password }),
  logout: () => call<null>("POST", "/auth/logout"),
  inviteInfo: (token: string) => call<InviteInfo>("GET", `/invites/${encodeURIComponent(token)}`),
  acceptInvite: (token: string) => call<Org>("POST", `/invites/${encodeURIComponent(token)}/accept`),
  updateOrg: (org: string, name: string) => call<Org>("PATCH", `/orgs/${org}`, { name }),
  members: (org: string) => call<Member[]>("GET", `/orgs/${org}/members`),
  updateMember: (org: string, userId: string, role: Role) => call<Member>("PATCH", `/orgs/${org}/members/${userId}`, { role }),
  removeMember: (org: string, userId: string) => call<null>("DELETE", `/orgs/${org}/members/${userId}`),
  invites: (org: string) => call<Invite[]>("GET", `/orgs/${org}/invites`),
  createInvite: (org: string, role: Role, email?: string) =>
    call<Invite & { token: string; link: string }>("POST", `/orgs/${org}/invites`, { role, email: email || null }),
  revokeInvite: (org: string, id: string) => call<null>("DELETE", `/orgs/${org}/invites/${id}`),
  aiSettings: (org: string) => call<AISettings>("GET", `/orgs/${org}/ai-settings`),
  saveAiSettings: (org: string, keys: Record<AIProvider, string | null>) =>
    call<AISettings>("PUT", `/orgs/${org}/ai-settings`, keys),
  saveAiConfig: (org: string, config: AIConfig) => call<AISettings>("PUT", `/orgs/${org}/ai-config`, config),
  testAi: (org: string, group: AIGroup) => call<AITestResult>("POST", `/orgs/${org}/ai-test`, { group }),
  aiTraces: (org: string, filters: Record<string, string> = {}) =>
    call<AiTraceList>("GET", `/orgs/${org}/ai/traces`, undefined, filters),
  aiTrace: (org: string, id: string) => call<AiTrace>("GET", `/orgs/${org}/ai/traces/${id}`),
  aiPrompts: (org: string) => call<{ prompts: PromptInfo[]; problems: string[] }>("GET", `/orgs/${org}/ai/prompts`),
  aiUsage: (org: string, days = 30) => call<AIUsageReport>("GET", `/orgs/${org}/ai-usage`, undefined, { days: String(days) }),
  setAiBudget: (org: string, monthly_limit_usd: number | null, alert_percent: number) =>
    call<AIUsageReport>("PUT", `/orgs/${org}/ai-budget`, { monthly_limit_usd, alert_percent }),
  collabTicket: (pid: string, branch: string, color: string) =>
    call<{ ticket: string; doc: string; read_only: boolean; expires_in: number }>(
      "GET", `/projects/${pid}/collab-ticket`, undefined, { branch, color }),

  health: () => call<{ ok: boolean; ai_mode: string; model: string; llm?: { available: boolean; groups: Record<string, string[]> } }>("GET", "/health"),
  catalog: () => call<Catalog>("GET", "/catalog"),
  convert: (pid: string, branch: string, target: string, actor: string) =>
    call<{ changed: number; summary: string[]; skipped: string[] }>("POST", `/projects/${pid}/convert`, { target, actor }, b(branch)),
  templates: () => call<{ key: string; name: string }[]>("GET", "/templates"),
  orgs: () => call<Org[]>("GET", "/orgs"),
  mcpInfo: () => call<{ url: string; issuer: string; scopes: string[] }>("GET", "/mcp-info"),
  tokens: (org: string) => call<ApiToken[]>("GET", `/orgs/${org}/tokens`),
  authImpact: (org: string, rules: { require_mfa?: boolean; require_sso?: boolean } = {}) =>
    call<AuthImpact>("GET", `/orgs/${org}/auth-settings/impact`, undefined,
      Object.fromEntries(Object.entries(rules).filter(([, v]) => v !== undefined).map(([k, v]) => [k, String(v)]))),
  revokeBlockedTokens: (org: string) => call<{ revoked: number }>("POST", `/orgs/${org}/auth-settings/revoke-blocked-tokens`),
  createToken: (org: string, name: string, access: "read" | "write", expires_in_days: number | null, project_ids: string[] | null = null) =>
    call<ApiToken & { token: string }>("POST", `/orgs/${org}/tokens`, { name, access, expires_in_days, project_ids }),
  revokeToken: (org: string, id: string) => call<null>("DELETE", `/orgs/${org}/tokens/${id}`),
  createOrg: (name: string) => call<Org>("POST", "/orgs", { name }),
  projects: (org_id?: string) => call<Project[]>("GET", "/projects", undefined, org_id ? { org_id } : undefined),
  createProject: (name: string, template: string, org_id: string, description = "") =>
    call<Project>("POST", "/projects", { name, template, description, org_id }),
  deleteProject: (pid: string) => call<null>("DELETE", `/projects/${pid}`),
  project: (pid: string, branch: string) =>
    call<{ project: Project; branch: string; branches: Branch[]; head_version: number | null; state: ProjectState;
      role: Role; ai_available: boolean }>(
      "GET", `/projects/${pid}`, undefined, b(branch)),
  ops: (pid: string, branch: string, ops: Op[], actor: string) =>
    call<{ state: ProjectState; summary: string[] }>("POST", `/projects/${pid}/ops`, { ops, actor }, b(branch)),
  commit: (pid: string, branch: string, message: string, author: string) =>
    call<Version>("POST", `/projects/${pid}/commit`, { message, author }, b(branch)),
  versions: (pid: string) => call<Version[]>("GET", `/projects/${pid}/versions`),
  activity: (pid: string, branch: string) => call<Activity[]>("GET", `/projects/${pid}/activity`, undefined, b(branch)),
  diff: (pid: string, branch: string, from: string, to = "working") =>
    call<Diff>("GET", `/projects/${pid}/diff`, undefined, { branch, from, to }),
  restore: (pid: string, branch: string, version: number, actor: string) =>
    call<{ state: ProjectState }>("POST", `/projects/${pid}/restore`, { version, actor }, b(branch)),
  createBranch: (pid: string, name: string, from_branch: string, from_version: number | null) =>
    call<{ name: string }>("POST", `/projects/${pid}/branches`, { name, from_branch, from_version }),
  // ---- branch merge ----
  mergePreview: (pid: string, source: string, target: string) =>
    call<MergePreview>("POST", `/projects/${pid}/merge/preview`, { source, target }),
  /** AI-proposed resolution per conflict; engine "none" when no AI is configured. */
  mergeSuggest: (pid: string, source: string, target: string) =>
    call<{ suggestions: MergeSuggestion[]; engine: "llm" | "none" }>("POST", `/projects/${pid}/merge/suggest`, { source, target }),
  mergeApply: (pid: string, source: string, target: string, resolutions: MergeResolution[], source_rev: number, target_rev: number) =>
    call<MergeResult>("POST", `/projects/${pid}/merge/apply`, { source, target, resolutions, source_rev, target_rev }),
  analysis: (pid: string, branch: string) => call<Analysis>("GET", `/projects/${pid}/analysis`, undefined, b(branch)),
  requirementHistory: (pid: string, branch: string, rid: string) =>
    call<RequirementRevision[]>("GET", `/projects/${pid}/requirements/${encodeURIComponent(rid)}/history`, undefined, b(branch)),
  /** Admins: remove a deleted requirement permanently (its history is kept). */
  purgeRequirement: (pid: string, branch: string, rid: string) =>
    call<{ summary: string[]; rev: number }>("DELETE", `/projects/${pid}/requirements/${encodeURIComponent(rid)}`, undefined, b(branch)),
  promptTargets: () => call<{ key: string; name: string }[]>("GET", "/prompt-targets"),
  prompt: (pid: string, branch: string, node_ids: string[], requirement_ids: string[], target: string, extra: string) =>
    call<{ prompt: string }>("POST", `/projects/${pid}/prompt`, { node_ids, requirement_ids, target, extra }, b(branch)),
  // ---- OpenSpec ----
  openspec: (pid: string) => call<OpenSpecOverview>("GET", `/projects/${pid}/openspec`),
  openspecNew: (pid: string, branch: string, node_ids: string[], requirement_ids: string[], target: string, extra: string, use_ai: boolean) =>
    call<OpenSpecJob>("POST", `/projects/${pid}/openspec/changes`, { node_ids, requirement_ids, target, extra, use_ai }, b(branch)),
  openspecJob: (pid: string, id: string) => call<OpenSpecJob>("GET", `/projects/${pid}/openspec/jobs/${id}`),
  openspecChange: (pid: string, cid: string, target: string) =>
    call<{ change: OpenSpecChange; dir: string; files: Record<string, string>; prompt: string }>(
      "GET", `/projects/${pid}/openspec/changes/${cid}`, undefined, { target }),
  openspecSaveFile: (pid: string, cid: string, path: string, content: string) =>
    call<OpenSpecChange>("PUT", `/projects/${pid}/openspec/changes/${cid}/files`, { path, content }),
  openspecValidate: (pid: string, cid: string) => call<OpenSpecChange>("POST", `/projects/${pid}/openspec/changes/${cid}/validate`),
  openspecArchive: (pid: string, cid: string) => call<OpenSpecChange>("POST", `/projects/${pid}/openspec/changes/${cid}/archive`),
  openspecDelete: (pid: string, cid: string) => call<null>("DELETE", `/projects/${pid}/openspec/changes/${cid}`),
  openspecFile: (pid: string, path: string) =>
    call<{ path: string; content: string; updated_by: string; updated_at: string }>("GET", `/projects/${pid}/openspec/file`, undefined, { path }),
  openspecZip: (pid: string) => download(`/projects/${pid}/openspec.zip`, {}, "openspec.zip"),
  command: (pid: string, branch: string, text: string, selection: string[], actor: string) =>
    call<{
      applied: boolean; summary?: string[]; explanation: string; questions: string[];
      engine: string; error?: string; state?: ProjectState;
    }>("POST", `/projects/${pid}/ai/command`, { text, selection, actor }, b(branch)),
  /** One design-chat turn. With ``conversation_id`` the server keeps the transcript, so send only the new turn. */
  chat: (pid: string, branch: string, turns: ChatTurn[], actor: string, conversation_id?: string) =>
    call<{ message: string; card: ChatCard | null; engine: string; user_message?: ConversationMessage;
      assistant_message?: ConversationMessage }>("POST", `/projects/${pid}/chat`, { turns, actor, conversation_id }, b(branch)),
  chatApply: (pid: string, branch: string, option: ArchOption, requirements: Record<string, unknown>[],
    mode: "replace" | "merge" | "branch", actor: string, branch_name?: string, conversation_id?: string) =>
    call<{ branch: string; version: number; summary: string[]; state: ProjectState; conversation?: Conversation }>(
      "POST", `/projects/${pid}/chat/apply`, { option, requirements, mode, actor, branch_name, conversation_id }, b(branch)),
  // ---- conversations and project memory ----
  conversations: (pid: string, kind?: ConversationKind) =>
    call<Conversation[]>("GET", `/projects/${pid}/conversations`, undefined, kind ? { kind } : undefined),
  conversation: (pid: string, cid: string) =>
    call<Conversation & { messages: ConversationMessage[] }>("GET", `/projects/${pid}/conversations/${cid}`),
  createConversation: (pid: string, branch: string, kind: ConversationKind, messages: ChatTurn[] = [], title = "") =>
    call<Conversation>("POST", `/projects/${pid}/conversations`, { kind, messages, title }, b(branch)),
  renameConversation: (pid: string, cid: string, title: string) =>
    call<Conversation>("PATCH", `/projects/${pid}/conversations/${cid}`, { title }),
  deleteConversation: (pid: string, cid: string) => call<null>("DELETE", `/projects/${pid}/conversations/${cid}`),
  clearConversation: (pid: string, cid: string) => call<null>("DELETE", `/projects/${pid}/conversations/${cid}/messages`),
  askStream: (pid: string, branch: string, cid: string, question: string, handlers: StreamHandlers, signal?: AbortSignal) =>
    stream(`/projects/${pid}/conversations/${cid}/ask`, { question }, b(branch), handlers, signal),
  regenerateStream: (pid: string, branch: string, cid: string, handlers: StreamHandlers, signal?: AbortSignal) =>
    stream(`/projects/${pid}/conversations/${cid}/regenerate`, undefined, b(branch), handlers, signal),
  conversationCommand: (pid: string, branch: string, cid: string, text: string, selection: string[]) =>
    call<{ user_message: ConversationMessage; message: ConversationMessage; state?: ProjectState }>(
      "POST", `/projects/${pid}/conversations/${cid}/command`, { text, selection }, b(branch)),
  memory: (pid: string) => call<MemoryItem[]>("GET", `/projects/${pid}/memory`),
  addMemory: (pid: string, branch: string, text: string, category: MemoryCategory, pinned = false) =>
    call<MemoryItem>("POST", `/projects/${pid}/memory`, { text, category, pinned }, b(branch)),
  updateMemory: (pid: string, branch: string, id: string, changes: Partial<Pick<MemoryItem, "text" | "category" | "pinned">>) =>
    call<MemoryItem>("PATCH", `/projects/${pid}/memory/${id}`, changes, b(branch)),
  deleteMemory: (pid: string, branch: string, id: string) => call<null>("DELETE", `/projects/${pid}/memory/${id}`, undefined, b(branch)),
  /** Specialist agents review the design in parallel (security, reliability, performance, cost). */
  review: (pid: string, branch: string) => call<ReviewResult>("POST", `/projects/${pid}/ai/review`, undefined, b(branch)),
  /** LLM suggestions (starting prompts or follow-ups); engine "none" when no AI is configured. */
  suggestions: (pid: string, branch: string, body: { mode: SuggestionMode; selection?: string[]; conversation_id?: string | null;
    last_question?: string; last_answer?: string }) =>
    call<{ suggestions: AiSuggestion[]; engine: "llm" | "none"; cached?: boolean }>("POST", `/projects/${pid}/ai/suggestions`, body, b(branch)),
  ask: (pid: string, branch: string, question: string) =>
    call<{ answer: string }>("POST", `/projects/${pid}/ai/ask`, { question }, b(branch)),
  // ---- comments and notifications ----
  comments: (pid: string, branch: string) => call<Comment[]>("GET", `/projects/${pid}/comments`, undefined, b(branch)),
  addComment: (pid: string, branch: string, data: { body: string; target_type?: CommentTarget; target_id?: string; parent_id?: string; mentions?: string[] }) =>
    call<Comment>("POST", `/projects/${pid}/comments`, data, b(branch)),
  updateComment: (pid: string, id: string, changes: { body?: string; mentions?: string[]; resolved?: boolean }) =>
    call<Comment>("PATCH", `/projects/${pid}/comments/${id}`, changes),
  deleteComment: (pid: string, id: string) => call<null>("DELETE", `/projects/${pid}/comments/${id}`),
  notifications: () => call<{ items: AppNotification[]; unread: number }>("GET", "/notifications"),
  readNotifications: (ids?: number[]) => call<{ unread: number }>("POST", "/notifications/read", { ids: ids ?? null }),

  // ---- version approvals ----
  versionReview: (pid: string, version: number) => call<VersionReview>("GET", `/projects/${pid}/versions/${version}/review`),
  reviewVersion: (pid: string, version: number, action: "request-review" | "approve" | "request-changes", comment = "") =>
    call<VersionReview>("POST", `/projects/${pid}/versions/${version}/${action}`, { comment }),
  approvalPolicy: (org: string) => call<ApprovalPolicy>("GET", `/orgs/${org}/approval-policy`),
  setApprovalPolicy: (org: string, required_approvals: number, require_for_adopt: boolean) =>
    call<ApprovalPolicy>("PUT", `/orgs/${org}/approval-policy`, { required_approvals, require_for_adopt }),

  // ---- architecture decisions ----
  decisions: (pid: string) => call<Decision[]>("GET", `/projects/${pid}/decisions`),
  decision: (pid: string, id: string) => call<Decision>("GET", `/projects/${pid}/decisions/${id}`),
  createDecision: (pid: string, data: Partial<Decision> & { title: string; actor: string }) =>
    call<Decision>("POST", `/projects/${pid}/decisions`, data),
  updateDecision: (pid: string, id: string, changes: Partial<Decision> & { actor: string }) =>
    call<Decision>("PATCH", `/projects/${pid}/decisions/${id}`, changes),
  adoptDecision: (pid: string, id: string, target_branch: string, actor: string) =>
    call<{ version: number; safety_version: number; branch: string; source_branch: string; decision: Decision }>(
      "POST", `/projects/${pid}/decisions/${id}/adopt`, { target_branch, actor }),
  evaluate: (pid: string, branches: string[], actor: string, create_decision = false) =>
    call<Evaluation & { decision?: Decision }>("POST", `/projects/${pid}/evaluate`, { branches, actor, create_decision }),
  askComparison: (pid: string, branches: string[], question: string, evaluation: Evaluation | null) =>
    call<{ answer: string; follow_ups: string[] }>("POST", `/projects/${pid}/evaluate/ask`, { branches, question, evaluation: evaluation ? { ...evaluation, raw: undefined } : null }),
  generateLld: (pid: string, branch: string, nodeId: string, actor: string, replace = false) =>
    call<LldResult>("POST", `/projects/${pid}/nodes/${encodeURIComponent(nodeId)}/lld`, { actor, replace }, b(branch)),
  /** Interactive diagram of one level (system, or a component's internals), rendered by Archify on the server. */
  exportDiagram: (pid: string, branch: string, level = "") =>
    download(`/projects/${pid}/export/diagram`, { branch, level }, "diagram.html"),
  /** Spec exports derived from the branch: Markdown, print-ready HTML (Save as PDF), full project JSON / YAML. */
  exportSpec: (pid: string, branch: string, format: "spec.md" | "spec.html" | "project.json" | "project.yaml") =>
    download(`/projects/${pid}/export/${format}`, { branch }, format),
  /** The print-ready spec as HTML text, to open in a new tab and print to PDF. */
  exportPrintHtml: async (pid: string, branch: string): Promise<string> => {
    const res = await fetch(`/api/projects/${pid}/export/spec.html?${new URLSearchParams({ branch, inline: "true" })}`,
      { credentials: "include" });
    if (!res.ok) {
      if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
      throw new ApiError(res.status, await res.json().catch(() => null));
    }
    return res.text();
  },
  /** CLAUDE.md, AGENTS.md and .cursorrules: derived skeleton, plus AI-written guidance when ``use_ai``. */
  exportGuides: (pid: string, branch: string, use_ai: boolean) =>
    call<{ files: Record<string, string>; engine: "llm" | "offline"; notes: string[] }>(
      "POST", `/projects/${pid}/export/guides`, { use_ai }, b(branch)),
  exportSequence: (pid: string, branch: string, nodeId: string) =>
    download(`/projects/${pid}/nodes/${encodeURIComponent(nodeId)}/export/sequence`, { branch }, "request-flow.html"),
  views: (pid: string, branch: string, nodeId: string) =>
    call<ComponentViews>("GET", `/projects/${pid}/nodes/${encodeURIComponent(nodeId)}/views`, undefined, b(branch)),
  /** Diagrams derived from the model (C4, sequence per flow, ER, deployment, data flow) with alt text; ai = let the AI name the flows. */
  diagrams: (pid: string, branch: string, ai = false) =>
    call<{ branch: string; diagrams: DiagramInfo[] }>("GET", `/projects/${pid}/diagrams`, undefined, { branch, ai: String(ai) }),
  diagram: (pid: string, branch: string, id: string, ai = false) =>
    call<Diagram>("GET", `/projects/${pid}/diagrams/${encodeURIComponent(id)}`, undefined, { branch, ai: String(ai) }),
  // ---- repository analysis (code graph) ----
  codeOverview: (pid: string) => call<CodeOverview>("GET", `/projects/${pid}/codegraph`),
  codeAnalyze: (pid: string, repo_url: string) => call<CodeJob>("POST", `/projects/${pid}/codegraph/analyze`, { repo_url }),
  codeJob: (pid: string, id: string) => call<CodeJob>("GET", `/projects/${pid}/codegraph/jobs/${id}`),
  codeUpload: (pid: string, file: Blob, repo_url?: string, commit_sha?: string) =>
    upload<CodeGraphRecord>(`/projects/${pid}/codegraph/upload`, file,
      Object.fromEntries(Object.entries({ repo_url, commit_sha }).filter(([, v]) => v)) as Record<string, string>),
  codeReconstruct: (pid: string, id: string) =>
    call<ReconstructResult>("POST", `/projects/${pid}/codegraph/${id}/reconstruct`, { refine: true }),
  /** Read a document into proposed requirements (nothing is applied yet), reporting each step; abort to cancel. */
  importRequirements: importStream,
  /** Add the accepted (possibly edited) proposals with their source; optionally save notes to project memory. */
  acceptImport: (pid: string, branch: string, file: IntakeResult["file"], requirements: Record<string, unknown>[],
    memory: (IntakeNote & { category: MemoryCategory })[]) =>
    call<{ added: string[]; summary: string[]; memory: MemoryItem[] }>(
      "POST", `/projects/${pid}/requirements/import/accept`, { file, requirements, memory }, b(branch)),
  codeDrift: (pid: string, branch: string) => call<DriftResult>("GET", `/projects/${pid}/codegraph/drift`, undefined, b(branch)),
  codeImpact: (pid: string, branch: string, files: string[]) =>
    call<ImpactResult>("POST", `/projects/${pid}/codegraph/impact`, { files, branch }),
  // ---- Jira Cloud push (connection: admins; preview and push: developers and up) ----
  jiraSettings: (org: string) => call<JiraSettings>("GET", `/orgs/${org}/jira`),
  saveJiraSettings: (org: string, body: { site_url: string; email: string; api_token: string | null; project_key: string }) =>
    call<JiraSettings>("PUT", `/orgs/${org}/jira`, body),
  deleteJiraSettings: (org: string) => call<null>("DELETE", `/orgs/${org}/jira`),
  testJira: (org: string) => call<JiraTestResult>("POST", `/orgs/${org}/jira/test`),
  jiraLinks: (pid: string) => call<JiraLinks>("GET", `/projects/${pid}/jira/links`),
  jiraPlan: (pid: string, branch: string, project_key?: string, ai = true) =>
    call<JiraPlan>("POST", `/projects/${pid}/jira/plan`, { project_key: project_key || null, ai }, b(branch)),
  jiraPush: (pid: string, branch: string, project_key?: string, ai = true) =>
    call<JiraPushResult>("POST", `/projects/${pid}/jira/push`, { project_key: project_key || null, ai, confirm: true }, b(branch)),
  jiraRefresh: (pid: string, branch: string) =>
    call<{ updated: number; links: JiraLinks["links"] }>("POST", `/projects/${pid}/jira/refresh`, undefined, b(branch)),
  // ---- Jira two-way sync: webhook secret (admins), unlinked issues and import (developers and up) ----
  createJiraWebhook: (org: string) => call<JiraWebhookSetup>("POST", `/orgs/${org}/jira/webhook`),
  deleteJiraWebhook: (org: string) => call<null>("DELETE", `/orgs/${org}/jira/webhook`),
  jiraIssues: (pid: string, opts: { page_token?: string | null; q?: string; limit?: number } = {}) =>
    call<JiraIssuePage>("GET", `/projects/${pid}/jira/issues`, undefined, {
      limit: String(opts.limit ?? 25), ...(opts.q ? { q: opts.q } : {}), ...(opts.page_token ? { page_token: opts.page_token } : {}) }),
  jiraImport: (pid: string, branch: string, keys: string[], ai = true) =>
    call<JiraImportResult>("POST", `/projects/${pid}/jira/import`, { keys, ai }, b(branch)),
  // ---- meeting intake ----
  /** Read a transcript or recording into proposed requirements, decisions and action items (nothing applied yet). */
  importMeeting: meetingStream,
  acceptMeeting: (pid: string, branch: string, file: MeetingResult["file"], requirements: Record<string, unknown>[],
    decisions: Omit<MeetingDecision, "id" | "selected">[], memory: { text: string; category: MemoryCategory }[]) =>
    call<{ added: string[]; summary: string[]; decisions: Decision[]; memory: MemoryItem[] }>(
      "POST", `/projects/${pid}/meetings/import/accept`, { file, requirements, decisions, memory }, b(branch)),
  // ---- knowledge base ----
  knowledge: (pid: string) => call<KbStatus>("GET", `/projects/${pid}/knowledge`),
  addKnowledge: (pid: string, file: File) =>
    upload<KbDocument>(`/projects/${pid}/knowledge`, file, { filename: file.name }, "application/octet-stream"),
  deleteKnowledge: (pid: string, id: string) => call<null>("DELETE", `/projects/${pid}/knowledge/${id}`),
  knowledgePassages: (pid: string, id: string) =>
    call<KbDocument & { passages: Omit<KbPassage, "ref" | "score" | "keyword_rank" | "vector_score">[] }>("GET", `/projects/${pid}/knowledge/${id}/chunks`),
  searchKnowledge: (pid: string, query: string, limit = 8) =>
    call<{ query: string; mode: "hybrid" | "keyword"; results: KbPassage[] }>("POST", `/projects/${pid}/knowledge/search`, { query, limit }),
  askKnowledge: (pid: string, question: string) => call<KbAnswer>("POST", `/projects/${pid}/knowledge/ask`, { question }),
  reindexKnowledge: (pid: string) =>
    call<KbStatus & { model: string; reindexed: number; failed: { id: string; name: string; error: string }[] }>("POST", `/projects/${pid}/knowledge/reindex`),
  // ---- cost estimate ----
  costs: (pid: string, branch: string, assumptions: Record<string, unknown>, ai = true) =>
    call<CostEstimate>("POST", `/projects/${pid}/costs`, { assumptions, ai }, b(branch)),
  costsCsv: (pid: string, branch: string) => download(`/projects/${pid}/costs.csv`, { branch }, "costs.csv"),
  // ---- generated infrastructure as code, tests and documents ----
  generate: (pid: string, branch: string, kind: GenerationKind, target: GenerationTarget, ai: boolean, cloud = "") =>
    call<GenerationResult>("POST", `/projects/${pid}/generate`, { kind, target, branch, ai, cloud }),
  generateZip: (pid: string, branch: string, kind: GenerationKind, target: GenerationTarget, files: GeneratedFile[]) =>
    downloadPost(`/projects/${pid}/generate/zip`, { kind, target, branch, files }, `${kind}-${target}.zip`),

  // ---- compliance frameworks (report: viewers; frameworks and evidence: developers and up) ----
  compliance: (pid: string, branch: string, frameworks: string[] = []) =>
    call<ComplianceReport>("GET", `/projects/${pid}/compliance`, undefined, { branch, frameworks: frameworks.join(",") }),
  saveComplianceFrameworks: (pid: string, frameworks: string[]) =>
    call<{ frameworks: string[] }>("PUT", `/projects/${pid}/compliance/settings`, { frameworks }),
  setComplianceEvidence: (pid: string, framework: string, control_id: string, status: "" | "met" | "not_applicable", note: string) =>
    call<unknown>("PUT", `/projects/${pid}/compliance/evidence`, { framework, control_id, status, note }),
  complianceReview: (pid: string, branch: string, frameworks: string[], ai: boolean) =>
    call<ComplianceRemediation>("POST", `/projects/${pid}/compliance/review`, { frameworks, ai }, b(branch)),
  complianceExport: (pid: string, branch: string, format: "md" | "json", frameworks: string[]) =>
    download(`/projects/${pid}/compliance.${format}`, { branch, frameworks: frameworks.join(",") }, `compliance.${format}`),
  /** The print-ready report as HTML text, to open in a new tab and print to PDF. */
  compliancePrintHtml: async (pid: string, branch: string, frameworks: string[]): Promise<string> => {
    const res = await fetch(`/api/projects/${pid}/compliance.html?${new URLSearchParams({ branch, frameworks: frameworks.join(","), inline: "true" })}`,
      { credentials: "include" });
    if (!res.ok) {
      if (res.status === 401) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
      throw new ApiError(res.status, await res.json().catch(() => null));
    }
    return res.text();
  },

  // ---- integrations (settings: admins; GitHub push and Confluence publish: developers and up) ----
  integrations: (org: string) => call<IntegrationSettings>("GET", `/orgs/${org}/integrations`),
  saveIntegration: (org: string, kind: IntegrationKind, config: Record<string, unknown>, secret: string | null) =>
    call<IntegrationSettings>("PUT", `/orgs/${org}/integrations/${kind}`, { config, secret }),
  deleteIntegration: (org: string, kind: IntegrationKind) => call<null>("DELETE", `/orgs/${org}/integrations/${kind}`),
  testIntegration: (org: string, kind: IntegrationKind) =>
    call<{ ok: boolean; detail?: string; error?: string }>("POST", `/orgs/${org}/integrations/${kind}/test`),
  githubLinks: (pid: string) => call<ExternalLinks>("GET", `/projects/${pid}/github/links`),
  githubPlan: (pid: string, branch: string, include_nfr?: boolean) =>
    call<GitHubPlan>("POST", `/projects/${pid}/github/plan`, { include_nfr: include_nfr ?? null }, b(branch)),
  githubPush: (pid: string, branch: string, include_nfr?: boolean) =>
    call<GitHubPushResult>("POST", `/projects/${pid}/github/push`, { include_nfr: include_nfr ?? null, confirm: true }, b(branch)),
  githubRefresh: (pid: string, branch: string) =>
    call<{ updated: number; links: ExternalLinks["links"] }>("POST", `/projects/${pid}/github/refresh`, undefined, b(branch)),
  confluenceLinks: (pid: string) => call<ExternalLinks>("GET", `/projects/${pid}/confluence/links`),
  confluencePublish: (pid: string, branch: string, dry_run: boolean) =>
    call<ConfluencePublish>("POST", `/projects/${pid}/confluence/publish`, { dry_run }, b(branch)),

  // ---- two-step sign-in (MFA), single sign-on, per-project roles ----
  mfaVerify: (mfa_token: string, code: string) => call<Me>("POST", "/auth/mfa/verify", { mfa_token, code }),
  mfaStatus: () => call<MfaStatus>("GET", "/auth/mfa"),
  mfaSetup: () => call<{ secret: string; otpauth_uri: string }>("POST", "/auth/mfa/setup"),
  mfaEnable: (code: string) => call<MfaStatus>("POST", "/auth/mfa/enable", { code }),
  mfaDisable: (code: string) => call<MfaStatus>("POST", "/auth/mfa/disable", { code }),
  mfaRecoveryCodes: (code: string) => call<MfaStatus>("POST", "/auth/mfa/recovery-codes", { code }),
  ssoDiscover: (email: string) => call<{ org_id: string; org_name: string; start_url: string }>("POST", "/auth/sso/discover", { email }),
  authSettings: (org: string) => call<AuthSettings>("GET", `/orgs/${org}/auth-settings`),
  saveSso: (org: string, body: { enabled: boolean; issuer: string; client_id: string; client_secret?: string; domains: string[];
    default_role: string; require_sso: boolean }) => call<AuthSettings>("PUT", `/orgs/${org}/auth-settings/sso`, body),
  deleteSso: (org: string) => call<null>("DELETE", `/orgs/${org}/auth-settings/sso`),
  testSso: (org: string) => call<{ ok: boolean; detail: string }>("POST", `/orgs/${org}/auth-settings/sso/test`),
  setRequireMfa: (org: string, require_mfa: boolean) => call<AuthSettings>("PUT", `/orgs/${org}/auth-settings/mfa`, { require_mfa }),
  projectRoles: (pid: string) => call<ProjectRoles>("GET", `/projects/${pid}/roles`),
  setProjectRole: (pid: string, userId: string, role: ProjectRole) => call<ProjectRoles>("PUT", `/projects/${pid}/roles/${userId}`, { role }),
  clearProjectRole: (pid: string, userId: string) => call<ProjectRoles>("DELETE", `/projects/${pid}/roles/${userId}`),
};
