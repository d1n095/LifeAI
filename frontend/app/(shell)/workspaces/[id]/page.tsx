"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  api,
  ChatSource,
  ConversationItem,
  KnowledgeSourceItem,
  WorkspaceDetail,
} from "@/lib/api";

type ChatEntry = {
  role: "user" | "assistant";
  content: string;
  sources?: ChatSource[];
};

const SOURCE_STATUS_LABELS: Record<string, string> = {
  indexed: "Indexerad",
  received: "Mottagen",
  original_stored: "Original sparat",
  extracting: "Extraherar",
  extracted: "Extraherad",
  embedding: "Skapar embedding",
  awaiting_provider: "Väntar på leverantör",
  blocked_provider: "Blockerad av leverantör",
  storage_failed: "Lagring misslyckades",
  extraction_failed: "Extrahering misslyckades",
  indexing_failed: "Indexering misslyckades",
  failed: "Misslyckades",
  cancelled: "Avbruten",
};

export default function WorkspaceDetailPage() {
  const params = useParams<{ id: string }>();
  const workspaceId = params.id;

  const [workspace, setWorkspace] = useState<WorkspaceDetail | null>(null);
  const [library, setLibrary] = useState<KnowledgeSourceItem[]>([]);
  const [selectedDocument, setSelectedDocument] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [newTask, setNewTask] = useState("");
  const [decisionTitle, setDecisionTitle] = useState("");
  const [decisionText, setDecisionText] = useState("");

  const [conversations, setConversations] = useState<ConversationItem[]>([]);
  const [conversationId, setConversationId] = useState<string | undefined>();
  const [chatEntries, setChatEntries] = useState<ChatEntry[]>([]);
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);

  async function refresh() {
    try {
      setError(null);
      const detail = await api.getWorkspace(workspaceId);
      setWorkspace(detail);
      setConversations(detail.conversations);
      if (!conversationId && detail.conversations[0]) {
        setConversationId(detail.conversations[0].id);
        await loadConversation(detail.conversations[0].id);
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function loadConversation(id: string) {
    try {
      const detail = await api.getConversation(id);
      setConversationId(detail.id);
      setChatEntries(
        detail.messages
          .filter((entry) => entry.role !== "system")
          .map((entry) => ({
            role: entry.role as "user" | "assistant",
            content: entry.content,
          }))
      );
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    // Documented fetch-on-mount pattern used throughout the shell.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    api.listLibrary().then(setLibrary).catch(() => setLibrary([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function addTask() {
    if (!newTask.trim()) return;
    await run(async () => {
      await api.createWorkspaceTask(workspaceId, {
        title: newTask.trim(),
        status: "todo",
        priority: "medium",
      });
      setNewTask("");
    });
  }

  async function addDecision() {
    if (!decisionTitle.trim() || !decisionText.trim()) return;
    await run(async () => {
      await api.createWorkspaceDecision(workspaceId, {
        title: decisionTitle.trim(),
        decision: decisionText.trim(),
        status: "accepted",
      });
      setDecisionTitle("");
      setDecisionText("");
    });
  }

  async function associateDocument() {
    if (!selectedDocument) return;
    await run(async () => {
      await api.associateWorkspaceDocument(workspaceId, selectedDocument);
      setSelectedDocument("");
    });
  }

  async function startConversation() {
    await run(async () => {
      const conversation = await api.createWorkspaceConversation(workspaceId);
      setConversationId(conversation.id);
      setChatEntries([]);
    });
  }

  async function sendMessage() {
    const text = message.trim();
    if (!text || sending) return;
    setSending(true);
    setError(null);
    setMessage("");
    setChatEntries((entries) => [...entries, { role: "user", content: text }]);
    try {
      let activeConversationId = conversationId;
      if (!activeConversationId) {
        const conversation = await api.createWorkspaceConversation(workspaceId);
        activeConversationId = conversation.id;
        setConversationId(conversation.id);
      }
      const response = await api.sendChat(text, activeConversationId, undefined, workspaceId);
      setChatEntries((entries) => [
        ...entries,
        {
          role: "assistant",
          content:
            response.assistant_status === "succeeded"
              ? response.reply ?? ""
              : response.error_message ?? "AI-svaret kunde inte genereras.",
          sources: response.sources,
        },
      ]);
      await refresh();
    } catch (e: any) {
      setError(e.message);
      setChatEntries((entries) => [
        ...entries,
        { role: "assistant", content: "Svaret kunde inte hämtas. Meddelandet kan ändå vara sparat." },
      ]);
    } finally {
      setSending(false);
    }
  }

  if (loading) {
    return (
      <div role="status" className="text-sm text-white/40">
        Läser workspace…
      </div>
    );
  }

  if (error && !workspace) {
    return (
      <div className="space-y-4">
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
        <Link href="/workspaces" className="text-sm text-accent2 hover:underline">
          ← Tillbaka till workspaces
        </Link>
      </div>
    );
  }

  if (!workspace) return null;

  const attachedIds = new Set(workspace.sources.map((source) => source.id));
  const attachable = library.filter((source) => !attachedIds.has(source.id));
  const openTasks = workspace.tasks.filter((task) => task.status !== "done");

  return (
    <div className="space-y-6">
      <div>
        <Link href="/workspaces" className="text-sm text-white/50 hover:text-white">
          ← Tillbaka till workspaces
        </Link>
        <div className="mt-3 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold">{workspace.name}</h1>
            <p className="mt-1 max-w-3xl text-sm text-white/50">
              {workspace.purpose || workspace.description || "Inget syfte formulerat ännu."}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <span className="rounded-full border border-border px-3 py-1 text-xs text-white/60">
              {workspace.archived_at ? "arkiverad" : workspace.status}
            </span>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                run(() =>
                  workspace.archived_at
                    ? api.restoreWorkspace(workspace.id)
                    : api.archiveWorkspace(workspace.id)
                )
              }
              className="rounded-lg border border-border px-3 py-1.5 text-xs text-white/60 hover:text-white disabled:opacity-50"
            >
              {workspace.archived_at ? "Återställ" : "Arkivera"}
            </button>
          </div>
        </div>
        {(workspace.goals ?? []).length > 0 && (
          <ul className="mt-3 flex flex-wrap gap-2" aria-label="Workspace-mål">
            {(workspace.goals ?? []).map((goal) => (
              <li key={goal} className="rounded-full border border-accent/30 bg-accent/10 px-3 py-1 text-xs text-white/70">
                {goal}
              </li>
            ))}
          </ul>
        )}
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Källor" value={workspace.sources.length} />
        <Stat label="Öppna uppgifter" value={openTasks.length} total={workspace.tasks.length} />
        <Stat label="Samtal" value={workspace.conversations.length} />
        <Stat label="Beslut" value={workspace.decisions.length} />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <section aria-labelledby="workspace-knowledge" className="rounded-xl border border-border bg-panel p-4">
          <h2 id="workspace-knowledge" className="text-sm font-medium text-white/70">
            Källor och kunskap
          </h2>
          <div className="mt-3 flex flex-col gap-2 sm:flex-row">
            <label htmlFor="attach-document" className="sr-only">
              Välj källa att koppla
            </label>
            <select
              id="attach-document"
              value={selectedDocument}
              onChange={(event) => setSelectedDocument(event.target.value)}
              className="min-w-0 flex-1 rounded-lg border border-border bg-base px-3 py-2 text-sm"
            >
              <option value="">Välj källa att koppla…</option>
              {attachable.map((source) => (
                <option key={source.id} value={source.id}>
                  {source.title}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={associateDocument}
              disabled={busy || !selectedDocument}
              className="rounded-lg bg-accent px-3 py-2 text-sm disabled:opacity-50"
            >
              Koppla
            </button>
          </div>
          <ul className="mt-3 space-y-2">
            {workspace.sources.map((source) => (
              <li key={source.id} className="rounded-lg border border-border/60 px-3 py-2 text-sm">
                <div className="flex items-start justify-between gap-2">
                  <Link href={`/library/${source.id}`} className="min-w-0 truncate text-white/85 hover:text-accent hover:underline">
                    {source.title}
                  </Link>
                  <button
                    type="button"
                    onClick={() => run(() => api.disassociateWorkspaceDocument(workspace.id, source.id))}
                    className="shrink-0 text-xs text-white/40 hover:text-red-300"
                    aria-label={`Koppla loss ${source.title}`}
                  >
                    Koppla loss
                  </button>
                </div>
                <div className="mt-1 text-xs text-white/40">
                  {SOURCE_STATUS_LABELS[source.status] ?? source.status} · {source.chunk_count} chunkar · v
                  {source.version_number}
                </div>
              </li>
            ))}
            {workspace.sources.length === 0 && (
              <li className="text-sm text-white/30">Inga källor är kopplade ännu.</li>
            )}
          </ul>
        </section>

        <section aria-labelledby="workspace-tasks" className="rounded-xl border border-border bg-panel p-4">
          <h2 id="workspace-tasks" className="text-sm font-medium text-white/70">
            Uppgifter
          </h2>
          <div className="mt-3 flex gap-2">
            <label htmlFor="workspace-task" className="sr-only">
              Ny uppgift
            </label>
            <input
              id="workspace-task"
              value={newTask}
              onChange={(event) => setNewTask(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && addTask()}
              placeholder="Ny uppgift i workspace…"
              className="min-w-0 flex-1 rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
            <button
              type="button"
              onClick={addTask}
              disabled={busy || !newTask.trim()}
              className="rounded-lg bg-accent px-3 py-2 text-sm disabled:opacity-50"
            >
              Lägg till
            </button>
          </div>
          <ul className="mt-3 space-y-2">
            {workspace.tasks.map((task) => (
              <li key={task.id} className="rounded-lg border border-border/60 px-3 py-2 text-sm">
                <div className="flex items-center justify-between gap-2">
                  <span className={task.status === "done" ? "text-white/30 line-through" : "text-white/85"}>
                    {task.title}
                  </span>
                  <span className="shrink-0 text-xs text-white/40">{task.status}</span>
                </div>
              </li>
            ))}
            {workspace.tasks.length === 0 && <li className="text-sm text-white/30">Inga uppgifter ännu.</li>}
          </ul>
        </section>
      </div>

      <section aria-labelledby="workspace-chat" className="rounded-xl border border-border bg-panel p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 id="workspace-chat" className="text-sm font-medium text-white/70">
              MainAI i workspace
            </h2>
            <p className="mt-1 text-xs text-white/35">
              Samtalet är kopplat till det här workspaceet och söker bara i dess källor.
            </p>
          </div>
          <button
            type="button"
            onClick={startConversation}
            disabled={busy}
            className="rounded-lg border border-border px-3 py-1.5 text-xs text-white/60 hover:text-white disabled:opacity-50"
          >
            Nytt samtal
          </button>
        </div>

        {conversations.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {conversations.map((conversation) => (
              <button
                key={conversation.id}
                type="button"
                onClick={() => loadConversation(conversation.id)}
                className={`rounded-full border px-3 py-1 text-xs ${
                  conversation.id === conversationId
                    ? "border-accent/50 bg-accent/10 text-white"
                    : "border-border text-white/50 hover:text-white"
                }`}
              >
                {conversation.title}
              </button>
            ))}
          </div>
        )}

        <div className="mt-4 max-h-80 space-y-3 overflow-y-auto rounded-lg border border-border/60 bg-base/50 p-3" aria-live="polite">
          {chatEntries.length === 0 && (
            <p className="text-sm text-white/30">Ställ en fråga om projektet för att börja.</p>
          )}
          {chatEntries.map((entry, index) => (
            <div key={`${entry.role}-${index}`} className={entry.role === "user" ? "text-right" : "text-left"}>
              <div
                className={`inline-block max-w-[90%] whitespace-pre-wrap rounded-lg px-3 py-2 text-sm ${
                  entry.role === "user" ? "bg-accent/20 text-white/90" : "bg-panel text-white/80"
                }`}
              >
                {entry.content}
              </div>
              {entry.sources && entry.sources.length > 0 && (
                <ul className="mt-1 space-y-1 text-left text-xs text-white/40">
                  {entry.sources.map((source) => (
                    <li key={source.document_id}>
                      Källa:{" "}
                      <Link href={`/library/${source.document_id}`} className="text-accent2 hover:underline">
                        {source.title}
                      </Link>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
          {sending && <div className="text-xs text-white/35">MainAI svarar…</div>}
        </div>

        <div className="mt-3 flex gap-2">
          <label htmlFor="workspace-message" className="sr-only">
            Meddelande till MainAI
          </label>
          <textarea
            id="workspace-message"
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                sendMessage();
              }
            }}
            rows={2}
            placeholder="Fråga MainAI om det här projektet…"
            className="min-w-0 flex-1 resize-none rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
          />
          <button
            type="button"
            onClick={sendMessage}
            disabled={sending || !message.trim()}
            className="self-end rounded-lg bg-accent px-4 py-2 text-sm disabled:opacity-50"
          >
            {sending ? "Skickar…" : "Skicka"}
          </button>
        </div>
      </section>

      <div className="grid gap-6 lg:grid-cols-2">
        <section aria-labelledby="workspace-decisions" className="rounded-xl border border-border bg-panel p-4">
          <h2 id="workspace-decisions" className="text-sm font-medium text-white/70">
            Beslut
          </h2>
          <div className="mt-3 space-y-2">
            <label htmlFor="decision-title" className="sr-only">
              Beslutstitel
            </label>
            <input
              id="decision-title"
              value={decisionTitle}
              onChange={(event) => setDecisionTitle(event.target.value)}
              placeholder="Beslutstitel…"
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
            <label htmlFor="decision-text" className="sr-only">
              Beslut
            </label>
            <textarea
              id="decision-text"
              value={decisionText}
              onChange={(event) => setDecisionText(event.target.value)}
              rows={2}
              placeholder="Vad beslutades, och varför?"
              className="w-full resize-none rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
            <button
              type="button"
              onClick={addDecision}
              disabled={busy || !decisionTitle.trim() || !decisionText.trim()}
              className="rounded-lg bg-accent px-3 py-2 text-sm disabled:opacity-50"
            >
              Registrera beslut
            </button>
          </div>
          <ul className="mt-4 space-y-2">
            {workspace.decisions.map((decision) => (
              <li key={decision.id} className="rounded-lg border border-border/60 px-3 py-2 text-sm">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-white/85">{decision.title}</span>
                  <span className="text-xs text-white/40">{decision.status}</span>
                </div>
                <p className="mt-1 whitespace-pre-wrap text-xs text-white/50">{decision.decision}</p>
              </li>
            ))}
            {workspace.decisions.length === 0 && <li className="text-sm text-white/30">Inga beslut registrerade.</li>}
          </ul>
        </section>

        <section aria-labelledby="workspace-activity" className="rounded-xl border border-border bg-panel p-4">
          <h2 id="workspace-activity" className="text-sm font-medium text-white/70">
            Aktivitet och kontext
          </h2>
          <ul className="mt-3 space-y-2">
            {workspace.activities.slice(0, 12).map((activity) => (
              <li key={activity.id} className="border-b border-border/60 pb-2 text-sm last:border-0">
                <div className="text-white/75">{activity.summary}</div>
                <div className="mt-0.5 text-xs text-white/35">
                  {activity.kind} · {new Date(activity.created_at).toLocaleString("sv-SE")}
                </div>
              </li>
            ))}
            {workspace.activities.length === 0 && <li className="text-sm text-white/30">Ingen aktivitet ännu.</li>}
          </ul>

          {workspace.context.uncertainties.length > 0 && (
            <div className="mt-4 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3">
              <h3 className="text-xs font-medium uppercase tracking-wide text-amber-200/80">
                Osäker eller motsägelsefull kunskap
              </h3>
              <ul className="mt-2 space-y-1 text-xs text-white/55">
                {workspace.context.uncertainties.slice(0, 6).map((item, index) => (
                  <li key={index}>{String(item.title ?? item.kind ?? "Okänd osäkerhet")}</li>
                ))}
              </ul>
            </div>
          )}
          {workspace.context.retrieval_degraded_reason && (
            <p className="mt-3 text-xs text-amber-200">
              Semantisk kontextsökning är degraderad: {workspace.context.retrieval_degraded_reason}
            </p>
          )}
        </section>
      </div>
    </div>
  );
}

function Stat({ label, value, total }: { label: string; value: number; total?: number }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-xs uppercase tracking-wide text-white/40">{label}</div>
      <div className="mt-2 text-2xl font-semibold">
        {value}
        {total !== undefined && <span className="text-base text-white/30"> / {total}</span>}
      </div>
    </div>
  );
}
