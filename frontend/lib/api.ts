import { getCsrfToken, setCsrfToken } from "@/lib/auth";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
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
  register: (email: string, password: string, website: string = "") =>
    request<MessageResponse>("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password, website }),
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
};
