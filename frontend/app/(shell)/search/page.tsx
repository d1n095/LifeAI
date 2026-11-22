"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, ProjectItem, SearchResponse } from "@/lib/api";

const TYPE_LABELS: Record<string, string> = {
  project: "Projekt",
  task: "Uppgift",
  document: "Dokument",
  conversation: "Konversation",
  calendar_event: "Kalender",
};

const TYPE_OPTIONS = ["", "project", "task", "document", "conversation", "calendar_event"];
const PAGE_SIZE = 10;

function dateStart(value: string): string | undefined {
  return value ? new Date(`${value}T00:00:00`).toISOString() : undefined;
}

function dateEnd(value: string): string | undefined {
  return value ? new Date(`${value}T23:59:59`).toISOString() : undefined;
}

export default function SearchPage() {
  const [query, setQuery] = useState("");
  const [type, setType] = useState("");
  const [projectId, setProjectId] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [response, setResponse] = useState<SearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);

  useEffect(() => {
    api.listProjects().then(setProjects).catch(() => setProjects([]));
  }, []);

  async function run(offset: number) {
    const term = query.trim();
    if (term.length < 2) {
      setError("Sökordet måste vara minst två tecken.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const result = await api.searchPersonalWork({
        q: term,
        type: type || undefined,
        project_id: projectId || undefined,
        date_from: dateStart(dateFrom),
        date_to: dateEnd(dateTo),
        limit: PAGE_SIZE,
        offset,
      });
      setResponse(result);
      setSearched(true);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  function submit(event: React.FormEvent) {
    event.preventDefault();
    run(0);
  }

  const results = response?.results ?? [];
  const offset = response?.offset ?? 0;
  const total = response?.total ?? 0;
  const hasPrevious = offset > 0;
  const hasNext = offset + PAGE_SIZE < total;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Sök</h1>
        <p className="mt-1 max-w-3xl text-sm text-white/50">
          Sök i dina projekt, uppgifter, dokument, konversationer och kalenderhändelser. Varje
          resultat är owner-filtrerat före läsning och länkar tillbaka till källan.
        </p>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      <form onSubmit={submit} className="rounded-xl border border-border bg-panel p-4">
        <div className="flex flex-col gap-2 sm:flex-row">
          <div className="min-w-0 flex-1">
            <label htmlFor="search-q" className="mb-1 block text-xs text-white/40">
              Sökord
            </label>
            <input
              id="search-q"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="Sök i ditt personliga arbete…"
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
          <div className="flex items-end">
            <button
              type="submit"
              disabled={loading || query.trim().length < 2}
              className="rounded-lg bg-accent px-4 py-2 text-sm disabled:opacity-50"
            >
              {loading ? "Söker…" : "Sök"}
            </button>
          </div>
        </div>

        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <div>
            <label htmlFor="search-type" className="mb-1 block text-xs text-white/40">
              Objekttyp
            </label>
            <select
              id="search-type"
              value={type}
              onChange={(event) => setType(event.target.value)}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            >
              {TYPE_OPTIONS.map((value) => (
                <option key={value || "all"} value={value}>
                  {value ? TYPE_LABELS[value] : "Alla typer"}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="search-project" className="mb-1 block text-xs text-white/40">
              Projekt
            </label>
            <select
              id="search-project"
              value={projectId}
              onChange={(event) => setProjectId(event.target.value)}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            >
              <option value="">Alla projekt</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="search-from" className="mb-1 block text-xs text-white/40">
              Från datum
            </label>
            <input
              id="search-from"
              type="date"
              value={dateFrom}
              onChange={(event) => setDateFrom(event.target.value)}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label htmlFor="search-to" className="mb-1 block text-xs text-white/40">
              Till datum
            </label>
            <input
              id="search-to"
              type="date"
              value={dateTo}
              onChange={(event) => setDateTo(event.target.value)}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            />
          </div>
        </div>
      </form>

      <section aria-labelledby="search-results" className="rounded-xl border border-border bg-panel p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="search-results" className="text-sm font-medium text-white/70">
            Resultat
          </h2>
          {response && (
            <span className="text-xs text-white/40">
              {total} träffar · visar {results.length === 0 ? 0 : offset + 1}–
              {Math.min(offset + results.length, total)}
            </span>
          )}
        </div>

        {!searched && !loading ? (
          <p className="mt-4 text-sm text-white/30">Ange minst två tecken och välj Sök.</p>
        ) : loading ? (
          <p role="status" className="mt-4 text-sm text-white/40">
            Söker…
          </p>
        ) : results.length === 0 ? (
          <div className="mt-4 rounded-lg border border-dashed border-border p-8 text-center text-sm text-white/40">
            Inga träffar. Prova ett bredare sökord eller rensa filtren.
          </div>
        ) : (
          <>
            <ul className="mt-4 space-y-2">
              {results.map((result) => (
                <li key={`${result.type}-${result.id}`} className="rounded-lg border border-border/60 p-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <Link href={result.href} className="text-sm text-white/85 hover:text-accent hover:underline">
                      {result.title}
                    </Link>
                    <span className="rounded-full border border-border px-2 py-0.5 text-[10px] text-white/50">
                      {TYPE_LABELS[result.type] ?? result.type}
                    </span>
                  </div>
                  <p className="mt-1 text-sm text-white/50">{result.snippet}</p>
                  <div className="mt-1 flex flex-wrap gap-3 text-xs text-white/35">
                    {result.updated_at && (
                      <span>Uppdaterad {new Date(result.updated_at).toLocaleDateString("sv-SE")}</span>
                    )}
                    {result.occurred_at && (
                      <span>Inträffar {new Date(result.occurred_at).toLocaleString("sv-SE")}</span>
                    )}
                    {result.project_id && <span>Projektkopplad</span>}
                  </div>
                </li>
              ))}
            </ul>

            <div className="mt-4 flex items-center justify-between gap-2">
              <button
                type="button"
                onClick={() => run(Math.max(0, offset - PAGE_SIZE))}
                disabled={!hasPrevious || loading}
                className="rounded-lg border border-border px-3 py-2 text-sm text-white/70 disabled:opacity-40"
              >
                ← Föregående
              </button>
              <span className="text-xs text-white/40">
                Sida {Math.floor(offset / PAGE_SIZE) + 1} av {Math.max(1, Math.ceil(total / PAGE_SIZE))}
              </span>
              <button
                type="button"
                onClick={() => run(offset + PAGE_SIZE)}
                disabled={!hasNext || loading}
                className="rounded-lg border border-border px-3 py-2 text-sm text-white/70 disabled:opacity-40"
              >
                Nästa →
              </button>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
