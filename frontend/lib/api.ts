import { clearToken, getToken } from "@/lib/auth";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export class AuthError extends Error {}

function authHeaders(): Record<string, string> {
  const token = getToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(options.headers || {}) },
    cache: "no-store",
  });
  if (res.status === 401) {
    clearToken();
    if (typeof window !== "undefined") window.location.href = "/login";
    throw new AuthError("Sessionen har gått ut. Logga in igen.");
  }
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`API-fel (${res.status}): ${body}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
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
    request<{ access_token: string; token_type: string }>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
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

  uploadDocument: async (file: File, category?: string) => {
    const form = new FormData();
    form.append("file", file);
    const qs = category ? `?category=${encodeURIComponent(category)}` : "";
    const res = await fetch(`${API_URL}/api/documents/upload${qs}`, {
      method: "POST",
      headers: authHeaders(),
      body: form,
    });
    if (res.status === 401) {
      clearToken();
      if (typeof window !== "undefined") window.location.href = "/login";
      throw new AuthError("Sessionen har gått ut. Logga in igen.");
    }
    if (!res.ok) throw new Error(await res.text());
    return res.json() as Promise<DocumentItem>;
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
