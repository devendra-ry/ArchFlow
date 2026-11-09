export const NODE_TYPES = [
  "client", "gateway", "service", "database", "cache", "queue", "storage",
  "agent", "llm", "mcp", "external", "security", "note",
  // low-level design (inside a component's own diagram)
  "component", "endpoint", "table", "event",
] as const;
export type NodeType = (typeof NODE_TYPES)[number];

export interface Versioned { rev: number; field_revs: Record<string, number> }

export interface ArchNode extends Versioned {
  id: string;
  type: NodeType;
  name: string;
  technology: string;
  vendor?: string;
  catalog_id?: string;
  description: string;
  position: { x: number; y: number };
  requirement_ids: string[];
  props: Record<string, string>;
  /** "" = top-level (HLD) diagram; else the id of the component whose internal (LLD) diagram holds it. */
  parent_id?: string;
}

export interface ArchEdge extends Versioned {
  id: string;
  source: string;
  target: string;
  protocol: string;
  label: string;
}

export type Priority = "low" | "medium" | "high" | "critical";
export type ReqStatus = "draft" | "review" | "approved" | "implemented";

export interface Requirement extends Versioned {
  id: string;
  title: string;
  description: string;
  kind: "functional" | "non_functional";
  category: string;
  priority: Priority;
  actors: string[];
  acceptance_criteria: string[];
  status: ReqStatus;
  /** Provenance: where it came from, e.g. {type: "pdf", ref: "prd.pdf", section, page, at}. */
  source?: Record<string, string>;
  /** Soft-deleted: hidden everywhere except the Deleted filter, where it can be restored. */
  deleted?: boolean;
}

/** Requirements that are not soft-deleted. */
export const activeRequirements = (s: ProjectState): Requirement[] => Object.values(s.requirements).filter((r) => !r.deleted);

/** A component whose linked requirement changed after the component last did (cleared by "Mark reviewed"). */
export interface StaleLink { node_id: string; requirement_id: string; fields: string[]; component_rev: number; requirement_rev: number }

/** One change to a requirement (newest first from the history API). */
export interface RequirementRevision {
  id: number; requirement_id: string; rev: number; created_at: string;
  action: "added" | "updated" | "deleted" | "restored" | "purged";
  actor: string; actor_kind: "ai" | "agent" | "human";
  changes: { field: string; before: unknown; after: unknown }[];
}

export interface ProjectState {
  rev: number;
  nodes: Record<string, ArchNode>;
  edges: Record<string, ArchEdge>;
  requirements: Record<string, Requirement>;
}

export interface Project { id: string; name: string; description: string; created_at: string; org_id?: string }
export interface CatalogItem {
  id: string; vendor: string; kind: string; name: string; type: NodeType; category: string; kind_label: string;
}
export interface Catalog { vendors: Record<string, string>; items: CatalogItem[] }
export interface Org { id: string; name: string; created_at: string; role?: Role }

// ---- accounts and roles ----
export const ROLES = ["viewer", "reviewer", "developer", "architect", "admin", "owner"] as const;
export type Role = (typeof ROLES)[number];
const RANK: Record<string, number> = Object.fromEntries(ROLES.map((r, i) => [r, i]));
/** Mirrors backend authz.py: read = any member, review = reviewer+, edit = developer+, admin, owner. */
export function can(role: Role | null | undefined, action: "read" | "review" | "edit" | "admin" | "owner"): boolean {
  const min = { read: "viewer", review: "reviewer", edit: "developer", admin: "admin", owner: "owner" }[action];
  return role != null && role in RANK && RANK[role] >= RANK[min];
}
export const ROLE_HELP: Record<Role, string> = {
  viewer: "Read only",
  reviewer: "Read, accept or reject decisions, and approve versions",
  developer: "Edit the canvas, requirements, versions and decisions; run AI",
  architect: "Same as developer, and approve versions",
  admin: "Also manage members, invites, API tokens and AI keys",
  owner: "Everything, including organization settings and owners",
};
export interface Account { id: string; email: string; name: string; created_at: string; last_login_at: string | null }
export interface Me {
  user: Account; orgs: (Org & { role: Role })[]; features: { ai_settings: boolean; mfa?: boolean };
  security?: MeSecurity;
}
export interface Member { org_id: string; user_id: string; email: string; name: string; role: Role; created_at: string }
export interface Invite {
  id: string; org_id: string; email: string | null; role: Role; created_by: string | null; created_at: string; expires_at: number;
}
export interface InviteInfo { org_name: string; role: Role; email: string | null; expires_at: number }
export type AIProvider = string; // gemini, anthropic, openai, openrouter, mistral, groq, deepseek, xai, azure, openai_compatible
export type AIGroup = "reasoning" | "fast";
export interface AIProviderInfo {
  id: AIProvider; label: string; key: "required" | "optional"; params: Record<string, string>; required_params: string[];
  docs: string | null; examples: Record<AIGroup, string>; server_key: boolean;
}
export interface AIModelChoice { provider: AIProvider; model: string }
export interface AIConfig { reasoning: AIModelChoice[]; fast: AIModelChoice[]; params: Record<AIProvider, Record<string, string>> }
export interface AISettings {
  /** False when the server cannot encrypt keys (ARCHFLOW_SECRET_KEY and no data directory): keys cannot be saved. */
  enabled: boolean;
  providers: Record<AIProvider, { configured: boolean; last4: string | null; updated_at: string | null }>;
  catalog: AIProviderInfo[]; config: AIConfig; defaults: Record<AIGroup, string[]>; effective: Record<AIGroup, string[]>;
  available: boolean;
}
export interface AITestResult { ok: boolean; group: AIGroup; model?: string; chain?: string[]; latency_ms?: number; reply?: string; error?: string }

