"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, ProjectItem } from "@/lib/api";

export default function WorkspacesPage() {
  const [workspaces, setWorkspaces] = useState<ProjectItem[]>([]);
  const [includeArchived, setIncludeArchived] = useState(false);
  const [name, setName] = useState("");
  const [purpose, setPurpose] = useState("");
  const [goals, setGoals] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function refresh(archived = includeArchived) {
    try {
      setError(null);
      setWorkspaces(await api.listWorkspaces(archived));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // Documented fetch-on-mount pattern used by the other shell pages.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh(includeArchived);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [includeArchived]);

  async function createWorkspace() {
    if (!name.trim() || creating) return;
    setCreating(true);
    setError(null);
    try {
      await api.createWorkspace({
        name: name.trim(),
        purpose: purpose.trim() || null,
        status: "active",
        goals: goals
          .split(/[\n,]+/)
          .map((goal) => goal.trim())
          .filter(Boolean),
      });
      setName("");
      setPurpose("");
      setGoals("");
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setCreating(false);
    }
  }

  async function toggleArchive(workspace: ProjectItem) {
    try {
      if (workspace.archived_at) await api.restoreWorkspace(workspace.id);
      else await api.archiveWorkspace(workspace.id);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    }
  }

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Life Workspaces</h1>
          <p className="mt-1 max-w-2xl text-sm text-white/50">
            Samla ett pågående projekt, dess källor, samtal, beslut och uppgifter på ett ställe.
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm text-white/60">
          <input
            type="checkbox"
            checked={includeArchived}
            onChange={(event) => setIncludeArchived(event.target.checked)}
          />
          Visa arkiverade
        </label>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      <section aria-labelledby="new-workspace" className="rounded-xl border border-border bg-panel p-4">
        <h2 id="new-workspace" className="text-sm font-medium text-white/70">
          Nytt workspace
        </h2>
        <div className="mt-3 grid gap-3 md:grid-cols-[1fr_2fr_auto]">
          <div>
            <label htmlFor="workspace-name" className="mb-1 block text-xs text-white/40">
              Namn
            </label>
            <input
              id="workspace-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && createWorkspace()}
              placeholder="Projektets namn…"
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
          <div>
            <label htmlFor="workspace-purpose" className="mb-1 block text-xs text-white/40">
              Syfte (valfritt)
            </label>
            <input
              id="workspace-purpose"
              value={purpose}
              onChange={(event) => setPurpose(event.target.value)}
              placeholder="Vad ska det här projektet åstadkomma?"
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
          <div className="flex items-end">
            <button
              type="button"
              onClick={createWorkspace}
              disabled={creating || !name.trim()}
              className="w-full rounded-lg bg-accent px-4 py-2 text-sm disabled:opacity-50 md:w-auto"
            >
              {creating ? "Skapar…" : "Skapa workspace"}
            </button>
          </div>
          <div className="md:col-span-3">
            <label htmlFor="workspace-goals" className="mb-1 block text-xs text-white/40">
              Mål (ett per rad)
            </label>
            <textarea
              id="workspace-goals"
              value={goals}
              onChange={(event) => setGoals(event.target.value)}
              rows={2}
              placeholder="Vad ska vara sant när projektet lyckas?"
              className="w-full resize-none rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
        </div>
      </section>

      {loading ? (
        <div role="status" className="text-sm text-white/40">
          Läser workspaces…
        </div>
      ) : workspaces.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border p-8 text-center text-sm text-white/40">
          Inga workspaces ännu. Skapa ett ovan för att börja samla projektet.
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {workspaces.map((workspace) => (
            <article key={workspace.id} className="flex flex-col rounded-xl border border-border bg-panel p-4">
              <div className="flex items-start justify-between gap-3">
                <Link
                  href={`/workspaces/${workspace.id}`}
                  className="text-base font-medium text-white/90 hover:text-accent hover:underline"
                >
                  {workspace.name}
                </Link>
                <span className="shrink-0 rounded-full border border-border px-2 py-0.5 text-xs text-white/50">
                  {workspace.archived_at ? "arkiverad" : workspace.status}
                </span>
              </div>
              <p className="mt-2 line-clamp-3 min-h-[3rem] text-sm text-white/50">
                {workspace.purpose || workspace.description || "Inget syfte formulerat ännu."}
              </p>
              <div className="mt-3 flex flex-wrap gap-2 text-xs text-white/40">
                <span>{(workspace.goals ?? []).length} mål</span>
                <span>·</span>
                <span>
                  {workspace.last_activity_at
                    ? `senast ${new Date(workspace.last_activity_at).toLocaleDateString("sv-SE")}`
                    : "ingen aktivitet"}
                </span>
              </div>
              <div className="mt-4 flex items-center justify-between gap-2 border-t border-border/60 pt-3">
                <Link href={`/workspaces/${workspace.id}`} className="text-sm text-accent2 hover:underline">
                  Öppna →
                </Link>
                <button
                  type="button"
                  onClick={() => toggleArchive(workspace)}
                  className="text-xs text-white/50 hover:text-white"
                >
                  {workspace.archived_at ? "Återställ" : "Arkivera"}
                </button>
              </div>
            </article>
          ))}
        </div>
      )}
    </div>
  );
}
