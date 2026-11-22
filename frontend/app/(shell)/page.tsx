"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, DashboardData, ProjectStatus } from "@/lib/api";
import ProjectOverview from "@/components/ProjectOverview";

const STATUS_LABELS: Record<string, string> = {
  todo: "Att göra",
  in_progress: "Pågår",
  blocked: "Blockerad",
  done: "Klar",
  cancelled: "Avbruten",
};

const PRIORITY_LABELS: Record<string, string> = {
  low: "Låg",
  medium: "Medel",
  high: "Hög",
  critical: "Kritisk",
};

function formatDate(value: string | null | undefined, withTime = false): string {
  if (!value) return "—";
  return new Date(value).toLocaleString("sv-SE", {
    dateStyle: "medium",
    ...(withTime ? { timeStyle: "short" } : {}),
  });
}

export default function DashboardPage() {
  const [data, setData] = useState<DashboardData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [projectStatus, setProjectStatus] = useState<ProjectStatus | null>(null);
  const [projectStatusError, setProjectStatusError] = useState<string | null>(null);

  useEffect(() => {
    api
      .dashboard()
      .then(setData)
      .catch((e: any) => setError(e.message))
      .finally(() => setLoading(false));

    api
      .projectStatus()
      .then(setProjectStatus)
      .catch((e) => setProjectStatusError(e instanceof Error ? e.message : "Okänt fel"));
  }, []);

  if (loading) {
    return (
      <div className="space-y-6" role="status" aria-live="polite">
        <div>
          <h1 className="text-2xl font-semibold">Dashboard</h1>
          <p className="mt-1 text-sm text-white/50">Läser din samlade arbetsstatus…</p>
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
          {Array.from({ length: 8 }).map((_, index) => (
            <div key={index} className="h-24 animate-pulse rounded-xl border border-border bg-panel" />
          ))}
        </div>
        <div className="h-64 animate-pulse rounded-xl border border-border bg-panel" />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-semibold">Dashboard</h1>
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          Kunde inte läsa dashboarden: {error ?? "okänt fel"}. Kontrollera att API:et körs och försök igen.
        </div>
        <button
          type="button"
          onClick={() => window.location.reload()}
          className="rounded-lg border border-border px-4 py-2 text-sm text-white/70 hover:text-white"
        >
          Försök igen
        </button>
      </div>
    );
  }

  const { counts } = data;
  const hasWork =
    data.active_projects.length > 0 ||
    data.outstanding_tasks.length > 0 ||
    data.upcoming_events.length > 0 ||
    data.recent_decisions.length > 0 ||
    data.recent_documents.length > 0 ||
    data.active_conversations.length > 0;

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">God kväll, Dennis</h1>
          <p className="mt-1 text-sm text-white/50">
            Statusen kommer från LifeAI:s register. Uppdaterad{" "}
            {new Date(data.generated_at).toLocaleString("sv-SE")}.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Link href="/tasks" className="rounded-lg bg-accent px-4 py-2 text-sm font-medium">
            Öppna uppgifter
          </Link>
          <Link href="/calendar" className="rounded-lg border border-border px-4 py-2 text-sm">
            Kalender
          </Link>
          <Link href="/search" className="rounded-lg border border-border px-4 py-2 text-sm">
            Sök
          </Link>
        </div>
      </div>

      {!hasWork && (
        <div className="rounded-xl border border-dashed border-border p-6 text-sm text-white/50">
          <div className="font-medium text-white/75">Här är det tomt ännu.</div>
          <p className="mt-1">
            Börja med att skapa ett projekt, en uppgift eller ett kalenderevenemang. Dashboarden
            visar bara verklig data från databasen — inga exempelvärden.
          </p>
          <div className="mt-3 flex flex-wrap gap-2">
            <Link href="/workspaces" className="rounded-lg bg-accent px-3 py-2 text-sm">
              Skapa workspace
            </Link>
            <Link href="/tasks" className="rounded-lg border border-border px-3 py-2 text-sm">
              Skapa uppgift
            </Link>
            <Link href="/library" className="rounded-lg border border-border px-3 py-2 text-sm">
              Ladda upp kunskap
            </Link>
          </div>
        </div>
      )}

      <ProjectOverview status={projectStatus} error={projectStatusError} />

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard label="Aktiva projekt" value={counts.active_projects} href="/projects" />
        <StatCard label="Öppna uppgifter" value={counts.open_tasks} href="/tasks" />
        <StatCard label="Blockerade" value={counts.blocked_tasks} href="/tasks?blocked=1" tone="warn" />
        <StatCard label="Kommande evenemang" value={counts.upcoming_events} href="/calendar" />
        <StatCard label="Senaste beslut" value={counts.recent_decisions} href="/workspaces" />
        <StatCard label="Dokument" value={counts.recent_documents} href="/library" />
        <StatCard label="Aktiva samtal" value={counts.active_conversations} href="/chat" />
        <StatCard
          label="MainAI supervision"
          value={counts.active_mainai_goals}
          detail={`${counts.running_mainai_tasks} körs · ${counts.awaiting_approval_tasks} väntar`}
          href="/admin/mainai-execution"
        />
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Panel title="Aktiva projekt" href="/projects" linkLabel="Alla projekt">
          {data.active_projects.map((project) => (
            <Link
              key={project.id}
              href={`/projects/${project.id}`}
              className="flex justify-between gap-3 border-b border-border/60 py-2 text-sm last:border-0 hover:text-accent"
            >
              <span className="min-w-0 truncate text-white/80">{project.name}</span>
              <span className="shrink-0 text-xs text-white/40">{project.status}</span>
            </Link>
          ))}
          {data.active_projects.length === 0 && <EmptyRow text="Inga aktiva projekt." />}
        </Panel>

        <Panel title="Öppna uppgifter" href="/tasks" linkLabel="Alla uppgifter">
          {data.outstanding_tasks.map((task) => (
            <div key={task.id} className="border-b border-border/60 py-2 text-sm last:border-0">
              <div className="flex items-start justify-between gap-2">
                <span className="min-w-0 truncate text-white/80">{task.title}</span>
                <span className="shrink-0 text-xs text-white/40">
                  {PRIORITY_LABELS[task.priority] ?? task.priority}
                </span>
              </div>
              <div className="mt-0.5 flex flex-wrap gap-2 text-xs text-white/35">
                <span>{STATUS_LABELS[task.status] ?? task.status}</span>
                <span>·</span>
                <span>{formatDate(task.due_at)}</span>
                {task.is_blocked && <span className="text-amber-200">· Blockerad</span>}
              </div>
            </div>
          ))}
          {data.outstanding_tasks.length === 0 && <EmptyRow text="Inga öppna uppgifter." />}
        </Panel>

        <Panel title="Blockerat arbete" href="/tasks?blocked=1" linkLabel="Visa blockerade">
          {data.blocked_tasks.map((task) => (
            <div key={task.id} className="border-b border-border/60 py-2 text-sm last:border-0">
              <div className="text-white/80">{task.title}</div>
              <div className="mt-0.5 text-xs text-amber-200/80">
                {task.blocked_reason || "Väntar på ett beroende."}
              </div>
            </div>
          ))}
          {data.blocked_tasks.length === 0 && <EmptyRow text="Inget blockerat arbete." />}
        </Panel>

        <Panel title="Kommande evenemang" href="/calendar" linkLabel="Öppna kalendern">
          {data.upcoming_events.map((event) => (
            <div key={event.id} className="flex justify-between gap-3 border-b border-border/60 py-2 text-sm last:border-0">
              <span className="min-w-0 truncate text-white/80">{event.title}</span>
              <span className="shrink-0 text-xs text-white/40">{formatDate(event.starts_at, true)}</span>
            </div>
          ))}
          {data.upcoming_events.length === 0 && <EmptyRow text="Inga kommande evenemang." />}
        </Panel>

        <Panel title="Senaste beslut" href="/workspaces" linkLabel="Workspaces">
          {data.recent_decisions.map((decision) => (
            <div key={decision.id} className="border-b border-border/60 py-2 text-sm last:border-0">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="min-w-0 truncate text-white/80">{decision.title}</span>
                <span className="shrink-0 text-[10px] text-white/40">
                  {decision.epistemic_status === "confirmed"
                    ? "Bekräftat"
                    : decision.epistemic_status === "uncertain"
                      ? "Osäkert"
                      : "AI-förslag"}
                </span>
              </div>
              <p className="mt-0.5 line-clamp-2 text-xs text-white/40">{decision.decision}</p>
            </div>
          ))}
          {data.recent_decisions.length === 0 && <EmptyRow text="Inga beslut registrerade." />}
        </Panel>

        <Panel title="Senaste dokument" href="/library" linkLabel="Biblioteket">
          {data.recent_documents.map((document) => (
            <Link
              key={document.id}
              href={`/library/${document.id}`}
              className="flex justify-between gap-3 border-b border-border/60 py-2 text-sm last:border-0 hover:text-accent"
            >
              <span className="min-w-0 truncate text-white/80">{document.title}</span>
              <span className="shrink-0 text-xs text-white/40">{document.status}</span>
            </Link>
          ))}
          {data.recent_documents.length === 0 && <EmptyRow text="Inga dokument ännu." />}
        </Panel>

        <Panel title="Aktiva samtal" href="/chat" linkLabel="Öppna chat">
          {data.active_conversations.map((conversation) => (
            <Link
              key={conversation.id}
              href={`/chat?conversation=${conversation.id}`}
              className="flex justify-between gap-3 border-b border-border/60 py-2 text-sm last:border-0 hover:text-accent"
            >
              <span className="min-w-0 truncate text-white/80">{conversation.title}</span>
              <span className="shrink-0 text-xs text-white/40">{formatDate(conversation.updated_at)}</span>
            </Link>
          ))}
          {data.active_conversations.length === 0 && <EmptyRow text="Inga samtal ännu." />}
        </Panel>

        <Panel title="Supervised MainAI" href="/admin/mainai-execution" linkLabel="MainAI Execution">
          {data.supervised_goals.map((goal) => (
            <div key={goal.id} className="border-b border-border/60 py-2 text-sm last:border-0">
              <div className="flex justify-between gap-2">
                <span className="min-w-0 truncate text-white/80">{goal.title}</span>
                <span className="shrink-0 text-xs text-white/40">{goal.status}</span>
              </div>
              <div className="mt-0.5 text-xs text-white/35">plan v{goal.current_plan_version}</div>
            </div>
          ))}
          {data.supervised_tasks.slice(0, 3).map((task) => (
            <div key={task.id} className="border-b border-border/60 py-2 text-sm last:border-0">
              <div className="truncate text-white/70">{task.description}</div>
              <div className="mt-0.5 text-xs text-white/35">{task.status}</div>
            </div>
          ))}
          {data.supervised_goals.length === 0 && data.supervised_tasks.length === 0 && (
            <EmptyRow text="Ingen aktiv MainAI-körning." />
          )}
        </Panel>
      </div>
    </div>
  );
}

function StatCard({
  label,
  value,
  detail,
  href,
  tone,
}: {
  label: string;
  value: number;
  detail?: string;
  href: string;
  tone?: "warn";
}) {
  return (
    <Link
      href={href}
      className={`rounded-xl border bg-panel p-4 transition-colors hover:border-accent/50 ${
        tone === "warn" && value > 0 ? "border-amber-500/40" : "border-border"
      }`}
    >
      <div className="text-xs uppercase tracking-wide text-white/40">{label}</div>
      <div className={`mt-2 text-2xl font-semibold ${tone === "warn" && value > 0 ? "text-amber-200" : ""}`}>
        {value}
      </div>
      {detail && <div className="mt-1 text-xs text-white/35">{detail}</div>}
    </Link>
  );
}

function Panel({
  title,
  href,
  linkLabel,
  children,
}: {
  title: string;
  href: string;
  linkLabel: string;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-border bg-panel p-4">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 className="text-sm font-medium text-white/70">{title}</h2>
        <Link href={href} className="text-xs text-accent2 hover:underline">
          {linkLabel} →
        </Link>
      </div>
      {children}
    </section>
  );
}

function EmptyRow({ text }: { text: string }) {
  return <div className="py-2 text-sm text-white/30">{text}</div>;
}