/** What a sign-in rule change would block (GET /orgs/{id}/auth-settings/impact). */
export interface AuthImpact {
  members_without_mfa: string[];
  members_needing_sso: string[];
  tokens_blocked: { id: string; kind: "pat" | "refresh"; name: string; user_name: string; reason: string }[];
}
export interface ApiToken {
  id: string; kind: "pat" | "refresh"; name: string; user_name: string; access: "read" | "write";
  client_name: string; created_at: string; expires_at: number | null; last_used_at: string | null;
  /** How the user had signed in when the token was granted ("password" or "sso:<org>"). */
  auth_method?: string | null;
  /** Projects the token is limited to (null: every project in the org). */
  project_ids?: string[] | null;
}
export interface Branch { name: string; base_version: number | null; created_at: string }
export interface Version {
  number: number; branch: string; message: string; author: string; parent: number | null; created_at: string;
  /** The user who saved it (null for versions saved by the system or before authors were recorded). */
  author_id?: string | null;
  review?: { status: ReviewStatus; approvals: number; required: number };
}
// ---- version approvals ----
export type ReviewStatus = "draft" | "in_review" | "approved" | "changes_requested";
export type ReviewAction = "request" | "approve" | "request_changes";
export interface ReviewEntry {
  id: number; version: number; branch: string; action: ReviewAction; actor_id: string; actor_name: string; comment: string; created_at: string;
}
export interface VersionReview {
  version: number; status: ReviewStatus; required: number; approvals: { id: string; name: string; at: string }[];
  requested_by: { id: string; name: string } | null; history: ReviewEntry[];
}
export interface ApprovalPolicy { required_approvals: number; require_for_adopt: boolean; updated_by: string | null; updated_at: string | null }
/** Roles that may approve a version or request changes (developers request reviews but do not approve). */
export const APPROVER_ROLES: Role[] = ["reviewer", "architect", "admin", "owner"];
export interface Activity { actor: string; summary: string[]; rev: number; created_at: string }

export type Op = Record<string, unknown> & { op: string };

export interface Issue {
  severity: "error" | "warning" | "info";
  code: string; message: string; suggestion: string;
  node_ids: string[]; requirement_ids: string[];
  capability?: string;  // for "completeness" issues: the checklist capability that is missing
}

export interface Analysis {
  requirement_issues: Issue[];
  architecture_issues: Issue[];
  traceability: { requirement_to_nodes: Record<string, string[]>; untraced_requirements: string[]; unjustified_nodes: string[] };
  health: {
    requirements_complete: number; requirements_approved: number; traceability: number; architecture_score: number;
    counts: { requirements: number; components: number; connections: number };
    issues: { errors: number; warnings: number; info: number };
    /** Present when the project has a code analysis: how well the design matches the code (0-100). */
    code_alignment?: number;
  };
  /** Designed-vs-code drift (only when the project has a code analysis). */
  drift_issues?: Issue[];
  /** Component → requirement links where the requirement changed after the component. */
  stale_links?: StaleLink[];
}

interface DiffSection { added: { id: string; name: string }[]; removed: { id: string; name: string }[]; changed: { id: string; name: string; fields: { field: string; from: unknown; to: unknown }[] }[] }
export interface Diff {
  nodes: DiffSection; edges: DiffSection; requirements: DiffSection;
  total_changes: number; text: string; from_state: ProjectState;
}

