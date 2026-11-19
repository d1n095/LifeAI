"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import {
  api,
  CANCELLABLE_MAINAI_JOB_STATUSES,
  ChatSource,
  ConversationItem,
  DocumentItem,
  KnowledgeSourceItem,
  MainAIJob,
  MainAITask,
  SystemHealth,
} from "@/lib/api";
import LivingCore, { CoreMode, CoreState } from "@/components/living-core/LivingCore";
import ConfidenceBadge from "@/components/ConfidenceBadge";

// The Founder Console shows only what the backend measured. There is no placeholder health
// value, no invented security indicator, and no "all systems operational" copy: a failed fetch
// is displayed as unknown/degraded, never smoothed over.

type QuickAnswer = {
  text: string;
  provider: string;
  sources: ChatSource[];
  confidence?: "high" | "medium" | "low" | "none";
  confidenceScore?: number;
};

type HealthTone = "ok" | "warn" | "bad" | "unknown";

function toneClasses(tone: HealthTone) {
  switch (tone) {
    case "ok":
      return "bg-emerald-400/80";
    case "warn":
      return "bg-amber-400/80";
    case "bad":
      return "bg-red-400/80";
    default:
      return "bg-white/25";
  }
}

function statusLine(health: SystemHealth | null, error: string | null) {
  if (error) return { tone: "bad" as HealthTone, label: "Backend onåbar" };
  if (!health) return { tone: "unknown" as HealthTone, label: "Kontrollerar…" };
  if (health.status !== "ok") return { tone: "bad" as HealthTone, label: "API:t svarar inte" };
  const model = health.local_model;
  if (model && model.reachable === false) return { tone: "bad" as HealthTone, label: "Lokal modell onåbar" };
  if (model?.missing_models?.length) return { tone: "warn" as HealthTone, label: "Modell saknas" };
  if (!health.storage?.writable) return { tone: "warn" as HealthTone, label: "Lagring ej skrivbar" };
  if (health.worker !== "alive") return { tone: "warn" as HealthTone, label: "Worker ej bekräftad" };
  return { tone: "ok" as HealthTone, label: "Driftläge verifierat" };
}

function modelLine(health: SystemHealth | null): { tone: HealthTone; value: string; detail: string } {
  const model = health?.local_model;
  if (!model) return { tone: "unknown", value: "Ej konfigurerad", detail: "Ingen lokal provider är aktiv." };
  const names = model.configured_models.join(", ") || "Ingen modell vald";
  if (model.reachable === false) {
    return { tone: "bad", value: names, detail: `${model.runtime} svarar inte just nu.` };
  }
  if (model.reachable === null) {
    return { tone: "unknown", value: names, detail: "Ingen modell att probea." };
  }
  if (model.missing_models?.length) {
    return { tone: "warn", value: names, detail: `Saknas lokalt: ${model.missing_models.join(", ")}.` };
  }
  return {
    tone: "ok",
    value: names,
    detail: model.missing_models ? `${model.runtime} har alla konfigurerade modeller.` : `${model.runtime} svarade; modellistan kontrollerades inte.`,
  };
}

