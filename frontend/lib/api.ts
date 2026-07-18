import { getCsrfToken, setCsrfToken } from "@/lib/auth";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
const MUTATING_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export class AuthError extends Error {}

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

  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    headers,
    // Sends and receives the HttpOnly session cookies cross-origin. Nothing about the
    // session ever needs to be read or stored by this JS — the browser handles it.
    credentials: "include",
    cache: "no-store",
  });

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
    throw new Error(`API-fel (${res.status}): ${body}`);
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
};

export const api = {
  login: (email: string, password: string) =>
    request<CurrentUser>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  logout: () => request("/api/auth/logout", { method: "POST" }),
  me: () => request<CurrentUser>("/api/auth/me"),

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