// ---- branch merge (three-way) ----
/** One field both branches changed differently, or an element one deleted while the other changed it. */
export interface MergeConflict {
  id: string; collection: "nodes" | "edges" | "requirements"; entity_id: string;
  entity: "component" | "connection" | "requirement"; name: string;
  field: string; // "" for a deletion conflict; "props.<key>" for one property
  kind: "field" | "deleted-in-ours" | "deleted-in-theirs";
  base: unknown; ours: unknown; theirs: unknown;
}
export interface MergePreview {
  source: string; target: string; source_rev: number; target_rev: number;
  base: { version: number | null; how: "common-ancestor" | "branch-point" | "none" };
  changes: Omit<Diff, "from_state">;
  conflicts: MergeConflict[];
  dropped: { collection: string; id: string; entity: string; name: string; reason: string }[];
  renumbered: { from: string; to: string; title: string }[];
}
export type MergeChoice = "ours" | "theirs" | "custom";
export interface MergeResolution { id: string; choice: MergeChoice; value?: unknown }
export interface MergeSuggestion extends MergeResolution { rationale: string }
export interface MergeResult {
  version: number; safety_version: number | null; source_version: number; branch: string; source_branch: string;
  summary: string[]; conflicts_resolved: number; state: ProjectState;
}

export interface User { name: string; color: string }

// ---- guided design chat ----
export interface ChatOption { id: string; label: string; description: string; recommended?: boolean }
export interface QuestionCard {
  type: "question"; id: string; question: string; why?: string; multi: boolean;
  options: ChatOption[]; step?: number; total?: number | null;
}
export interface ArchComponent { key: string; name: string; type: NodeType; technology: string; description?: string; layer: number }
export interface ArchOption {
  id: string; name: string; summary: string; pros: string[]; cons: string[]; best_for?: string;
  cost: "low" | "medium" | "high"; complexity: "low" | "medium" | "high"; recommended?: boolean;
  components: ArchComponent[]; connections: { from: string; to: string; protocol?: string }[];
  review?: ArchReview;
}
/** What ArchFlow's review changed in a proposed option (deterministic checks + optional AI repair). */
export interface ArchReview {
  engine: "rules" | "llm+rules";
  issues_before: { severity: "error" | "warning" | "info"; code: string; message: string }[];
  issues_after: { severity: "error" | "warning" | "info"; code: string; message: string }[];
  fixes: string[];
  score?: number;
}
export interface ArchitecturesCard { type: "architectures"; id: string; title: string; options: ArchOption[]; requirements: Record<string, unknown>[] }
export type ChatCard = QuestionCard | ArchitecturesCard;
export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
  card?: ChatCard | null;
  answer?: { option_ids: string[]; other?: string };
  applied?: { option: string; branch: string; version: number };
}

// ---- architecture decisions (ADRs) and alternative evaluation ----
export type DecisionStatus = "proposed" | "accepted" | "rejected" | "superseded";
export interface DecisionAlternative { name: string; branch?: string | null; summary: string; pros: string[]; cons: string[] }
export interface Evaluation {
  engine: "jev" | "llm" | "heuristic";
  recommended: string;
  alternatives: string[];
  criteria: string[];
  criteria_labels: Record<string, string>;
  scores: Record<string, Record<string, number>>;
  totals: Record<string, number>;
  rationale: string;
  confidence: number | null;
  notes: string[];
  requirement_ids?: string[];
  probabilities?: Record<string, number>;
  raw?: unknown;
  /** NFR-derived criterion weights behind `totals` (older evaluations lack them). */
  weights?: Record<string, number>;
  details?: Record<string, EvaluationDetail>;
  deciding_factors?: string[];
  flip_conditions?: string;
  /** With Jev scoring: the LLM agent's own pick, run in parallel, and whether it agrees. */
  second_opinion?: { engine: "llm"; recommended: string; totals: Record<string, number>; scores: Record<string, Record<string, number>>;
    rationale: string; confidence: number | null; agrees: boolean };
  /** Questions the AI suggests asking next about this comparison. */
  follow_up_questions?: string[];
}
export interface EvaluationDetail {
  summary: string;
  strengths: string[];
  risks: string[];
  best_when: string;
  reasons: Record<string, string>;
}
export interface Decision {
  id: string; title: string; status: DecisionStatus; context: string; decision: string; consequences: string;
  alternatives: DecisionAlternative[]; chosen: string; requirement_ids: string[]; node_ids: string[];
  evaluation: Evaluation | null; author: string; created_at: string; updated_at: string;
}
// ---- HLD → LLD drill-down ----
export interface LldResult {
  node_id: string; name: string; engine: "llm" | "offline"; counts: Record<string, number>;
  connections: number; summary: string[]; rev: number;
}
export interface ComponentViews {
  node_id: string; name: string; has_lld: boolean;
  api: { endpoints: { id: string; method: string; path: string; description: string; requirement_ids: string[] }[]; openapi: string };
  er: {
    tables: { id: string; name: string; description: string; columns: { name: string; type: string; key: string }[] }[];
    relations: { from: string; to: string; label: string }[]; mermaid: string;
  };
  sequence: { mermaid: string };
}

