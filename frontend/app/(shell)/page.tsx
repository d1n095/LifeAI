"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, DocumentItem, ProjectItem, TaskItem } from "@/lib/api";

export default function DashboardPage() {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.listDocuments(), api.listProjects(), api.listTasks()])
      .then(([d, p, t]) => {
        setDocuments(d);
        setProjects(p);
        setTasks(t);
      })
      .catch((e) => setError(e.message));
  }, []);

  const indexed = documents.filter((d) => d.status === "indexed").length;
  const pendingTasks = tasks.filter((t) => t.status !== "done").length;
  const activeProjects = projects.filter((p) => p.status === "active").length;

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold">God kväll, Dennis</h1>
        <p className="text-white/50 text-sm mt-1">Allt system operationellt — anslutet till backend.</p>
      </div>

      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300"
        >
          Kunde inte nå backend: {error}. Kontrollera att API:et körs (se README).
        </div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
        <StatCard label="Indexerade dokument" value={indexed} total={documents.length} />
        <StatCard label="Aktiva projekt" value={activeProjects} total={projects.length} />
        <StatCard label="Öppna uppgifter" value={pendingTasks} total={tasks.length} />
        <StatCard label="Konversationer idag" value={0} total={0} hideTotal />
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
        <Link href="/documents" className="rounded-lg border border-border px-4 py-2 text-sm">
          Ladda upp dokument
        </Link>
      </div>
    </div>
  );
}

function StatCard({ label, value, total, hideTotal }: { label: string; value: number; total: number; hideTotal?: boolean }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-xs uppercase tracking-wide text-white/40">{label}</div>
      <div className="mt-2 text-2xl font-semibold">
        {value}
        {!hideTotal && <span className="text-white/30 text-base"> / {total}</span>}
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
