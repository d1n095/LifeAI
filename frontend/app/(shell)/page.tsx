"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, AuthError, DocumentItem, FounderOpsSnapshot, NetworkError, ProjectItem, TaskItem } from "@/lib/api";

function errorMessage(reason: unknown, fallback: string): string {
  if (reason instanceof Error && reason.message) return reason.message;
  return fallback;
}

export default function DashboardPage() {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [snapshot, setSnapshot] = useState<FounderOpsSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([
      api.listDocuments(),
      api.listProjects(),
      api.listTasks(),
      api.founderOpsSnapshot(),
    ]).then((results) => {
      if (cancelled) return;
      const messages: string[] = [];
      if (results[0].status === "fulfilled") setDocuments(results[0].value);
      else messages.push(errorMessage(results[0].reason, "dokument"));
      if (results[1].status === "fulfilled") setProjects(results[1].value);
      else messages.push(errorMessage(results[1].reason, "projekt"));
      if (results[2].status === "fulfilled") setTasks(results[2].value);
      else messages.push(errorMessage(results[2].reason, "uppgifter"));
      if (results[3].status === "fulfilled") setSnapshot(results[3].value);
      else {
        const reason = results[3].reason;
        if (!(reason instanceof AuthError)) {
          messages.push(reason instanceof NetworkError ? reason.message : errorMessage(reason, "Founder-status"));
        }
      }
      if (messages.length) setError(messages.join(" · "));
      setLoaded(true);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const indexed = documents.filter((d) => d.status === "indexed").length;
  const pendingTasks = tasks.filter((t) => t.status !== "done").length;
  const activeProjects = projects.filter((p) => p.status === "active").length;
  const conversationsToday = snapshot ? snapshot.conversation_count_today : null;

  const healthLine = statusLine(snapshot, error, loaded);

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">God kväll</h1>
        <p className="text-white/50 text-sm mt-1" data-testid="dashboard-health">
          {healthLine}
        </p>
      </div>

      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300"
        >
          Kunde inte nå backend: {error}. Kontrollera att API:et körs (se README).
        </div>
      )}

      {snapshot?.stop?.blocked && (
        <div role="alert" className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-4 text-sm text-amber-200">
          STOP aktiv{snapshot.stop.code ? ` (${snapshot.stop.code})` : ""}. Inget arbete ska behandlas som framåtskridande.
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <StatCard label="Indexerade dokument" value={indexed} total={documents.length} />
        <StatCard label="Aktiva projekt" value={activeProjects} total={projects.length} />
        <StatCard label="Öppna uppgifter" value={pendingTasks} total={tasks.length} />
        <StatCard
          label="Konversationer idag"
          value={conversationsToday}
          total={conversationsToday ?? 0}
          hideTotal
          unknown={conversationsToday === null}
        />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Panel title="Senaste dokument">
          {documents.slice(0, 5).map((d) => (
            <div key={d.id} className="flex justify-between text-sm py-2 border-b border-border/60 last:border-0">
              <span className="text-white/80">{d.title}</span>
              <span className="text-white/40">{d.status}</span>
            </div>
          ))}
          {documents.length === 0 && <EmptyRow />}
        </Panel>

        <Panel title="Öppna uppgifter">
          {tasks.slice(0, 5).map((t) => (
            <div key={t.id} className="flex justify-between text-sm py-2 border-b border-border/60 last:border-0">
              <span className="text-white/80">{t.title}</span>
              <span className="text-white/40">{t.priority}</span>
            </div>
          ))}
          {tasks.length === 0 && <EmptyRow />}
        </Panel>
      </div>

      <div className="flex gap-3">
        <Link href="/chat" className="rounded-lg bg-accent px-4 py-2 text-sm font-medium">
          Öppna chat
        </Link>
        <Link href="/admin/founder-ops" className="rounded-lg border border-border px-4 py-2 text-sm">
          Founder-status
        </Link>
        <Link href="/documents" className="rounded-lg border border-border px-4 py-2 text-sm">
          Ladda upp dokument
        </Link>
      </div>
    </div>
  );
}

function statusLine(snapshot: FounderOpsSnapshot | null, error: string | null, loaded: boolean): string {
  if (!loaded) return "Hämtar systemstatus…";
  if (error && !snapshot) return "Backend otillgänglig — ingen driftsstatus påstås.";
  if (error) return "Backend delvis otillgänglig — ingen samlad driftsstatus påstås.";
  if (!snapshot) return "Founder-status inte inläst.";
  const parts = [
    snapshot.health.backend_reachable ? "Backend nåbar" : "Backend otillgänglig",
    `beredskap ${snapshot.startup_readiness.level}`,
    snapshot.stop.blocked ? "STOP blockerar" : "STOP inaktiv",
    snapshot.safe_launch.privileged_remote_effects_enabled ? "fjärrskrivning på" : "fjärrskrivning av",
    snapshot.safe_launch.recall_http_mounted ? "Recall monterad" : "Recall av",
  ];
  if (snapshot.first_cycle_go) parts.push("GO (oväntat)");
  return parts.join(" · ");
}

function StatCard({
  label,
  value,
  total,
  hideTotal,
  unknown,
}: {
  label: string;
  value: number | null;
  total: number;
  hideTotal?: boolean;
  unknown?: boolean;
}) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-xs uppercase tracking-wide text-white/40">{label}</div>
      <div className="mt-2 text-2xl font-semibold">
        {unknown || value === null ? "—" : value}
        {!hideTotal && !unknown && value !== null && <span className="text-white/30 text-base"> / {total}</span>}
      </div>
    </div>
  );
}

function Panel({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-sm font-medium text-white/70 mb-2">{title}</div>
      {children}
    </div>
  );
}

function EmptyRow() {
  return <div className="text-sm text-white/30 py-2">Inget här ännu.</div>;
}