// ---- diagrams derived from the model (backend diagrams.py) ----
export type DiagramKind = "hld" | "c4-context" | "c4-container" | "sequence" | "er" | "deployment" | "dataflow";
export interface DiagramInfo {
  id: string; kind: DiagramKind; title: string; alt: string; empty: boolean;
  flow?: string; requirement_ids?: string[]; path?: string[]; engine?: "llm" | "offline";
}
export interface Diagram extends DiagramInfo { mermaid: string; plantuml: string }

// ---- repository analysis (code graph) ----
export interface CodeSummary {
  stats: { files?: number; symbols?: number; edges?: number; communities?: number };
  generator: { tool?: string; version?: string };
  root_name: string;
  languages: Record<string, number>;
  infrastructure: { key: string; label: string; category: string; technology: string; files: number; evidence: string[] }[];
  frameworks: { name: string; kind: string; files: number }[];
  entry_points: { path: string; kind: string; name: string }[];
  manifests: { path: string; kind: string; name: string }[];
  compose_services: { name: string; image: string; build: string | null; files?: string[]; dev_tool?: boolean }[];
  dev_tools?: { name: string; image: string; files: string[] }[];  // excluded from the reconstructed architecture
  dockerfiles: number; k8s: number; terraform: number;
  communities: { id: number; size: number; label: string; paths: string[] }[];
  god_nodes: { id: string; name: string; degree: number }[];
  import_cycles: number;
}
export interface CodeGraphRecord {
  id: string; project_id: string; source: "git" | "local" | "upload"; repo_url: string | null; commit_sha: string | null;
  created_at: string; created_by: string; status: string; summary?: CodeSummary;
}
export interface CodeOverview {
  available: boolean; local_repos: boolean; latest: (CodeGraphRecord & { summary: CodeSummary }) | null;
  graphs: CodeGraphRecord[]; cli: string; install_hint: string | null;
}
export interface CodeJob {
  id: string; project_id: string; status: "queued" | "running" | "done" | "failed"; phase: string; source: string;
  repo_url: string | null; created_by: string; error: string | null; code_graph_id: string | null; commit_sha?: string | null;
}
export interface DriftResult {
  branch: string; code_graph_id: string; repo_url: string | null; commit_sha: string | null; alignment: number;
  issues: (Issue & { code_node_ids: string[] })[];
  matches: { node_id: string; name: string; code_node_id: string; code_name: string; score: number }[];
  code_only: string[]; design_only: string[];
}
export interface ImpactResult {
  branch: string; changed_files: string[]; unmatched_files: string[]; seed_symbols: number;
  affected_symbols: { id: string; name: string; file: string; line: number | null; depth: number; via: string }[];
  affected_symbol_count: number; affected_files: string[];
  code_components: { id: string; name: string }[];
  components: { id: string; name: string; type: string }[];
  requirements: { id: string; title: string; priority: string; status: string }[];
}
export interface ReconstructResult {
  branch: string; version: number; code_graph_id: string; engine: string; components: number; connections: number;
  requirements_linked: number;
  dev_tools?: { name: string; image: string; files: string[]; included: boolean }[];
  excluded?: { path: string; role: string; reason: string; files: number }[];
}

// ---- conversations (shared AI chat threads) and project memory ----
export type ConversationKind = "assistant" | "design";
export interface Conversation {
  id: string; project_id: string; branch: string; kind: ConversationKind; title: string;
  /** Running summary of older turns the AI no longer sees verbatim. */
  summary: string; summarized_seq: number;
  created_by: string; created_by_id: string | null; created_at: string; updated_at: string; message_count: number;
}
export interface ConversationMessage {
  seq: number; role: "user" | "assistant"; mode: "ask" | "command" | "design" | "note"; content: string;
  data: {
    engine?: string; error?: string; applied?: boolean | { option: string; branch: string; version: number };
    summary?: string[]; questions?: string[]; question_options?: Record<string, string[]>; selection?: string[];
    card?: ChatCard | null; answer?: { option_ids: string[]; other?: string };
  };
  author: string; created_at: string;
}
export const MEMORY_CATEGORIES = ["constraint", "preference", "decision", "requirement", "context", "fact"] as const;
export type MemoryCategory = (typeof MEMORY_CATEGORIES)[number];
export interface MemoryItem {
  id: string; text: string; category: MemoryCategory; pinned: boolean;
  source: "user" | "ai" | "design-chat" | "mcp" | "import"; conversation_id: string | null;
  created_by: string; created_at: string; updated_at: string;
}

