"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import {
  api,
  ChatSource,
  ConversationItem,
  KnowledgeSourceItem,
  newClientMessageId,
  WorkspaceDecision,
  WorkspaceDetail,
  WorkspaceSupervised,
  WorkspaceSyncRun,
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

const SYNC_STATUS_LABELS: Record<string, string> = {
  queued: "Köad",
  running: "Pågår",
  completed: "Klar",
  failed: "Misslyckades",
  cancelled: "Avbruten",
  stale: "Inaktuell",
};

const TERMINAL_TASK_STATUSES = new Set(["completed", "failed", "cancelled"]);

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
  const [newMilestone, setNewMilestone] = useState("");
  const [newMilestoneDue, setNewMilestoneDue] = useState("");
  const [decisionTitle, setDecisionTitle] = useState("");
  const [decisionText, setDecisionText] = useState("");
  const [decisionEpistemic, setDecisionEpistemic] = useState("confirmed");
  const [supersedingId, setSupersedingId] = useState<string | null>(null);

  const [conversations, setConversations] = useState<ConversationItem[]>([]);
  const [conversationId, setConversationId] = useState<string | undefined>();
  const [chatEntries, setChatEntries] = useState<ChatEntry[]>([]);
  const [message, setMessage] = useState("");
  const [sending, setSending] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const abortRef = useRef<AbortController | null>(null);

  const [supervised, setSupervised] = useState<WorkspaceSupervised>({ goals: [], tasks: [] });
  const [supervisedError, setSupervisedError] = useState<string | null>(null);
  const [syncRuns, setSyncRuns] = useState<WorkspaceSyncRun[]>([]);
  const [syncing, setSyncing] = useState(false);

  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editName, setEditName] = useState("");
  const [editPurpose, setEditPurpose] = useState("");
  const [editGoals, setEditGoals] = useState("");
  const [editStatus, setEditStatus] = useState("active");
  const [editSettings, setEditSettings] = useState("{}");

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

  async function refresh() {
    try {
      setError(null);
      const detail = await api.getWorkspace(workspaceId);
      setWorkspace(detail);
      setConversations(detail.conversations);
      if (!editing) {
        setEditName(detail.name);
        setEditPurpose(detail.purpose ?? "");
        setEditGoals((detail.goals ?? []).join("\n"));
        setEditStatus(detail.status);
        setEditSettings(JSON.stringify(detail.settings ?? {}, null, 2));
      }
      // A workspace switch must never keep the previous workspace's conversation open.
      const active = detail.conversations.find((item) => item.id === conversationId);
      if (!active) {
        if (detail.conversations[0]) {
          await loadConversation(detail.conversations[0].id);
        } else {
          setConversationId(undefined);
          setChatEntries([]);
        }
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  async function refreshSupervised() {
    try {
      setSupervised(await api.workspaceSupervised(workspaceId));
      setSupervisedError(null);
    } catch (e: any) {
      setSupervisedError(e.message);
    }
  }

  async function refreshSyncRuns() {
    try {
      setSyncRuns(await api.workspaceSyncRuns(workspaceId));
    } catch {
      // The main workspace error boundary already reports an unavailable workspace.
      setSyncRuns([]);
    }
  }

  useEffect(() => {
    // Documented fetch-on-mount pattern used throughout the shell.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setWorkspace(null);
    setConversations([]);
    setConversationId(undefined);
    setChatEntries([]);
    setStreamingText("");
    setSupervised({ goals: [], tasks: [] });
    setSyncRuns([]);
    setLoading(true);
    refresh();
    refreshSupervised();
    refreshSyncRuns();
    api.listLibrary().then(setLibrary).catch(() => setLibrary([]));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workspaceId]);

  async function run(action: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await action();
      await refresh();
      await refreshSupervised();
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
        epistemic_status: decisionEpistemic as "confirmed" | "uncertain" | "proposed",
      });
      setDecisionTitle("");
      setDecisionText("");
      setDecisionEpistemic("confirmed");
    });
  }

  function startSupersede(decision: WorkspaceDecision) {
    setSupersedingId(decision.id);
    setDecisionTitle(`Ersätter: ${decision.title}`);
    setDecisionText(decision.decision);
    setDecisionEpistemic("confirmed");
  }

  function cancelSupersede() {
    setSupersedingId(null);
    setDecisionTitle("");
    setDecisionText("");
    setDecisionEpistemic("confirmed");
  }

  async function supersedeDecision() {
    if (!supersedingId || !decisionTitle.trim() || !decisionText.trim()) return;
    await run(async () => {
      await api.supersedeWorkspaceDecision(workspaceId, supersedingId, {
        title: decisionTitle.trim(),
        decision: decisionText.trim(),
        status: "accepted",
        epistemic_status: decisionEpistemic as "confirmed" | "uncertain" | "proposed",
      });
      cancelSupersede();
    });
  }

  async function addMilestone() {
    if (!newMilestone.trim()) return;
    await run(async () => {
      await api.createMilestone(workspaceId, {
        title: newMilestone.trim(),
        due_at: newMilestoneDue ? new Date(newMilestoneDue).toISOString() : null,
        status: "open",
      });
      setNewMilestone("");
      setNewMilestoneDue("");
    });
  }

  async function associateDocument() {
    if (!selectedDocument) return;
    await run(async () => {
      await api.associateWorkspaceDocument(workspaceId, selectedDocument);
      setSelectedDocument("");
    });
  }

  async function saveWorkspace() {
    if (!workspace || saving) return;
    const name = editName.trim();
    if (!name) {
      setError("Workspace-namnet får inte vara tomt.");
      return;
    }
    let settings: Record<string, unknown>;
    try {
      const parsed = editSettings.trim() ? JSON.parse(editSettings) : {};
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("not an object");
      }
      settings = parsed as Record<string, unknown>;
    } catch {
      setError("Inställningarna måste vara ett giltigt JSON-objekt.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await api.updateWorkspace(workspace.id, {
        name,
        purpose: editPurpose.trim() || null,
        goals: editGoals
          .split(/[\n,]+/)
          .map((goal) => goal.trim())
          .filter(Boolean),
        status: editStatus,
        settings,
        expected_version: workspace.version,
      });
      setEditing(false);
      await refresh();
    } catch (e: any) {
      setError(e.message);
      if (e?.status === 409) {
        setEditing(false);
        await refresh();
      }
    } finally {
      setSaving(false);
    }
  }

  async function startSync() {
    if (!workspace || syncing) return;
    setSyncing(true);
    setError(null);
    try {
      await api.startWorkspaceSync(workspace.id, newClientMessageId());
      await refreshSyncRuns();
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSyncing(false);
    }
  }

  async function retrySync(runId: string) {
    if (!workspace || syncing) return;
    setSyncing(true);
    setError(null);
    try {
      await api.retryWorkspaceSync(workspace.id, runId);
      await refreshSyncRuns();
    } catch (e: any) {
      setError(e.message);
      await refreshSyncRuns();
    } finally {
      setSyncing(false);
    }
  }

  async function supervisedAction(action: (id: string) => Promise<unknown>, taskId: string) {
    setBusy(true);
    setError(null);
    try {
      await action(taskId);
      await refreshSupervised();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function startConversation() {
    if (sending) return;
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
    setStreamingText("");
    setChatEntries((entries) => [...entries, { role: "user", content: text }]);
    try {
      let activeConversationId = conversationId;
      if (!activeConversationId) {
        const conversation = await api.createWorkspaceConversation(workspaceId);
        activeConversationId = conversation.id;
        setConversationId(conversation.id);
      }
      const controller = new AbortController();
      abortRef.current = controller;
      const response = await api.streamChat(
        text,
        activeConversationId,
        newClientMessageId(),
        {
          signal: controller.signal,
          onEvent: (event) => {
            if (event.type === "start") {
              if (event.conversation_id !== activeConversationId) {
                activeConversationId = event.conversation_id;
                setConversationId(event.conversation_id);
              }
            } else if (event.type === "delta") {
              setStreamingText((previous) => previous + event.text);
            }
          },
        },
        workspaceId
      );
      setStreamingText("");
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
      setStreamingText("");
      if (e?.name === "AbortError") {
        setChatEntries((entries) => [
          ...entries,
          { role: "assistant", content: "Genereringen avbröts. Ditt meddelande finns sparat." },
        ]);
        await refresh();
      } else {
        setError(e.message);
        setChatEntries((entries) => [
          ...entries,
          { role: "assistant", content: "Svaret kunde inte hämtas. Meddelandet kan ändå vara sparat." },
        ]);
      }
    } finally {
      abortRef.current = null;
      setSending(false);
    }
  }

  function stopStreaming() {
    abortRef.current?.abort();
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
  const latestSync = syncRuns[0];

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
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => setEditing((value) => !value)}
              className="rounded-lg border border-border px-3 py-1.5 text-xs text-white/60 hover:text-white"
            >
              {editing ? "Stäng redigering" : "Redigera"}
            </button>
            <button
              type="button"
              disabled={syncing}
              onClick={startSync}
              className="rounded-lg border border-border px-3 py-1.5 text-xs text-white/60 hover:text-white disabled:opacity-50"
            >
              {syncing ? "Synkar…" : "Synka workspace"}
            </button>
            <span className="rounded-full border border-border px-3 py-1 text-xs text-white/60">
              {workspace.archived_at ? "arkiverad" : workspace.status} · v{workspace.version ?? 1}
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
        {latestSync && (
          <p role="status" className="mt-3 text-xs text-white/40">
            Senaste synk: {SYNC_STATUS_LABELS[latestSync.status] ?? latestSync.status} ·{" "}
            {latestSync.progress_current}/{latestSync.progress_total} entiteter · försök{" "}
            {latestSync.attempt}
          </p>
        )}
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      {editing && (
        <section aria-labelledby="workspace-settings" className="rounded-xl border border-border bg-panel p-4">
          <h2 id="workspace-settings" className="text-sm font-medium text-white/70">
            Workspace-inställningar
          </h2>
          <div className="mt-3 grid gap-3 md:grid-cols-2">
            <div>
              <label htmlFor="workspace-edit-name" className="mb-1 block text-xs text-white/40">
                Workspace-namn
              </label>
              <input
                id="workspace-edit-name"
                value={editName}
                onChange={(event) => setEditName(event.target.value)}
                className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
              />
            </div>
            <div>
              <label htmlFor="workspace-edit-status" className="mb-1 block text-xs text-white/40">
                Status
              </label>
              <select
                id="workspace-edit-status"
                value={editStatus}
                onChange={(event) => setEditStatus(event.target.value)}
                className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
              >
                <option value="idea">Idé</option>
                <option value="active">Aktiv</option>
                <option value="paused">Pausad</option>
                <option value="done">Klar</option>
              </select>
            </div>
            <div className="md:col-span-2">
              <label htmlFor="workspace-edit-purpose" className="mb-1 block text-xs text-white/40">
                Syfte
              </label>
              <textarea
                id="workspace-edit-purpose"
                value={editPurpose}
                onChange={(event) => setEditPurpose(event.target.value)}
                rows={2}
                className="w-full resize-none rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
              />
            </div>
            <div>
              <label htmlFor="workspace-edit-goals" className="mb-1 block text-xs text-white/40">
                Mål (ett per rad)
              </label>
              <textarea
                id="workspace-edit-goals"
                value={editGoals}
                onChange={(event) => setEditGoals(event.target.value)}
                rows={4}
                className="w-full resize-none rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
              />
            </div>
            <div>
              <label htmlFor="workspace-edit-settings" className="mb-1 block text-xs text-white/40">
                Inställningar (JSON)
              </label>
              <textarea
                id="workspace-edit-settings"
                value={editSettings}
                onChange={(event) => setEditSettings(event.target.value)}
                rows={4}
                spellCheck={false}
                className="w-full resize-none rounded-lg border border-border bg-base px-3 py-2 font-mono text-xs outline-none focus:border-accent"
              />
            </div>
          </div>
          <div className="mt-3 flex gap-2">
            <button
              type="button"
              onClick={saveWorkspace}
              disabled={saving}
              className="rounded-lg bg-accent px-3 py-2 text-sm disabled:opacity-50"
            >
              {saving ? "Sparar…" : "Spara workspace"}
            </button>
            <button
              type="button"
              onClick={() => setEditing(false)}
              className="rounded-lg border border-border px-3 py-2 text-sm text-white/60 hover:text-white"
            >
              Avbryt
            </button>
          </div>
        </section>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-5">
        <Stat label="Källor" value={workspace.sources.length} />
        <Stat label="Öppna uppgifter" value={openTasks.length} total={workspace.tasks.length} />
        <Stat label="Milstolpar" value={workspace.milestones.length} />
        <Stat label="Samtal" value={workspace.conversations.length} />
        <Stat label="Beslut" value={workspace.decisions.length} />
      </div>

      <section aria-labelledby="workspace-milestones" className="rounded-xl border border-border bg-panel p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h2 id="workspace-milestones" className="text-sm font-medium text-white/70">
              Milstolpar
            </h2>
            <p className="mt-1 text-xs text-white/35">
              Konkreta delmål i workspaceet. Uppgifter kan kopplas till dem från uppgiftshanteraren.
            </p>
          </div>
          <Link href="/tasks" className="text-xs text-accent2 hover:underline">
            Uppgiftshanteraren →
          </Link>
        </div>
        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
          <div className="min-w-0 flex-1">
            <label htmlFor="workspace-milestone-title" className="sr-only">
              Ny milstolpe
            </label>
            <input
              id="workspace-milestone-title"
              value={newMilestone}
              onChange={(event) => setNewMilestone(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && addMilestone()}
              placeholder="Ny milstolpe…"
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
          <div>
            <label htmlFor="workspace-milestone-due" className="sr-only">
              Deadline
            </label>
            <input
              id="workspace-milestone-due"
              type="date"
              value={newMilestoneDue}
              onChange={(event) => setNewMilestoneDue(event.target.value)}
              className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
            />
          </div>
          <button
            type="button"
            onClick={addMilestone}
            disabled={busy || !newMilestone.trim()}
            className="rounded-lg bg-accent px-3 py-2 text-sm disabled:opacity-50"
          >
            Lägg till
          </button>
        </div>
        <ul className="mt-3 grid gap-2 md:grid-cols-2">
          {workspace.milestones.map((milestone) => (
            <li key={milestone.id} className="rounded-lg border border-border/60 px-3 py-2 text-sm">
              <div className="flex items-center justify-between gap-2">
                <span className="min-w-0 truncate text-white/85">{milestone.title}</span>
                <span className="shrink-0 rounded-full border border-border px-2 py-0.5 text-[10px] text-white/50">
                  {milestone.status}
                </span>
              </div>
              <div className="mt-1 text-xs text-white/40">
                {milestone.due_at
                  ? `Deadline ${new Date(milestone.due_at).toLocaleDateString("sv-SE")}`
                  : "Ingen deadline"}{" "}
                · {milestone.completed_task_count}/{milestone.task_count} uppgifter klara
              </div>
            </li>
          ))}
          {workspace.milestones.length === 0 && (
            <li className="text-sm text-white/30">Inga milstolpar ännu.</li>
          )}
        </ul>
      </section>

      <section aria-labelledby="workspace-supervised" className="rounded-xl border border-border bg-panel p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 id="workspace-supervised" className="text-sm font-medium text-white/70">
              Superviserade MainAI-uppgifter
            </h2>
            <p className="mt-1 text-xs text-white/35">
              Riktiga goals och tasks från MainAI:s exekveringsloop. Här visas state och evidens;
              körningen sker i den befintliga motorn.
            </p>
          </div>
          <span className="rounded-full border border-border px-2 py-0.5 text-xs text-white/40">
            {supervised.tasks.length} tasks · {supervised.goals.length} goals
          </span>
        </div>

        {supervisedError && (
          <div role="alert" className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
            {supervisedError}
          </div>
        )}

        {supervised.tasks.length === 0 ? (
          <p className="mt-3 text-sm text-white/30">
            Inga superviserade MainAI-uppgifter är kopplade till det här workspaceet ännu.
          </p>
        ) : (
          <div className="mt-3 space-y-3">
            {supervised.tasks.map((item) => {
              const terminal = TERMINAL_TASK_STATUSES.has(item.task.status);
              return (
                <article key={item.task.id} className="rounded-lg border border-border/60 p-3 text-sm">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="text-white/85">{item.task.description}</div>
                      <div className="mt-1 text-xs text-white/40">
                        {item.goal.title} · {item.task.task_type} · risk {item.task.risk_level}
                      </div>
                    </div>
                    <span className="shrink-0 rounded-full border border-border px-2 py-0.5 text-xs text-white/50">
                      {item.task.status}
                    </span>
                  </div>
                  <div className="mt-2 flex flex-wrap gap-3 text-xs text-white/40">
                    <span>liveness: {item.task.liveness}</span>
                    <span>
                      försök {item.task.attempts}/{item.task.max_attempts}
                    </span>
                    {item.job && <span>jobb: {item.job.status}</span>}
                    {item.task.approval_required && (
                      <span>{item.approval_granted ? "godkänd" : "väntar på godkännande"}</span>
                    )}
                  </div>
                  {item.task.blocker_reason && (
                    <p className="mt-2 rounded-lg border border-amber-500/20 bg-amber-500/5 p-2 text-xs text-amber-200">
                      Blockerare: {item.task.blocker_reason}
                    </p>
                  )}
                  {item.checkpoints[0] && (
                    <p className="mt-2 text-xs text-white/40">
                      Senaste checkpoint: test {item.checkpoints[0].test_status ?? "okänt"} · CI{" "}
                      {item.checkpoints[0].ci_status ?? "okänt"}
                    </p>
                  )}
                  <div className="mt-2 flex flex-wrap gap-3 text-xs">
                    <details className="min-w-0">
                      <summary className="cursor-pointer text-white/50 hover:text-white">
                        Historik ({item.events.length})
                      </summary>
                      <ul className="mt-1 space-y-1 text-white/40">
                        {item.events.slice(0, 12).map((event) => (
                          <li key={event.id}>
                            {event.event_type} · {new Date(event.created_at).toLocaleString("sv-SE")}
                          </li>
                        ))}
                      </ul>
                    </details>
                    <details className="min-w-0">
                      <summary className="cursor-pointer text-white/50 hover:text-white">
                        Verifiering ({item.verification_evidence.length})
                      </summary>
                      <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-base/70 p-2 text-[11px] text-white/50">
                        {JSON.stringify(item.verification_evidence, null, 2)}
                      </pre>
                    </details>
                    {item.waits.length > 0 && (
                      <details>
                        <summary className="cursor-pointer text-white/50 hover:text-white">
                          Externa väntor ({item.waits.length})
                        </summary>
                        <ul className="mt-1 space-y-1 text-white/40">
                          {item.waits.map((wait) => (
                            <li key={wait.id}>
                              {wait.source_type}: {wait.status} ({wait.poll_count} polls)
                            </li>
                          ))}
                        </ul>
                      </details>
                    )}
                  </div>
                  {!terminal && (
                    <div className="mt-3 flex flex-wrap gap-2">
                      {item.task.approval_required && !item.approval_granted && (
                        <button
                          type="button"
                          disabled={busy}
                          onClick={() => supervisedAction(api.mainaiExecutionApproveTask, item.task.id)}
                          className="rounded-lg bg-accent px-3 py-1.5 text-xs disabled:opacity-50"
                        >
                          Godkänn
                        </button>
                      )}
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => supervisedAction(api.mainaiExecutionCancelTask, item.task.id)}
                        className="rounded-lg border border-border px-3 py-1.5 text-xs text-white/60 hover:text-white disabled:opacity-50"
                      >
                        Avbryt
                      </button>
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </section>

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
            disabled={busy || sending}
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
          {chatEntries.length === 0 && !streamingText && (
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
          {streamingText && (
            <div className="text-left">
              <div className="inline-block max-w-[90%] whitespace-pre-wrap rounded-lg bg-panel px-3 py-2 text-sm text-white/80">
                {streamingText}
              </div>
            </div>
          )}
          {sending && !streamingText && <div className="text-xs text-white/35">MainAI svarar…</div>}
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
          {sending ? (
            <button
              type="button"
              onClick={stopStreaming}
              className="self-end rounded-lg border border-border px-4 py-2 text-sm text-white/70 hover:text-white"
            >
              Stoppa
            </button>
          ) : (
            <button
              type="button"
              onClick={sendMessage}
              disabled={!message.trim()}
              className="self-end rounded-lg bg-accent px-4 py-2 text-sm disabled:opacity-50"
            >
              Skicka
            </button>
          )}
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
            <div>
              <label htmlFor="decision-epistemic" className="mb-1 block text-xs text-white/40">
                Kunskapsstatus
              </label>
              <select
                id="decision-epistemic"
                value={decisionEpistemic}
                onChange={(event) => setDecisionEpistemic(event.target.value)}
                className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
              >
                <option value="confirmed">Bekräftat</option>
                <option value="uncertain">Osäkert</option>
                <option value="proposed">AI-förslag</option>
              </select>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={supersedingId ? supersedeDecision : addDecision}
                disabled={busy || !decisionTitle.trim() || !decisionText.trim()}
                className="rounded-lg bg-accent px-3 py-2 text-sm disabled:opacity-50"
              >
                {supersedingId ? "Ersätt beslut" : "Registrera beslut"}
              </button>
              {supersedingId && (
                <button
                  type="button"
                  onClick={cancelSupersede}
                  className="rounded-lg border border-border px-3 py-2 text-sm text-white/60 hover:text-white"
                >
                  Avbryt ersättning
                </button>
              )}
            </div>
          </div>
          <ul className="mt-4 space-y-2">
            {workspace.decisions.map((decision) => (
              <li key={decision.id} className="rounded-lg border border-border/60 px-3 py-2 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="min-w-0 text-white/85">{decision.title}</span>
                  <div className="flex shrink-0 flex-wrap items-center gap-1.5 text-[10px]">
                    <span className="rounded-full border border-border px-2 py-0.5 text-white/50">
                      v{decision.version}
                    </span>
                    <span
                      className={`rounded-full border px-2 py-0.5 ${
                        decision.epistemic_status === "confirmed"
                          ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-200"
                          : decision.epistemic_status === "uncertain"
                            ? "border-amber-500/30 bg-amber-500/10 text-amber-200"
                            : "border-accent/30 bg-accent/10 text-accent2"
                      }`}
                    >
                      {decision.epistemic_status === "confirmed"
                        ? "Bekräftat"
                        : decision.epistemic_status === "uncertain"
                          ? "Osäkert"
                          : "AI-förslag"}
                    </span>
                    {decision.superseded_at && (
                      <span className="rounded-full border border-white/15 px-2 py-0.5 text-white/40">
                        Ersatt
                      </span>
                    )}
                  </div>
                </div>
                <p className="mt-1 whitespace-pre-wrap text-xs text-white/50">{decision.decision}</p>
                {!decision.superseded_at && decision.status !== "superseded" && (
                  <button
                    type="button"
                    onClick={() => startSupersede(decision)}
                    className="mt-1 text-xs text-accent2 hover:underline"
                  >
                    Ersätt med nytt beslut
                  </button>
                )}
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

          <div className="mt-4 rounded-lg border border-border/60 p-3">
            <h3 className="text-xs font-medium uppercase tracking-wide text-white/50">
              Synkronisering
            </h3>
            {syncRuns.length === 0 ? (
              <p className="mt-2 text-xs text-white/35">Ingen synk har körts ännu.</p>
            ) : (
              <ul className="mt-2 space-y-2">
                {syncRuns.slice(0, 5).map((run) => (
                  <li key={run.id} className="flex flex-wrap items-center justify-between gap-2 text-xs">
                    <span className="text-white/55">
                      {SYNC_STATUS_LABELS[run.status] ?? run.status} · {run.progress_current}/
                      {run.progress_total} · försök {run.attempt}
                    </span>
                    {(run.status === "failed" || run.status === "stale") && (
                      <button
                        type="button"
                        onClick={() => retrySync(run.id)}
                        disabled={syncing}
                        className="text-accent2 hover:underline disabled:opacity-50"
                      >
                        Försök igen
                      </button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>

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
