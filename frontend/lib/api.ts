import { getCsrfToken, setCsrfToken } from "@/lib/auth";

// Empty by default: fetch(`${API_URL}${path}`) then becomes a relative same-origin request
// (e.g. `/api/auth/register`), handled by this Next.js server's own proxy route
// (app/api/[...path]/route.ts), which forwards it server-side to the real backend — the
// browser never learns the backend's actual address, and the session cookie stays same-origin
// (see that file's comments). NEXT_PUBLIC_API_URL remains a deliberate escape hatch for a
// deployment that calls the backend directly cross-origin instead (e.g. a frontend hosted
// somewhere that can't reach the backend server-side, like Vercel without the proxy's
// INTERNAL_API_URL configured) — do not set it for a same-origin/proxied deployment.
const API_URL = process.env.NEXT_PUBLIC_API_URL || "";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export class AuthError extends Error {}

// Thrown when fetch() itself rejects — DNS failure, connection refused, TLS failure, CORS
// preflight rejection, mixed-content block, offline, etc. The browser's own message for this
// (e.g. "Failed to fetch", "NetworkError when attempting to fetch resource", "Load failed")
// is not something a user can act on and may hint at internal infra, so it's never shown —
// callers only ever see this generic, safe Swedish message.
export class NetworkError extends Error {
  constructor() {
    super("Kunde inte nå servern. Kontrollera din internetanslutning och försök igen.");
    this.name = "NetworkError";
  }
}

// Deduplicates concurrent refresh attempts — if several requests 401 at once (e.g. several
// widgets fetching in parallel right as the access token expires), only one refresh call
// goes out; the rest wait on the same in-flight promise instead of racing each other.
let refreshInFlight: Promise<boolean> | null = null;

function csrfHeader(): Record<string, string> {
  const token = getCsrfToken();
  return token ? { "X-CSRF-Token": token } : {};
}

async function attemptRefresh(): Promise<boolean> {
  if (!refreshInFlight) {
    refreshInFlight = fetch(`${API_URL}/api/auth/refresh`, {
      method: "POST",
      credentials: "include",
      headers: { ...csrfHeader() },
    })
      .then(async (res) => {
        if (!res.ok) return false;
        // Raw fetch, not the shared request() helper (to avoid recursing back through its
        // own 401-triggers-refresh logic) — so the csrf_token capture has to happen here
        // too, not just in request().
        const body = await res.json().catch(() => null);
        if (body && typeof body.csrf_token === "string") setCsrfToken(body.csrf_token);
        return true;
      })
      .catch(() => false)
      .finally(() => {
        refreshInFlight = null;
      });
  }
  return refreshInFlight;
}