// ---- OpenSpec (spec-driven changes, stored per project) ----
export interface OpenSpecIssue { level: "ERROR" | "WARNING" | "INFO"; path?: string; message: string }
export interface OpenSpecChange {
  change_id: string; title: string; status: "draft" | "valid" | "invalid" | "archived"; engine: string; branch: string;
  scope: { node_ids?: string[]; requirement_ids?: string[]; whole_system?: boolean; target?: string; extra?: string };
  validation: { valid?: boolean; issues?: OpenSpecIssue[]; repairs?: number; notes?: string[]; skipped?: string;
    archive?: { totals?: Record<string, number> } };
  archived_as: string | null; created_by: string; created_at: string; updated_at: string;
}
export interface OpenSpecOverview {
  available: boolean; ai: boolean; changes: OpenSpecChange[];
  specs: { capability: string; path: string; updated_at: string; updated_by: string }[];
}
export interface OpenSpecJob {
  id: string; status: "running" | "done" | "failed"; step: string; steps: { step: string; at: number }[];
  change_id: string | null; error: string | null; elapsed?: number;
}

// ---- AI usage and budgets ----
export interface AIUsageRow { key: string; calls: number; prompt_tokens: number; completion_tokens: number; cost_usd: number; name?: string }
export interface AIUsageReport {
  days: number; month_start: string; month_spend_usd: number; used_percent: number | null; status: "ok" | "warning" | "blocked";
  budget: { monthly_limit_usd: number | null; alert_percent: number; updated_by: string | null; updated_at: string | null };
  total: { calls: number; prompt_tokens: number; completion_tokens: number; cost_usd: number };
  by_feature: AIUsageRow[]; by_model: AIUsageRow[]; by_project: AIUsageRow[]; daily: AIUsageRow[];
  /** Calls per prompt version (id@version); missing on servers before prompt versioning. */
  by_prompt?: AIUsageRow[];
  /** Calls per person (name filled in); missing on older servers. */
  by_user?: AIUsageRow[];
}

// ---- AI architecture review (specialist agents in parallel, see review.py) ----
export interface ReviewFinding {
  focus: string; severity: "critical" | "high" | "medium" | "low"; title: string; detail: string;
  components: string[]; requirement_ids: string[]; fix: string;
}
export interface ReviewResult {
  findings: ReviewFinding[];
  specialists: Record<string, { label: string; status: "ok" | "error"; findings: number; summary: string; error?: string }>;
}

// ---- AI suggestions (LLM follow-ups and starting prompts) ----
/** kind: "start" (empty conversation), "follow_up" (after an answer), "reply" (an answer to the AI's question). */
export interface AiSuggestion { label: string; prompt: string; why?: string; kind?: "start" | "follow_up" | "reply" }
export type SuggestionMode = "edit" | "ask" | "design";

// ---- AI observability: agent traces and the prompt catalog ----
export interface AiSpan {
  kind: "model" | "tool" | "api"; name: string; started_at: string; latency_ms: number; status: "ok" | "error" | "running";
  input: unknown; output: unknown; error: string; model: string;
  prompt_tokens: number; completion_tokens: number; cost_usd: number; calls: number;
}
export interface AiTraceSummary {
  id: string; org_id: string; project_id: string; project_name?: string; feature: string; prompt: string; agent: string;
  /** Who asked, what they asked and what the AI answered (short previews). */
  user_id: string; user_name: string; question: string; answer: string;
  /** The run that fired this one (agents run in parallel); "" for a top-level run. */
  parent_id: string;
  status: "ok" | "error"; error: string; prompt_tokens: number; completion_tokens: number; cost_usd: number;
  latency_ms: number; model_calls: number; tool_calls: number; models: string[]; created_at: string;
}
export interface AiTrace extends AiTraceSummary {
  input: unknown; output: unknown; spans: AiSpan[]; metadata: Record<string, unknown>;
  /** Agents this run fired in parallel. */
  children: AiTraceSummary[];
}
export interface AiTraceList {
  traces: AiTraceSummary[]; people: { id: string; name: string }[]; content: "full" | "metadata" | "off"; retention_days: number;
}
export interface PromptInfo {
  id: string; feature: string; description: string; active: string; default: string;
  versions: { version: string; date: string; notes: string; sha: string }[];
}

