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
};

export type Confidence = "high" | "medium" | "low" | "none";

export type ChatResponse = {
  conversation_id: string;
  reply: string;
  provider: string;
  model: string;
  sources: ChatSource[];
  confidence: Confidence;
  confidence_score: number;
  providers_attempted: string[];
  conflicts_detected?: boolean;
  context_intent?: string | null;
  context_confidence?: string | null;
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

export type KnowledgeClaimItem = {
  id: string;
  claim_text: string;
  status: ClaimStatus;
  confidence: ClaimConfidence;
  grounding_score: number;
  chunk_id: string | null;
  created_at: string;
};

export type KnowledgeSourceDetail = KnowledgeSourceItem & {
  versions: KnowledgeVersionItem[];
  relationships: SourceRelationshipItem[];
  chunk_preview: string[];
  claims: KnowledgeClaimItem[];
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
  failure_reason: string | null;
  manifest: Record<string, unknown> | null;
  file_results: FileOutcome[] | null;
  started_at: string | null;
  completed_at: string | null;
  created_at: string;
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

export type LibraryListFilters = {
  project_id?: string;
  classification?: string;
  active_truth_status?: string;
  q?: string;
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

export type ProviderStatus = {
  name: string;
  configured: boolean;
  active_chat: boolean;
  active_embedding: boolean;
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
  usageSummary: () => request<UsageSummaryRow[]>("/api/admin/usage/summary"),

  importToLibrary: (file: File, projectId?: string) => {
    const form = new FormData();
    form.append("file", file);
    const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
    return request<ImportJobItem>(`/api/library/import${qs}`, { method: "POST", body: form });
  },
  getImportJob: (id: string) => request<ImportJobItem>(`/api/library/jobs/${id}`),
  listLibrary: (filters: LibraryListFilters = {}) => {
    const qs = new URLSearchParams(Object.entries(filters).filter(([, v]) => !!v) as [string, string][]).toString();
    return request<KnowledgeSourceItem[]>(`/api/library${qs ? `?${qs}` : ""}`);
  },
  getLibrarySource: (id: string) => request<KnowledgeSourceDetail>(`/api/library/${id}`),
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
    return request<LibrarySearchHit[]>(`/api/library/search/hybrid?${qs}`);
  },

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
};