async function request<T>(path: string, options: RequestInit = {}, isRetry = false): Promise<T> {
  const method = (options.method || "GET").toUpperCase();
  const headers: Record<string, string> = { ...(options.headers as Record<string, string> | undefined) };
  if (!(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  if (MUTATING_METHODS.has(method)) {
    Object.assign(headers, csrfHeader());
  }

  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, {
      ...options,
      headers,
      // Sends and receives the HttpOnly session cookies cross-origin. Nothing about the
      // session ever needs to be read or stored by this JS — the browser handles it.
      credentials: "include",
      cache: "no-store",
    });
  } catch {
    throw new NetworkError();
  }

  const isAuthEndpoint = path === "/api/auth/login" || path === "/api/auth/refresh";
  if (res.status === 401 && !isRetry && !isAuthEndpoint) {
    const refreshed = await attemptRefresh();
    if (refreshed) {
      return request<T>(path, options, true);
    }
    // Guarded by the pathname check: without it, the login page's own "am I already
    // logged in?" probe (api.me() in app/login/page.tsx) would 401, fail to refresh, and
    // force-navigate to /login — reloading the very page that made the call, which reruns
    // the same probe, which redirects again, forever.
    if (typeof window !== "undefined" && window.location.pathname !== "/login") {
      window.location.href = "/login";
    }
    throw new AuthError("Sessionen har gått ut. Logga in igen.");
  }

  if (!res.ok) {
    const body = await res.text();
    // FastAPI's HTTPException(detail=...) is always a clean, already-Swedish, user-facing
    // string — surface it directly rather than wrapping it in a generic "API error" message,
    // so forms (register, reset-password, ...) can show it as-is.
    let detail: string | undefined;
    try {
      const parsed = JSON.parse(body);
      if (parsed && typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      /* not JSON, fall through to the generic message */
    }
    const err = new Error(detail ?? `API-fel (${res.status}): ${body}`);
    (err as Error & { status?: number }).status = res.status;
    throw err;
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  const parsed = text ? (JSON.parse(text) as T) : (undefined as T);

  // login/refresh/me all include the current csrf_token in their response body — this is
  // the ONLY place it's ever transmitted (see lib/auth.ts). Capturing it generically here,
  // rather than in each call site, means every response that carries a fresh value updates
  // the in-memory copy uniformly.
  if (parsed && typeof parsed === "object" && "csrf_token" in parsed) {
    setCsrfToken((parsed as { csrf_token: string }).csrf_token);
  }

  return parsed;
}

export type ChatSource = {
  document_id: string;
  title: string;
  snippet: string;
  score: number;
  active_truth_status?: string | null;
  start_seconds?: number | null;
  end_seconds?: number | null;
};

export type Confidence = "high" | "medium" | "low" | "none";

export type AssistantStatus = "succeeded" | "failed";

// See backend/app/rag/context_status.py — the verified reason retrieval found nothing (or
// couldn't run), classified from real IndexStatus/worker-heartbeat/provider signals, never
// invented. Present only when retrieval returned zero hits; absent when real sources were
// found (nothing to explain).
export type ContextStatusReason =
  | "worker_unavailable"
  | "files_processing"
  | "awaiting_provider"
  | "search_provider_unavailable"
  | "indexing_failed"
  | "no_relevant_match"
  | "no_documents";

export type ContextStatus = {
  reason: ContextStatusReason;
  message: string;
  pending_count: number;
  awaiting_provider_count: number;
  failed_count: number;
  indexed_count: number;
  total_document_count: number;
  worker_reachable: boolean | null;
};

// The user's message and the assistant's reply are two independently durable steps — see
// backend/app/routers/chat.py's module docstring. `user_message_saved` is always true if this
// response exists at all; `reply`/`provider`/`model`/`sources`/`confidence*` are only present
// when assistant_status is "succeeded". Never infer "my message is gone" from a failed
// assistant_status — it never was, and never will be, lost because of a provider error.
export type ChatResponse = {
  conversation_id: string;
  user_message_id: string;
  user_message_saved: boolean;
  assistant_status: AssistantStatus;
  assistant_message_id: string | null;
  reply: string | null;
  provider: string | null;
  model: string | null;
  sources: ChatSource[];
  confidence: Confidence | null;
  confidence_score: number | null;
  providers_attempted: string[];
  conflicts_detected?: boolean;
  context_intent?: string | null;
  context_confidence?: string | null;
  context_status?: ContextStatus | null;
  error_category: string | null;
  error_message: string | null;
  retryable: boolean;
};

export type ConversationItem = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

export type ConversationMessage = {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  provider: string | null;
  model: string | null;
  created_at: string;
};

export type ConversationDetail = ConversationItem & {
  messages: ConversationMessage[];
};

export type DocumentItem = {
  id: string;
  title: string;
  source: string;
  category: string | null;
  status: string;
  chunk_count: number;
  created_at: string;
};

export type ProjectItem = {
  id: string;
  name: string;
  description: string | null;
  status: string;
  created_at: string;
};

// --- Founder Knowledge Studio v1 (see docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md) ---

export type Classification = "vision" | "architecture" | "decisions" | "history" | "security" | "general";
export type ActiveTruthStatus = "active" | "historical" | "proposed" | "superseded" | "disputed";

export type KnowledgeSourceItem = {
  id: string;
  title: string;
  source: string;
  media_type: string | null;
  original_filename: string | null;
  category: string | null;
  classification: Classification;
  active_truth_status: ActiveTruthStatus;
  status: string;
  chunk_count: number;
  checksum: string | null;
  project_id: string | null;
  version_number: number;
  error_message: string | null;
  created_at: string;
  updated_at: string;
  imported_at: string | null;
  media_duration_seconds: number | null;
  transcript_provider: string | null;
};

export type KnowledgeVersionItem = {
  id: string;
  version_number: number;
  checksum: string;
  extraction_version: string;
  raw_metadata: Record<string, unknown> | null;
  created_at: string;
};

export type SourceRelationshipItem = {
  id: string;
  from_source_id: string;
  to_source_id: string;
  relationship_type: string;
  note: string | null;
  created_at: string;
};

export type ClaimConfidence = "certain" | "likely" | "uncertain" | "conflict" | "no_basis";
export type ClaimStatus = "active" | "historical" | "proposed" | "disputed";
// P3 (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.1): what the claim actually IS, distinct
// from ClaimStatus (whether it's currently settled) and ClaimConfidence (how well-supported).
export type ClaimType = "idea" | "decision" | "task_reference" | "vision" | "technical" | "historical" | "uncategorized";

export type KnowledgeClaimItem = {
  id: string;
  claim_text: string;
  claim_type: ClaimType;
  status: ClaimStatus;
  confidence: ClaimConfidence;
  grounding_score: number;
  chunk_id: string | null;
  created_at: string;
};

export type MediaSegmentItem = {
  chunk_index: number;
  text: string;
  start_seconds: number | null;
  end_seconds: number | null;
};

export type KnowledgeSourceDetail = KnowledgeSourceItem & {
  versions: KnowledgeVersionItem[];
  relationships: SourceRelationshipItem[];
  chunk_preview: string[];
  claims: KnowledgeClaimItem[];
  segments: MediaSegmentItem[];
};

export type FileOutcome = {
  filename: string;
  status: "indexed" | "duplicate" | "failed" | "skipped";
  reason: string | null;
  source_id: string | null;
};

export type ImportJobItem = {
  id: string;
  status: "pending" | "running" | "completed" | "failed" | "partial";
  source_filename: string | null;
  source_checksum: string | null;
  progress_current: number;
  progress_total: number;
  succeeded_count: number;
  failed_count: number;
  skipped_count: number;
  blocked_count: number;
  failure_reason: string | null;
  manifest: Record<string, unknown> | null;
  file_results: FileOutcome[] | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
  attempt_count: number;
  max_attempts: number;
  last_failure_transient: boolean | null;
};

export type LibrarySearchHit = {
  document_id: string;
  title: string;
  text: string;
  score: number;
  classification: Classification;
  active_truth_status: ActiveTruthStatus;
  media_type: string | null;
  text_match: boolean;
};

// Search failure boundary: semantic_search_available=false means the embedding provider
// was unreachable and `results` came from the text-match (ILIKE) channel alone — the UI
// must show this as a degraded search, not silently render it as "semantic search found
// nothing" (see backend/app/schemas.py's LibrarySearchResponseOut).
export type LibrarySearchResponse = {
  results: LibrarySearchHit[];
  semantic_search_available: boolean;
  degraded_reason: string | null;
};

export type LibraryListFilters = {
  project_id?: string;
  classification?: string;
  active_truth_status?: string;
  q?: string;
};

// Durable-worker package: GET /api/library/ops/status — never carries private file paths or
// secrets (see backend/app/schemas.py's OpsStatusOut docstring), so it's safe to render
// as-is in the founder-only Library UI.
export type LibraryOpsStatus = {
  worker_reachable: boolean;
  queue_length: number;
  running_jobs: number;
  oldest_pending_age_seconds: number | null;
  failed_last_24h: number;
  storage_writable: boolean;
  free_disk_bytes: number | null;
  last_heartbeat_at: string | null;
};

// --- Founder Workbench (DEL 9) ---

export type WorkbenchLabel = "idea" | "proposal" | "decision" | "history";

export type WorkbenchAnalysis = {
  question: string;
  conclusion: string;
  critique: string | null;
  sources: ChatSource[];
  confidence: Confidence;
  confidence_score: number;
  conflicts_detected: boolean;
  provider: string;
  model: string;
};

export type TaskItem = {
  id: string;
  project_id: string | null;
  title: string;
  description: string | null;
  status: string;
  priority: string;
  suggested_by_ai: boolean;
  created_at: string;
};

export type ProviderVerification = {
  result: "ok" | "invalid_key" | "unreachable" | "rate_limited" | "unsupported" | "not_configured";
  message: string;
  checked_by: "system" | "founder";
  checked_at: string;
};

export type ProviderStatus = {
  name: string;
  configured: boolean;
  active_chat: boolean;
  active_embedding: boolean;
  chat_verification: ProviderVerification | null;
  embedding_verification: ProviderVerification | null;
};

export type UsageSummaryRow = {
  provider: string;
  model: string;
  role: string;
  request_count: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost_usd: number | null;
};

export type CurrentUser = {
  id: string;
  email: string;
  role: string;
  email_verified: boolean;
};

export type MessageResponse = { detail: string };

// MainAI Project Memory & Coordination Loop (see backend/app/project_memory.py) — the
// project's own durable memory of its state, not per-user data, hence founder-only like
// the rest of /api/admin/*.
export type ProjectNoteKind = "fact" | "decision" | "blocker" | "next_step" | "uncertainty" | "idea";
export type SideIssueClassification =
  | "blocking"
  | "directly_resolvable"
  | "registered_for_later"
  | "needs_founder_decision";

export type ProjectNote = {
  id: string;
  kind: ProjectNoteKind;
  status: "open" | "resolved" | "superseded";
  content: string;
  source_type: string;
  source_ref: string;
  source_id: string | null;
  classification: SideIssueClassification | null;
  created_by: string;
  created_at: string;
  resolved_at: string | null;
  resolved_by: string | null;
  resolution_note: string | null;
};

export type ProjectCheckpoint = {
  id: string;
  summary: string;
  branch_name: string;
  open_pr_refs: string;
  brief_storage_key: string;
  brief_sha256: string;
  git_commit_sha: string | null;
  created_by: string;
  created_at: string;
};

export type ProjectCheckpointDetail = ProjectCheckpoint & { brief: string };

export type ProjectBranchPRStatus = {
  id: string;
  kind: "branch" | "pr";
  ref: string;
  title: string | null;
  status: string;
  base_ref: string | null;
  head_ref: string | null;
  mergeable: boolean | null;
  ci_status: string | null;
  summary: string | null;
  is_current: boolean;
  recorded_by: string;
  recorded_at: string;
  superseded_at: string | null;
};

export type ProjectConflicts = {
  duplicate_work_candidates: Record<string, unknown>[];
  data_integrity_issues: Record<string, unknown>[];
};

// MainAI Core: agent orchestration (see backend/app/agent_orchestration.py) — one scoped
// work order MainAI created for a code/review agent, and its append-only event history.
export type AgentTaskStatus =
  | "created"
  | "dispatched"
  | "result_recorded"
  | "reviewed_approved"
  | "reviewed_needs_correction"
  | "reviewed_rejected"
  | "pr_prepared"
  | "pr_opened"
  | "ready_for_human";

export type AgentTask = {
  id: string;
  title: string;
  description: string;
  target_files: string[];
  constraints: string | null;
  acceptance_criteria: string;
  required_tests: string | null;
  status: AgentTaskStatus;
  source_note_id: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
};

export type AgentTaskEvent = {
  id: string;
  task_id: string;
  event_type: string;
  role: "code" | "review" | null;
  provider: string | null;
  model: string | null;
  payload: Record<string, unknown>;
  created_by: string;
  created_at: string;
};

export type AgentTaskDetail = AgentTask & { events: AgentTaskEvent[] };

// MainAI Runtime Truthfulness and Durable Job Foundation (see
// backend/docs/MAINAI_JOB_RUNTIME.md, backend/app/models/mainai_job.py) — a durable job the
// founder can observe/cancel/retry independently of whatever MainAI claims about its own
// state. `owner_id` is only ever populated by the admin listing (RLS already scopes every
// other endpoint to exactly one owner).
export type MainAIJobStatus = "queued" | "running" | "paused" | "completed" | "failed" | "cancelled";

export type MainAIJob = {
  id: string;
  owner_id: string | null;
  job_type: string;
  status: MainAIJobStatus;
  created_at: string;
  started_at: string | null;
  last_heartbeat_at: string | null;
  completed_at: string | null;
  progress_current: number;
  progress_total: number | null;
  current_phase: string | null;
  public_message: string | null;
  error_category: string | null;
  retry_count: number;
  max_retries: number;
  input_refs: Record<string, unknown>[];
  output_refs: Record<string, unknown>[];
  provider: string | null;
  model: string | null;
  cancel_requested: boolean;
  cancel_acknowledged: boolean;
  created_by: string;
};

export type MainAIJobEvent = {
  id: string;
  job_id: string;
  event_type: string;
  detail: Record<string, unknown>;
  created_at: string;
};

export type MainAIJobDetail = MainAIJob & { events: MainAIJobEvent[] };

export type MainAIJobProposal = {
  id: string;
  job_id: string;
  source_document_id: string | null;
  source_chunk_id: string | null;
  proposal_type: string;
  proposal_text: string;
  status: "proposed" | "dismissed";
  created_at: string;
};

// Statuses a cancel button should ever be shown for — mirrors
// CANCELLABLE_MAINAI_JOB_STATUSES in backend/app/models/mainai_job.py. Kept here rather than
// re-derived so the UI can never show a cancel button the API would reject.
export const CANCELLABLE_MAINAI_JOB_STATUSES: MainAIJobStatus[] = ["queued", "running", "paused"];
export const RETRYABLE_MAINAI_JOB_STATUSES: MainAIJobStatus[] = ["failed"];

// MainAI Execution Loop V0.1 (see backend/app/routers/mainai_execution.py,
// backend/app/mainai_execution/*). A goal's own richer status vocabulary than MainAIJobStatus
// above -- see backend/app/models/mainai_execution.py's MainAIGoalStatus/MainAITaskStatus
// docstrings for why a task can be `blocked`/`waiting_external`/`waiting_ci`, not just
// queued/running/completed/failed/cancelled.
export type MainAIGoal = {
  id: string;
  title: string;
  status: string;
  risk_level: string;
  approval_policy: string;
  current_plan_version: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  final_outcome: string | null;
};

export type MainAIPlan = {
  id: string;
  version: number;
  status: string;
  rationale: string;
  created_at: string;
};

// Computed by the backend from mainai_jobs.lease_expires_at, never stored -- see
// backend/app/mainai_execution/liveness.py's TaskLiveness enum.
export type MainAITaskLiveness = "idle" | "running" | "stalled" | "dead" | "waiting_external" | "waiting_ci" | "done";

export type MainAITask = {
  id: string;
  goal_id: string;
  plan_id: string;
  description: string;
  task_type: string;
  status: string;
  priority: number;
  risk_level: string;
  approval_required: boolean;
  verification_plan: Record<string, unknown>[];
  attempts: number;
  max_attempts: number;
  blocker_reason: string | null;
  mainai_job_id: string | null;
  // V0.3: when this task becomes eligible for the worker's automatic retry-with-backoff scan
  // -- null means no automatic retry is currently scheduled.
  next_retry_at: string | null;
  liveness: MainAITaskLiveness;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

export type MainAIGoalDetail = MainAIGoal & { plan: MainAIPlan | null; tasks: MainAITask[] };

export type MainAITaskEvent = {
  id: string;
  event_type: string;
  detail: Record<string, unknown>;
  created_at: string;
};

export type MainAICheckpoint = {
  id: string;
  plan_version: number;
  executor_state: Record<string, unknown>;
  test_status: string | null;
  ci_status: string | null;
  blocker: string | null;
  created_at: string;
};

export type MainAITaskDetail = MainAITask & {
  events: MainAITaskEvent[];
  checkpoints: MainAICheckpoint[];
  depends_on: string[];
  approval_granted: boolean;
};

// V0.3 -- app/mainai_execution/ci_wait.py's durable external-wait record.
export type MainAITaskWait = {
  id: string;
  task_id: string;
  job_id: string;
  source_type: string;
  status: string;
  resource_ref: Record<string, unknown>;
  poll_count: number;
  last_polled_at: string | null;
  next_poll_at: string;
  deadline_at: string;
  evidence: Record<string, unknown>;
  created_at: string;
  resolved_at: string | null;
};

// V0.3 -- founder-wide engineering lesson (app/mainai_execution/lessons.py), including
// `disputed` once app/mainai_execution/lesson_conflicts.py's mark_conflict() has run.
export type EngineeringLesson = {
  id: string;
  status: string;
  problem: string;
  root_cause: string;
  affected_component: string;
  severity: string;
  evidence: string;
  fix: string;
  regression_test: string | null;
  general_rule: string;
  applies_to: string[];
  source_type: string;
  source_ref: string;
  confidence: string;
  created_by: string;
  created_at: string;
  superseded_by: string | null;
};

// The shape app/mainai_execution/final_report.py's generate_goal_report() returns -- kept
// loose (not every nested field typed) since it is durable-state aggregation, not a stable
// wire contract the UI depends on field-by-field.
export type MainAIGoalReport = {
  goal: Record<string, unknown>;
  plan: Record<string, unknown> | null;
  tasks: Record<string, unknown>[];
  summary: { total_tasks: number; by_outcome: Record<string, number>; unresolved_risk_count: number };
  generated_at: string;
};

export const api = {
  login: (email: string, password: string) =>
    request<CurrentUser>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  verifyEmail: (token: string) =>
    request<{ status: string }>("/api/auth/verify-email", {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  resendVerification: (email: string) =>
    request<MessageResponse>("/api/auth/resend-verification", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  forgotPassword: (email: string) =>
    request<MessageResponse>("/api/auth/forgot-password", {
      method: "POST",
      body: JSON.stringify({ email }),
    }),
  resetPassword: (token: string, newPassword: string) =>
    request<{ status: string }>("/api/auth/reset-password", {
      method: "POST",
      body: JSON.stringify({ token, new_password: newPassword }),
    }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  logoutAll: () => request("/api/auth/logout-all", { method: "POST" }),
  me: () => request<CurrentUser>("/api/auth/me"),

  exportAccount: () => request<Record<string, unknown>>("/api/account/export"),
  deleteAccount: (password: string) =>
    request("/api/account", { method: "DELETE", body: JSON.stringify({ password }) }),

  sendChat: (message: string, conversationId?: string) =>
    request<ChatResponse>("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message, conversation_id: conversationId }),
    }),
  retryChatMessage: (userMessageId: string) =>
    request<ChatResponse>(`/api/chat/messages/${userMessageId}/retry`, { method: "POST" }),

  listConversations: () => request<ConversationItem[]>("/api/conversations"),
  getConversation: (id: string) => request<ConversationDetail>(`/api/conversations/${id}`),
  deleteConversation: (id: string) => request(`/api/conversations/${id}`, { method: "DELETE" }),

  listDocuments: () => request<DocumentItem[]>("/api/documents"),

  uploadDocument: (file: File, category?: string) => {
    const form = new FormData();
    form.append("file", file);
    const qs = category ? `?category=${encodeURIComponent(category)}` : "";
    return request<DocumentItem>(`/api/documents/upload${qs}`, { method: "POST", body: form });
  },

  deleteDocument: (id: string) => request(`/api/documents/${id}`, { method: "DELETE" }),

  listProjects: () => request<ProjectItem[]>("/api/projects"),
  createProject: (payload: Partial<ProjectItem>) =>
    request<ProjectItem>("/api/projects", { method: "POST", body: JSON.stringify(payload) }),

  listTasks: () => request<TaskItem[]>("/api/projects/tasks"),
  createTask: (payload: Partial<TaskItem>) =>
    request<TaskItem>("/api/projects/tasks", { method: "POST", body: JSON.stringify(payload) }),
  updateTask: (id: string, payload: Partial<TaskItem>) =>
    request<TaskItem>(`/api/projects/tasks/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),

  searchKnowledge: (query: string) =>
    request<{ document_id: string; title: string; text: string; score: number }[]>("/api/knowledge/search", {
      method: "POST",
      body: JSON.stringify({ query }),
    }),

  providerStatus: () => request<ProviderStatus[]>("/api/admin/providers/status"),
  setProviderConfig: (role: "chat" | "embedding", provider: string, model: string) =>
    request("/api/admin/providers/config", {
      method: "PUT",
      body: JSON.stringify({ role, provider, model }),
    }),
  // P1: "Testa nu" — an explicit, real verification call, bypassing the cache.
  verifyProvider: (provider: string, role: "chat" | "embedding") =>
    request<ProviderVerification>("/api/admin/providers/verify", {
      method: "POST",
      body: JSON.stringify({ provider, role }),
    }),
  usageSummary: () => request<UsageSummaryRow[]>("/api/admin/usage/summary"),

  importToLibrary: (file: File, projectId?: string) => {
    const form = new FormData();
    form.append("file", file);
    const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    return request<ImportJobItem>(`/api/library/import${qs}`, { method: "POST", body: form });
  },
  getImportJob: (id: string) => request<ImportJobItem>(`/api/library/jobs/${id}`),
  // DEL 3: the founder's recent/active import jobs, newest first — lets the upload queue
  // (frontend/lib/uploadQueue.tsx) recover real server state after a reload or fresh login.
  listImportJobs: () => request<ImportJobItem[]>("/api/library/jobs"),
  listLibrary: (filters: LibraryListFilters = {}) => {
    const qs = new URLSearchParams(Object.entries(filters).filter(([, v]) => !!v) as [string, string][]).toString();
    return request<KnowledgeSourceItem[]>(`/api/library${qs ? `?${qs}` : ""}`);
  },
  getLibrarySource: (id: string) => request<KnowledgeSourceDetail>(`/api/library/${id}`),
  // Not a request() call: this is a direct <audio>/<video> src URL. The browser attaches
  // the session cookie itself for a same-origin request (API_URL is empty in the normal
  // same-origin-proxied deployment — see this file's top comment), so no fetch/blob-URL
  // indirection is needed here.
  getLibraryMediaUrl: (id: string) => `${API_URL}/api/library/${id}/media`,
  deleteLibrarySource: (id: string) =>
    request(`/api/library/${id}`, { method: "DELETE", body: JSON.stringify({ confirm: true }) }),
  createSourceRelationship: (sourceId: string, toSourceId: string, relationshipType: string, note?: string) =>
    request<SourceRelationshipItem>(`/api/library/${sourceId}/relationships`, {
      method: "POST",
      body: JSON.stringify({ to_source_id: toSourceId, relationship_type: relationshipType, note }),
    }),
  searchLibrary: (query: string, filters: LibraryListFilters = {}) => {
    const qs = new URLSearchParams({
      q: query,
      ...Object.fromEntries(Object.entries(filters).filter(([, v]) => !!v)),
    } as Record<string, string>).toString();
    return request<LibrarySearchResponse>(`/api/library/search/hybrid?${qs}`);
  },
  getLibraryOpsStatus: () => request<LibraryOpsStatus>("/api/library/ops/status"),

  analyzeWorkbench: (question: string, projectId?: string, documentId?: string) =>
    request<WorkbenchAnalysis>("/api/workbench/analyze", {
      method: "POST",
      body: JSON.stringify({ question, project_id: projectId || null, document_id: documentId || null }),
    }),
  saveWorkbenchResult: (payload: {
    question: string;
    conclusion: string;
    critique?: string | null;
    label: WorkbenchLabel;
    project_id?: string | null;
    source_document_ids?: string[];
  }) => request<KnowledgeSourceItem>("/api/workbench/save", { method: "POST", body: JSON.stringify(payload) }),

  // Fas 4: minimal founder-only view onto the MainAI Project Memory & Coordination Loop.
  memoryLatestCheckpoint: () => request<ProjectCheckpointDetail>("/api/admin/memory/checkpoints/latest"),
  memoryCreateCheckpoint: (payload: { summary: string; branch_name: string; open_pr_refs: string[] }) =>
    request<ProjectCheckpointDetail>("/api/admin/memory/checkpoints", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  memoryCheckpointStale: (id: string) =>
    request<{ stale: boolean; reasons: string[] }>(`/api/admin/memory/checkpoints/${id}/stale`),
  memoryNotes: (status: "open" | "all" = "open") => request<ProjectNote[]>(`/api/admin/memory/notes?status=${status}`),
  memoryBranchPrStatus: () => request<ProjectBranchPRStatus[]>("/api/admin/memory/branch-pr-status"),
  memoryConflicts: () => request<ProjectConflicts>("/api/admin/memory/conflicts"),

  // MainAI Core: agent orchestration (see backend/app/agent_orchestration.py).
  agentTasks: () => request<AgentTask[]>("/api/admin/agents/tasks"),
  agentTaskDetail: (id: string) => request<AgentTaskDetail>(`/api/admin/agents/tasks/${id}`),
  agentCreateTask: (payload: {
    title: string;
    description: string;
    acceptance_criteria: string;
    target_files?: string[];
    constraints?: string | null;
    required_tests?: string | null;
    source_note_id?: string | null;
  }) => request<AgentTask>("/api/admin/agents/tasks", { method: "POST", body: JSON.stringify(payload) }),
  agentDispatchTask: (id: string) => request<AgentTaskEvent>(`/api/admin/agents/tasks/${id}/dispatch`, { method: "POST" }),
  agentRecordTestResults: (id: string, payload: { passed: boolean; output: string }) =>
    request<AgentTaskEvent>(`/api/admin/agents/tasks/${id}/test-results`, { method: "POST", body: JSON.stringify(payload) }),
  agentReviewTask: (id: string) => request<AgentTaskEvent>(`/api/admin/agents/tasks/${id}/review`, { method: "POST" }),
  agentPreparePr: (id: string, payload: { branch_name: string; base_branch: string }) =>
    request<Record<string, unknown>>(`/api/admin/agents/tasks/${id}/prepare-pr`, { method: "POST", body: JSON.stringify(payload) }),
  agentAttemptMerge: (id: string) =>
    request<{ merged: boolean; reason: string; conditions: string[] }>(`/api/admin/agents/tasks/${id}/attempt-merge`, { method: "POST" }),

  // MainAI Runtime Truthfulness and Durable Job Foundation (see
  // backend/docs/MAINAI_JOB_RUNTIME.md).
  mainaiJobs: (limit = 20, offset = 0) => request<MainAIJob[]>(`/api/mainai/jobs?limit=${limit}&offset=${offset}`),
  mainaiJobDetail: (id: string) => request<MainAIJobDetail>(`/api/mainai/jobs/${id}`),
  mainaiJobProposals: (id: string) => request<MainAIJobProposal[]>(`/api/mainai/jobs/${id}/proposals`),
  mainaiCreateJob: (payload: { job_type: string; input_refs: { type: string; id: string }[]; idempotency_key?: string }) =>
    request<MainAIJob>("/api/mainai/jobs", { method: "POST", body: JSON.stringify(payload) }),
  mainaiCancelJob: (id: string) => request<MainAIJob>(`/api/mainai/jobs/${id}/cancel`, { method: "POST" }),
  mainaiRetryJob: (id: string) => request<MainAIJob>(`/api/mainai/jobs/${id}/retry`, { method: "POST" }),
  mainaiJobsAdminAll: (limit = 20, offset = 0) => request<MainAIJob[]>(`/api/mainai/jobs/admin/all?limit=${limit}&offset=${offset}`),

  // MainAI Execution Loop V0.1 (see backend/app/routers/mainai_execution.py).
  mainaiExecutionGoals: (limit = 50, offset = 0) => request<MainAIGoal[]>(`/api/mainai/execution/goals?limit=${limit}&offset=${offset}`),
  mainaiExecutionCreateGoal: (payload: { title: string; original_instruction: string; risk_level?: string; approval_policy?: string }) =>
    request<MainAIGoal>("/api/mainai/execution/goals", { method: "POST", body: JSON.stringify(payload) }),
  mainaiExecutionGoalDetail: (id: string) => request<MainAIGoalDetail>(`/api/mainai/execution/goals/${id}`),
  mainaiExecutionCreatePlan: (goalId: string) => request<MainAIPlan>(`/api/mainai/execution/goals/${goalId}/plan`, { method: "POST" }),
  mainaiExecutionGoalReport: (goalId: string) => request<MainAIGoalReport>(`/api/mainai/execution/goals/${goalId}/report`),
  mainaiExecutionTaskDetail: (id: string) => request<MainAITaskDetail>(`/api/mainai/execution/tasks/${id}`),
  mainaiExecutionApproveTask: (id: string) => request<MainAITaskDetail>(`/api/mainai/execution/tasks/${id}/approve`, { method: "POST" }),
  mainaiExecutionRejectTask: (id: string) => request<MainAITaskDetail>(`/api/mainai/execution/tasks/${id}/reject`, { method: "POST" }),
  mainaiExecutionCancelTask: (id: string) => request<MainAITaskDetail>(`/api/mainai/execution/tasks/${id}/cancel`, { method: "POST" }),
  mainaiExecutionRetryTask: (id: string) => request<MainAITaskDetail>(`/api/mainai/execution/tasks/${id}/retry`, { method: "POST" }),
  // V0.3 additions
  mainaiExecutionGoalPlans: (goalId: string) => request<MainAIPlan[]>(`/api/mainai/execution/goals/${goalId}/plans`),
  mainaiExecutionTaskWaits: (id: string) => request<MainAITaskWait[]>(`/api/mainai/execution/tasks/${id}/waits`),
  mainaiExecutionLessons: (statusFilter?: string) =>
    request<EngineeringLesson[]>(`/api/mainai/execution/lessons${statusFilter ? `?status_filter=${statusFilter}` : ""}`),
};