// ---- comments and notifications ----
export type CommentTarget = "project" | "node" | "decision" | "requirement" | "edge";
export interface Comment {
  id: string; project_id: string; branch: string; target_type: CommentTarget; target_id: string; parent_id: string;
  author_id: string; author_name: string; body: string; mentions: string[]; resolved: boolean; resolved_by: string;
  created_at: string; edited_at: string | null;
}
export interface AppNotification {
  id: number; project_id: string; project_name: string; branch: string; comment_id: string | null; thread_id: string;
  /** Set for version review notifications (review requested, approved, changes requested). */
  version: number | null;
  kind: "mention" | "reply" | "review_requested" | "approved" | "changes_requested"; actor_name: string; preview: string;
  read: boolean; created_at: string;
}

// ---- document intake (requirements imported from a PDF, Word, Markdown or text file) ----
export interface IntakeMatch {
  id: string; title: string; where: "project" | "document"; reason: string; source: Record<string, string>;
  proposed_source?: Record<string, string>;
}
export interface IntakeProposal {
  id: string; title: string; description: string; kind: Requirement["kind"]; category: string; priority: Priority;
  actors: string[]; acceptance_criteria: string[]; section: string; page: number | null;
  source: Record<string, string>; duplicates: IntakeMatch[]; conflicts: IntakeMatch[]; selected: boolean;
}
export interface IntakeNote { text: string; section?: string; page?: number }
/** One progress event while a document is imported; ``done``/``total`` count pages or parts. */
export interface IntakeStep {
  stage: "upload" | "read" | "split" | "extract" | "compare"; message: string; done?: number; total?: number;
}
export interface IntakeResult {
  engine: "llm" | "offline" | "mixed"; notes: string[];
  file: { name: string; type: string; pages: number | null; sections: number; chars: number; chunks: number };
  /** The library that read the file and the model that extracted requirements ("" when the offline rules did). */
  reader?: { library: string; model: string };
  requirements: IntakeProposal[]; assumptions: IntakeNote[]; constraints: IntakeNote[]; open_questions: IntakeNote[];
  stakeholders: string[];
}

// ---- meeting intake (transcripts and recordings) ----
export interface MeetingProposal extends Omit<IntakeProposal, "section" | "page"> {
  speaker: string; timestamp: string; agreement: "agreed" | "proposed" | "disputed";
}
export interface MeetingDecision {
  id: string; title: string; decision: string; rationale: string; alternatives: string[]; decided_by: string;
  timestamp: string; selected: boolean;
}
export interface MeetingAction { id: string; task: string; owner: string; due: string; timestamp: string; selected: boolean }
export interface MeetingNote { text: string; timestamp?: string }
export interface MeetingStep { stage: "upload" | "read" | "split" | "extract" | "compare" | "knowledge"; message: string; done?: number; total?: number }
export interface MeetingResult {
  engine: "llm" | "offline" | "mixed"; notes: string[];
  file: { name: string; type: string; turns: number; speakers: string[]; duration: string; chars: number; chunks: number };
  reader: { transcription: string; model: string };
  summary: string; participants: string[];
  requirements: MeetingProposal[]; decisions: MeetingDecision[]; action_items: MeetingAction[];
  open_questions: MeetingNote[]; assumptions: MeetingNote[]; constraints: MeetingNote[];
  knowledge?: { id: string; chunks: number; embedded: boolean };
}

// ---- knowledge base ----
export interface KbDocument {
  id: string; project_id: string; name: string; type: string; origin: "upload" | "import" | "meeting"; chars: number;
  chunks: number; embedding_model: string; created_by: string; created_at: string; note?: string;
}
export interface KbStatus {
  documents: KbDocument[]; embedding_model: string; stale: number; chunks: number; max_documents: number; ai_available: boolean;
}
export interface KbPassage {
  id: number; document_id: string; document: string; type: string; ord: number; section: string; page: number | null;
  locator: string; text: string; ref: string; score: number; keyword_rank: number | null; vector_score: number | null;
}
export interface KbCitation {
  ref: string; quote: string; document_id: string; document: string; section: string; page: number | null; locator: string;
}
export interface KbAnswer {
  question: string; engine: "llm" | "offline"; mode: "hybrid" | "keyword"; answerable: boolean; answer: string;
  citations: KbCitation[]; follow_ups: string[]; passages: KbPassage[];
}