export default function FounderConsolePage() {
  const [health, setHealth] = useState<SystemHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [decisions, setDecisions] = useState<KnowledgeSourceItem[]>([]);
  const [conversations, setConversations] = useState<ConversationItem[]>([]);
  const [jobs, setJobs] = useState<MainAIJob[]>([]);
  const [approvals, setApprovals] = useState<MainAITask[]>([]);
  const [loading, setLoading] = useState(true);
  const [partialErrors, setPartialErrors] = useState<string[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  // The console polls every 10s and pauses while the tab is hidden, so "checked now" is a claim
  // the UI cannot make on its own. It shows the last attempt instead, and says so after a failure.
  const [lastCheckedAt, setLastCheckedAt] = useState<Date | null>(null);
  // A failed goals read is not an empty approval queue. Keeping the two apart stops the panel
  // from telling the founder there is nothing to approve when it simply could not look.
  const [approvalsUnreadable, setApprovalsUnreadable] = useState(false);

  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [quickAnswer, setQuickAnswer] = useState<QuickAnswer | null>(null);
  const [quickError, setQuickError] = useState<string | null>(null);
  const [conversationId, setConversationId] = useState<string | undefined>();
  const inputRef = useRef<HTMLInputElement>(null);

  const refreshHealth = useCallback(async () => {
    try {
      const next = await api.health();
      setHealth(next);
      setHealthError(null);
    } catch (e: unknown) {
      setHealth(null);
      setHealthError(e instanceof Error ? e.message : "Okänt fel");
    } finally {
      setLastCheckedAt(new Date());
    }
  }, []);

  const refreshActivity = useCallback(async () => {
    const [jobsResult, goalsResult] = await Promise.allSettled([
      api.mainaiJobs(20),
      api.mainaiExecutionGoals(10),
    ]);

    if (jobsResult.status === "fulfilled") setJobs(jobsResult.value);
    else setPartialErrors((prev) => [...new Set([...prev, "Arbetsjobb kunde inte läsas."])]);

    if (goalsResult.status !== "fulfilled") {
      setApprovalsUnreadable(true);
      setPartialErrors((prev) => [...new Set([...prev, "Godkännandekön kunde inte läsas."])]);
      return;
    }
    setApprovalsUnreadable(false);
    const activeGoals = goalsResult.value
      .filter((goal) => !["completed", "cancelled", "failed"].includes(goal.status))
      .slice(0, 4);
    const details = await Promise.all(
      activeGoals.map((goal) => api.mainaiExecutionGoalDetail(goal.id).catch(() => null))
    );
    const pending = details
      .flatMap((detail) => detail?.tasks ?? [])
      .filter((task) => task.approval_required && !["completed", "cancelled"].includes(task.status));
    setApprovals(pending);
  }, []);

  const loadAll = useCallback(async () => {
    // Every state write below happens after an await. The initial render already shows the
    // loading state, and an effect must not synchronously setState (cascading render).
    const [, docs, library, chats] = await Promise.allSettled([
      refreshHealth(),
      api.listDocuments(),
      api.listLibrary({ classification: "decisions" }),
      api.listConversations(),
      refreshActivity(),
    ]);
    if (docs.status === "fulfilled") setDocuments(docs.value);
    else setPartialErrors((prev) => [...prev, "Dokumentlistan kunde inte läsas."]);
    if (library.status === "fulfilled") setDecisions(library.value);
    else setPartialErrors((prev) => [...prev, "Beslutsarkivet kunde inte läsas."]);
    if (chats.status === "fulfilled") setConversations(chats.value);
    else setPartialErrors((prev) => [...prev, "Konversationer kunde inte läsas."]);
    setLoading(false);
  }, [refreshActivity, refreshHealth]);

  useEffect(() => {
    // Documented fetch-on-mount pattern, same convention as app/(shell)/chat/page.tsx: every
    // state write in loadAll() happens after an await; nothing is set synchronously here.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadAll();
    // Operational state is polled while the tab is visible: jobs/worker change without a user
    // action, and a "running" badge that never updates would be a false status.
    const timer = setInterval(() => {
      if (document.hidden) return;
      void refreshHealth();
      void refreshActivity();
    }, 10_000);
    return () => clearInterval(timer);
  }, [loadAll, refreshActivity, refreshHealth]);

  const runningJobs = useMemo(
    () => jobs.filter((job) => job.status === "running" || job.status === "queued" || job.status === "paused"),
    [jobs]
  );
  const indexed = documents.filter((doc) => doc.status === "indexed").length;
  const activeDecisions = decisions.filter((source) => source.active_truth_status === "active");

  const degraded =
    !!healthError ||
    health?.status === "unavailable" ||
    health?.local_model?.reachable === false ||
    (health?.local_model?.missing_models?.length ?? 0) > 0;

  const coreState: CoreState = asking
    ? "reasoning"
    : degraded
      ? "degraded"
      : runningJobs.length > 0
        ? "creating"
        : "resting";
  const coreMode: CoreMode = runningJobs.length > 0 ? "focus" : approvals.length > 0 ? "care" : "harmony";
  const coreLabel =
    coreState === "reasoning"
      ? "MainAI bearbetar din fråga"
      : coreState === "degraded"
        ? "En beroendetjänst är inte verifierad"
        : coreState === "creating"
          ? "Arbete pågår i bakgrunden"
          : "Vilar";

  const overall = statusLine(health, healthError);
  const model = modelLine(health);

  async function ask(event: React.FormEvent) {
    event.preventDefault();
    const text = question.trim();
    if (!text || asking) return;
    setAsking(true);
    setQuickError(null);
    try {
      const response = await api.sendChat(text, conversationId);
      setConversationId(response.conversation_id);
      if (response.assistant_status === "failed") {
        setQuickAnswer(null);
        setQuickError(response.error_message ?? "AI-svaret kunde inte genereras. Meddelandet sparades.");
      } else {
        setQuickAnswer({
          text: response.reply ?? "",
          provider: `${response.provider}/${response.model}`,
          sources: response.sources ?? [],
          confidence: response.confidence ?? undefined,
          confidenceScore: response.confidence_score ?? undefined,
        });
        setQuestion("");
        void api.listConversations().then(setConversations).catch(() => undefined);
      }
    } catch (e: unknown) {
      setQuickError(e instanceof Error ? e.message : "Frågan kunde inte skickas.");
    } finally {
      setAsking(false);
    }
  }

  async function cancelJob(id: string) {
    setBusyId(id);
    setActionError(null);
    try {
      await api.mainaiCancelJob(id);
      await refreshActivity();
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : "Avbrottet kunde inte registreras.");
    } finally {
      setBusyId(null);
    }
  }

  async function decideApproval(id: string, decision: "approve" | "reject") {
    setBusyId(id);
    setActionError(null);
    try {
      if (decision === "approve") await api.mainaiExecutionApproveTask(id);
      else await api.mainaiExecutionRejectTask(id);
      await refreshActivity();
    } catch (e: unknown) {
      setActionError(e instanceof Error ? e.message : "Beslutet kunde inte registreras.");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">Founder Console</h1>
          <p className="mt-1 text-sm text-white/50">
            Privat gränssnitt för MainAI. Statusen nedan kommer från backend, inte från gränssnittet.
          </p>
        </div>
        <div className="flex items-center gap-2 text-xs text-white/50">
          <span className={`h-2 w-2 rounded-full ${toneClasses(overall.tone)}`} aria-hidden="true" />
          <span>{overall.label}</span>
          <button
            type="button"
            onClick={() => {
              void refreshHealth();
              void refreshActivity();
            }}
            className="rounded-lg border border-border px-3 py-1.5 text-xs hover:bg-white/5"
          >
            Uppdatera
          </button>
        </div>
      </header>

      {(healthError || partialErrors.length > 0 || actionError) && (
        <div role="alert" className="space-y-1 rounded-xl border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200">
          {healthError && <div>Kunde inte nå backend: {healthError}. Kontrollera att API:et körs.</div>}
          {partialErrors.map((message) => (
            <div key={message}>{message}</div>
          ))}
          {actionError && <div>{actionError}</div>}
        </div>
      )}

      <section className="grid gap-4 lg:grid-cols-[minmax(0,320px)_1fr]">
        <div className="flex flex-col items-center justify-center gap-3 rounded-2xl border border-border bg-panel/70 p-6">
          <LivingCore state={coreState} mode={coreMode} size={220} />
          <div className="text-center" role="status" aria-live="polite">
            <div className="text-sm font-medium text-white/80">{coreLabel}</div>
            <div className="mt-1 text-[11px] uppercase tracking-widest text-white/35">
              Visuellt läge · {coreMode === "focus" ? "Fokus" : coreMode === "care" ? "Omtanke" : "Harmoni"}
            </div>
            <div className="mt-1 text-[11px] text-white/30">
              Living Core visar systemhändelser. Den är inte en känsla eller en säkerhetsbedömning.
            </div>
          </div>
        </div>

        <div className="rounded-2xl border border-border bg-panel/70 p-5">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-medium text-white/70">MainAI · driftstatus</h2>
            <span className="text-[11px] text-white/30">
              {loading
                ? "Läser…"
                : lastCheckedAt
                  ? `Senast kontrollerad ${lastCheckedAt.toLocaleTimeString("sv-SE")}`
                  : "Inte kontrollerad ännu"}
            </span>
          </div>
          <dl className="mt-4 space-y-3">
            <StatusRow tone={model.tone} label="Lokal modell" value={model.value} detail={model.detail} />
            <StatusRow
              tone={health ? (health.worker === "alive" ? "ok" : "warn") : "unknown"}
              label="Worker"
              value={health ? (health.worker === "alive" ? "Aktiv" : "Ej bekräftad") : "Okänd"}
              detail={
                health?.worker === "alive"
                  ? "Heartbeat mottagen från jobbworkern."
                  : "Ingen heartbeat har bekräftats — bakgrundsjobb kan stå still."
              }
            />
            <StatusRow
              tone={health ? (health.storage?.writable ? "ok" : "bad") : "unknown"}
              label="Lagring"
              value={health ? (health.storage?.writable ? "Skrivbar" : "Ej skrivbar") : "Okänd"}
              detail="Uppladdningar och dokumentindexering kräver skrivbar lagring."
            />
            <StatusRow
              tone={health ? (health.status === "ok" ? "ok" : "bad") : "unknown"}
              label="Databas"
              value={health ? (health.status === "ok" ? "Svarar" : "Svarar inte") : "Okänd"}
              detail="Verifieras av API:ets egen readiness-kontroll."
            />
          </dl>
        </div>
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <Panel title="Fortsätt samtalet" action={<Link className="text-xs text-accent2" href="/chat">Öppna chatten</Link>}>
          {quickAnswer ? (
            <div className="space-y-3">
              <p className="whitespace-pre-wrap rounded-xl border border-border bg-base/60 p-3 text-sm text-white/80">
                {quickAnswer.text}
              </p>
              <div className="flex flex-wrap items-center gap-2 text-[11px] text-white/40">
                {quickAnswer.confidence && (
                  <ConfidenceBadge confidence={quickAnswer.confidence} score={quickAnswer.confidenceScore ?? 0} />
                )}
                <span>{quickAnswer.provider}</span>
              </div>
              {quickAnswer.sources.length > 0 && (
                <div className="space-y-1 text-xs text-white/45">
                  <div>Källor:</div>
                  {quickAnswer.sources.map((source) => (
                    <Link
                      key={`${source.document_id}-${source.start_seconds ?? ""}`}
                      href={`/library/${source.document_id}${source.start_seconds != null ? `?t=${source.start_seconds}` : ""}`}
                      className="block border-l border-border pl-2 hover:border-accent hover:text-white/70"
                    >
                      {source.title} ({source.score.toFixed(2)})
                    </Link>
                  ))}
                </div>
              )}
            </div>
          ) : (
            <p className="text-sm text-white/45">
              Ställ en fråga — svaret genereras av den lokala modellen och citerar dina indexerade dokument.
            </p>
          )}
          {quickError && (
            <div role="alert" className="mt-3 rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-xs text-red-300">
              {quickError}
            </div>
          )}
          <form onSubmit={ask} className="mt-4 flex gap-2">
            <label className="sr-only" htmlFor="console-question">Fråga MainAI</label>
            <input
              id="console-question"
              ref={inputRef}
              value={question}
              onChange={(event) => setQuestion(event.target.value)}
              placeholder="Fråga MainAI…"
              className="min-w-0 flex-1 rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
            <button
              type="submit"
              disabled={asking || !question.trim()}
              className="rounded-lg bg-accent px-4 py-2 text-sm font-medium disabled:opacity-40"
            >
              {asking ? "Tänker…" : "Skicka"}
            </button>
          </form>
          {conversations[0] && (
            <p className="mt-3 text-[11px] text-white/30">
              Senaste samtal: {conversations[0].title} · sparas och kan återupptas efter omstart.
            </p>
          )}
        </Panel>

        <Panel
          title="Pågående arbete"
          action={
            <Link className="text-xs text-accent2" href="/admin/jobs">
              Alla jobb
            </Link>
          }
        >
          {runningJobs.length === 0 ? (
            <p className="text-sm text-white/45">Inga jobb körs just nu.</p>
          ) : (
            <ul className="space-y-3">
              {runningJobs.slice(0, 4).map((job) => (
                <li key={job.id} className="rounded-xl border border-border bg-base/50 p-3">
                  <div className="flex items-center justify-between gap-2 text-sm">
                    <span className="truncate text-white/80">{job.job_type}</span>
                    <span className="text-[11px] uppercase tracking-wide text-accent2">{job.status}</span>
                  </div>
                  <div className="mt-1 text-[11px] text-white/40">
                    {job.current_phase ?? "Bearbetar"}
                    {job.progress_total
                      ? ` · ${job.progress_current}/${job.progress_total}`
                      : ` · ${job.progress_current} steg`}
                  </div>
                  {job.progress_total ? (
                    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-white/10" aria-hidden="true">
                      <div
                        className="h-full rounded-full bg-accent2/70"
                        style={{ width: `${Math.min(100, Math.round((job.progress_current / job.progress_total) * 100))}%` }}
                      />
                    </div>
                  ) : null}
                  {CANCELLABLE_MAINAI_JOB_STATUSES.includes(job.status) && (
                    <button
                      type="button"
                      onClick={() => void cancelJob(job.id)}
                      disabled={busyId === job.id || job.cancel_requested}
                      className="mt-3 rounded-lg border border-border px-3 py-1 text-xs text-white/70 hover:border-red-400/40 hover:text-red-300 disabled:opacity-40"
                    >
                      {job.cancel_requested ? "Avbrott begärt…" : "Avbryt"}
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </section>

      <section className="grid gap-4 lg:grid-cols-2">
        <Panel
          title="Kräver ditt godkännande"
          action={<Link className="text-xs text-accent2" href="/admin/mainai-execution">Execution</Link>}
        >
          <p className="mb-3 text-[11px] text-white/35">
            Åtgärder som MainAI inte får utföra autonomt. Godkännande registreras i backend och krävs innan uppgiften fortsätter.
          </p>
          {approvalsUnreadable ? (
            <p className="text-sm text-amber-300/80">
              Kön kunde inte läsas — status är okänd, inte tom.
            </p>
          ) : approvals.length === 0 ? (
            <p className="text-sm text-white/45">Inga åtgärder väntar på godkännande.</p>
          ) : (
            <ul className="space-y-3">
              {approvals.slice(0, 4).map((task) => (
                <li key={task.id} className="rounded-xl border border-border bg-base/50 p-3">
                  <div className="text-sm text-white/80">{task.description}</div>
                  <div className="mt-1 text-[11px] text-white/40">
                    {task.task_type} · risk {task.risk_level} · {task.status}
                  </div>
                  <div className="mt-3 flex gap-2">
                    <button
                      type="button"
                      onClick={() => void decideApproval(task.id, "approve")}
                      disabled={busyId === task.id}
                      className="rounded-lg bg-accent px-3 py-1 text-xs font-medium disabled:opacity-40"
                    >
                      Godkänn
                    </button>
                    <button
                      type="button"
                      onClick={() => void decideApproval(task.id, "reject")}
                      disabled={busyId === task.id}
                      className="rounded-lg border border-border px-3 py-1 text-xs text-white/70 hover:text-red-300 disabled:opacity-40"
                    >
                      Avslå
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel
          title="Aktiva beslut"
          action={<Link className="text-xs text-accent2" href="/library?classification=decisions">Beslutsarkiv</Link>}
        >
          {activeDecisions.length === 0 ? (
            <p className="text-sm text-white/45">Inga aktiva beslut är sparade ännu.</p>
          ) : (
            <ul className="space-y-2">
              {activeDecisions.slice(0, 5).map((decision) => (
                <li key={decision.id} className="flex items-start justify-between gap-3 border-b border-border/60 py-2 last:border-0">
                  <Link href={`/library/${decision.id}`} className="min-w-0 text-sm text-white/80 hover:text-accent2">
                    <span className="block truncate">{decision.title}</span>
                    <span className="text-[11px] text-white/35">v{decision.version_number} · {decision.classification}</span>
                  </Link>
                  <span className="shrink-0 rounded-full border border-emerald-400/30 px-2 py-0.5 text-[10px] text-emerald-300">
                    {decision.active_truth_status}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      </section>

      <section className="grid gap-4 md:grid-cols-3">
        <Metric label="Indexerade dokument" value={`${indexed} / ${documents.length}`} href="/library" />
        <Metric label="Aktiva beslut" value={String(activeDecisions.length)} href="/library?classification=decisions" />
        <Metric label="Konversationer" value={String(conversations.length)} href="/chat" />
      </section>
    </div>
  );
}

function StatusRow({
  tone,
  label,
  value,
  detail,
}: {
  tone: HealthTone;
  label: string;
  value: string;
  detail: string;
}) {
  return (
    <div className="flex items-start gap-3">
      <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${toneClasses(tone)}`} aria-hidden="true" />
      <div className="min-w-0">
        <dt className="text-[11px] uppercase tracking-wide text-white/35">{label}</dt>
        <dd className="truncate text-sm text-white/80">{value}</dd>
        <dd className="text-[11px] text-white/35">{detail}</dd>
      </div>
    </div>
  );
}

function Panel({
  title,
  action,
  children,
}: {
  title: string;
  action?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <div className="rounded-2xl border border-border bg-panel/70 p-5">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="text-sm font-medium text-white/70">{title}</h2>
        {action}
      </div>
      {children}
    </div>
  );
}

function Metric({ label, value, href }: { label: string; value: string; href: string }) {
  return (
    <Link href={href} className="rounded-2xl border border-border bg-panel/70 p-4 hover:border-accent/40">
      <div className="text-[11px] uppercase tracking-wide text-white/35">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
    </Link>
  );
}