// ---- cost estimate ----
export interface CostAssumption { value: number | string | boolean; source: string; text?: string }
export type CostAssumptionKey = "monthly_active_users" | "peak_rps" | "storage_gb" | "egress_gb" | "availability" |
  "llm_calls_per_user" | "llm_input_tokens" | "llm_output_tokens" | "emails_per_user" | "messages_per_user" | "region" |
  "include_staging" | "staging_share";
export interface CostLine {
  node_id: string; name: string; type: string; kind: string; technology: string; category: string; vendor: string;
  monthly: number; low: number; high: number; basis: string; confidence: "high" | "medium" | "low"; notes: string[];
}
export interface CostRange { monthly: number; low: number; high: number }
export interface CostRecommendation {
  title: string; detail: string; saving_monthly: number; components: string[]; tradeoff: string; effort: "low" | "medium" | "high";
}
export interface CostEstimate {
  currency: string; price_date: string; branch: string; engine: "llm" | "offline";
  assumptions: Record<CostAssumptionKey, CostAssumption>; high_availability: boolean; lines: CostLine[];
  production: CostRange; staging: CostRange; total: CostRange; annual: CostRange; by_category: Record<string, number>;
  drivers: { name: string; monthly: number; share: number }[]; unpriced: string[]; disclaimer: string;
  budget?: { monthly: number; source: string; text: string; status: "within" | "at_risk" | "over" };
  recommendations: CostRecommendation[]; questions: string[];
}

// ---- integrations (GitHub, Confluence, Slack, Teams) ----
export type IntegrationKind = "github" | "confluence" | "slack" | "teams";
export interface IntegrationState {
  kind: IntegrationKind; label: string; configured: boolean; config: Record<string, any>; has_secret: boolean;
  last4: string | null; updated_at: string | null;
}
export interface IntegrationSettings {
  encryption: boolean; events: Record<string, string>;
  jira: { configured: boolean; site_url: string; email: string };
  integrations: Record<IntegrationKind, IntegrationState>;
}
export interface ExternalLinks { connected: boolean; repo?: string; space_key?: string; links: JiraLink[] }
export interface GitHubPlanItem {
  ref: string; action: "create" | "update" | "skip" | "close"; title: string; labels: string[]; body: string;
  external_key: string; external_url: string; status: string;
}
export interface GitHubPlan {
  items: GitHubPlanItem[]; counts: Record<"create" | "update" | "skip" | "close", number>; repo: string; include_nfr: boolean;
}
export interface GitHubPushResult {
  repo: string; counts: { created: number; updated: number; closed: number; unchanged: number; failed: number };
  results: { ref: string; title: string; action: string; status: "ok" | "skipped" | "error"; key: string; url: string; error: string }[];
}
export interface ConfluencePublish {
  title: string; space_key: string; action: "create" | "update" | "skip"; chars: number; url: string; page_id: string;
  dry_run: boolean; preview?: string; version?: number;
}

// ---- Jira Cloud push ----
export interface JiraSettings {
  /** False when the server cannot encrypt the token (no ARCHFLOW_SECRET_KEY): the connection cannot be saved. */
  enabled: boolean; configured: boolean; site_url: string; email: string; project_key: string;
  last4: string | null; updated_at: string | null;
  /** Live status from Jira webhooks: whether a secret exists (it is only shown once, when created). */
  webhook?: { configured: boolean; created_at: string | null; url: string };
}
export interface JiraTestResult {
  ok: boolean; account?: string; error?: string;
  project?: { key: string; name: string; issue_types: string[]; story_points: boolean };
}
export type JiraObjectKind = "epic" | "requirement" | "subtask" | "link";
export interface JiraLink {
  object_kind: JiraObjectKind; object_id: string; external_key: string; external_url: string; content_hash: string;
  status: string; status_category: string; status_checked_at: string | null; updated_at: string;
}
export interface JiraLinks { connected: boolean; site_url: string; project_key: string; links: JiraLink[] }
export interface JiraGwt { given: string; when: string; then: string }
export interface JiraPlanItem {
  ref: string; type: "epic" | "story" | "subtask" | "link"; object_kind: JiraObjectKind; object_id: string;
  summary: string; description?: string; labels?: string[]; components?: string[]; parent: string | null;
  acceptance_criteria?: JiraGwt[]; story_points?: number; points_basis?: string;
  action: "create" | "update" | "skip"; external_key: string; external_url: string;
}
export interface JiraPlan {
  engine: "llm" | "offline"; items: JiraPlanItem[]; counts: Record<"create" | "update" | "skip", number>;
  by_type: Record<"epic" | "story" | "subtask" | "link", number>; mapping: string; project_key: string; site_url: string;
}
// ---- sign-in security: two-step sign-in (MFA), single sign-on, project roles ----
export interface MeSecurity { auth_method: string; mfa_enabled: boolean; mfa_required: boolean; sso_required_orgs: string[] }
export interface MfaChallenge { mfa_required: true; mfa_token: string }
export interface MfaStatus {
  enabled: boolean; pending: boolean; recovery_codes_left: number; available: boolean;
  required_by: { id: string; name: string }[]; recovery_codes?: string[];
}
export interface SsoSettings {
  enabled: boolean; issuer: string; client_id: string; secret_set: boolean; secret_last4: string | null;
  domains: string[]; default_role: Role; require_sso: boolean;
}
export interface AuthSettings { sso: SsoSettings; require_mfa: boolean; callback_url: string; secrets_enabled: boolean; updated_at: string | null }
export type ProjectRole = "viewer" | "reviewer" | "developer" | "admin";
export interface ProjectRoleMember {
  user_id: string; name: string; email: string; org_role: Role; project_role: ProjectRole | null; effective_role: Role;
}
export interface ProjectRoles { members: ProjectRoleMember[]; roles: ProjectRole[]; can_manage: boolean }

export interface JiraPushResult {
  project_key: string; engine: string;
  counts: { created: number; updated: number; unchanged: number; failed: number };
  results: { ref: string; type: string; summary: string; action: string; status: "ok" | "skipped" | "error"; key: string; url: string; error: string }[];
}

// ---- Jira two-way sync (webhooks, pulling issues in) ----
export interface JiraWebhookSetup {
  secret: string; url: string; jql: string; events: string[]; steps: string[]; settings: JiraSettings;
}
export interface JiraIssuePreview {
  key: string; url: string; summary: string; issue_type: string; priority: string; labels: string[];
  status: string; status_category: string; description: string;
  requirement: Pick<Requirement, "title" | "description" | "kind" | "category" | "priority" | "actors" | "acceptance_criteria" | "status">;
}
export interface JiraIssuePage {
  project_key: string; jql: string; issues: JiraIssuePreview[]; next_page_token: string | null; hidden_linked: number;
}
export interface JiraImportResult {
  imported: { key: string; requirement_id: string; title: string; kind: string }[];
  skipped: { key: string; reason: string }[]; engine: "llm" | "offline"; links: JiraLink[];
}

// ---- compliance frameworks ----
export type ComplianceStatus = "pass" | "fail" | "needs_evidence" | "not_applicable";
export type ComplianceEffective = ComplianceStatus | "met";
export interface ComplianceEvidence { status: "" | "met" | "not_applicable"; note: string; updated_by: string | null; updated_at: string }
export interface ComplianceControl {
  id: string; title: string; description: string; checks: string[]; status: ComplianceStatus; effective: ComplianceEffective;
  evidence: ComplianceEvidence | null; summary: string; components: string[]; component_names: string[];
  requirements: string[]; fixes: string[];
}
export interface ComplianceFramework {
  id: string; name: string; version: string; description: string; score: number | null;
  counts: Record<"pass" | "met" | "needs_evidence" | "fail" | "not_applicable", number>; controls: ComplianceControl[];
}
export interface ComplianceProposal {
  key: string; title: string; description: string; kind: "non_functional"; category: string;
  priority: "low" | "medium" | "high" | "critical"; acceptance_criteria: string[]; controls: string[];
}
export interface ComplianceReport {
  branch: string; saved: boolean; selected: string[]; suggested: string[];
  available: { id: string; name: string; version: string; description: string }[];
  frameworks: ComplianceFramework[]; proposals: ComplianceProposal[]; disclaimer: string;
}
export interface ComplianceRemediation {
  summary: string; engine: "llm" | "offline"; questions: string[]; requirements: ComplianceProposal[];
  steps: { controls: string[]; title: string; detail: string; components: string[]; effort: "low" | "medium" | "high" }[];
}

// ---- generated artifacts (infrastructure as code, tests, documents) ----
export type GenerationKind = "iac" | "tests" | "docs";
export type GenerationTarget = "terraform" | "compose" | "k8s" | "gherkin" | "pytest" | "playwright" | "srs" | "brd" | "runbook";
export interface GeneratedFile { path: string; content: string }
export interface GenerationResult {
  kind: GenerationKind; target: GenerationTarget; branch: string; engine: "ai" | "offline"; cloud?: "aws" | "azure" | "gcp";
  files: GeneratedFile[]; notes: string[]; checks: { path: string; ok: boolean; detail: string }[]; valid: boolean;
}
