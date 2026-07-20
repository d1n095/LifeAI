"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  api,
  ImportJobItem,
  KnowledgeSourceItem,
  LibrarySearchHit,
  ProjectItem,
} from "@/lib/api";

const CLASSIFICATIONS = ["vision", "architecture", "decisions", "history", "security", "general"];
const TRUTH_STATUSES = ["active", "historical", "proposed", "superseded", "disputed"];

const STATUS_LABELS: Record<string, string> = {
  active: "Aktiv",
  historical: "Historisk",
  proposed: "Förslag",
  superseded: "Ersatt",
  disputed: "Omtvistad",
};

const STATUS_COLORS: Record<string, string> = {
  active: "bg-emerald-500/20 text-emerald-300",
  historical: "bg-white/10 text-white/50",
  proposed: "bg-amber-500/20 text-amber-300",
  superseded: "bg-orange-500/20 text-orange-300",
  disputed: "bg-red-500/20 text-red-300",
};

export default function LibraryPage() {
  const [sources, setSources] = useState<KnowledgeSourceItem[]>([]);
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const [uploading, setUploading] = useState(false);
  const [activeJob, setActiveJob] = useState<ImportJobItem | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const [classification, setClassification] = useState("");
  const [truthStatus, setTruthStatus] = useState("");
  const [projectId, setProjectId] = useState("");

  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<LibrarySearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);

  async function refresh() {
    try {
      setError(null);
      const filters = {
        classification: classification || undefined,
        active_truth_status: truthStatus || undefined,
        project_id: projectId || undefined,
      };
      setSources(await api.listLibrary(filters));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // See identical justification in app/(shell)/documents/page.tsx — documented
    // fetch-on-mount pattern, setState happens after an await.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    api.listProjects().then(setProjects).catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [classification, truthStatus, projectId]);

  async function pollJob(jobId: string) {
    for (let i = 0; i < 120; i++) {
      let job: ImportJobItem;
      try {
        job = await api.getImportJob(jobId);
      } catch (e: any) {
        setError(e.message);
        return;
      }
      setActiveJob(job);
      if (job.status !== "pending" && job.status !== "running") {
        if (job.status === "failed") {
          setError(job.failure_reason || "Importen misslyckades.");
        }
        await refresh();
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 500));
    }
  }

  async function handleImport(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError(null);
    setActiveJob(null);
    try {
      for (const file of Array.from(files)) {
        const job = await api.importToLibrary(file, projectId || undefined);
        setActiveJob(job);
        await pollJob(job.id);
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    handleImport(e.dataTransfer.files);
  }

  async function handleSearch() {
    if (!searchQuery.trim()) {
      setSearchResults(null);
      return;
    }
    setSearching(true);
    setError(null);
    try {
      const filters = {
        classification: classification || undefined,
        active_truth_status: truthStatus || undefined,
        project_id: projectId || undefined,
      };
      setSearchResults(await api.searchLibrary(searchQuery.trim(), filters));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSearching(false);
    }
  }

  function clearSearch() {
    setSearchQuery("");
    setSearchResults(null);
  }

  const projectName = (id: string | null) => projects.find((p) => p.id === id)?.name;

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Founder Knowledge Studio</h1>
          <p className="text-white/50 text-sm mt-1">Importera, organisera och sök i grundarens kunskapsbibliotek.</p>
        </div>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      {/* Import zone */}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
        className={`rounded-xl border-2 border-dashed p-6 text-center transition-colors ${
          dragOver ? "border-accent bg-accent/10" : "border-border"
        }`}
      >
        <p className="text-sm text-white/60 mb-3">
          Dra och släpp en ZIP eller en enskild fil (PDF, DOCX, TXT, Markdown, JSON, HTML) här
        </p>
        <label className="inline-block rounded-lg bg-accent px-4 py-2 text-sm font-medium cursor-pointer">
          {uploading ? "Importerar…" : "Välj fil att importera"}
          <input
            ref={fileInput}
            type="file"
            accept=".zip,.pdf,.docx,.txt,.md,.markdown,.json,.html,.htm"
            aria-label="Importera till kunskapsbiblioteket"
            className="hidden"
            disabled={uploading}
            onChange={(e) => handleImport(e.target.files)}
          />
        </label>

        {activeJob && (
          <div className="mt-4 text-left mx-auto max-w-md" role="status" aria-live="polite">
            <div className="flex justify-between text-xs text-white/50 mb-1">
              <span>{activeJob.source_filename}</span>
              <span>{activeJob.status}</span>
            </div>
            <div className="h-2 rounded-full bg-white/10 overflow-hidden">
              <div
                className="h-full bg-accent transition-all"
                style={{
                  width:
                    activeJob.progress_total > 0
                      ? `${Math.round((activeJob.progress_current / activeJob.progress_total) * 100)}%`
                      : activeJob.status === "completed"
                        ? "100%"
                        : "10%",
                }}
              />
            </div>
            {activeJob.status !== "pending" && activeJob.status !== "running" && (
              <p className="text-xs text-white/40 mt-1">
                {activeJob.succeeded_count} importerade, {activeJob.skipped_count} hoppade över/dubbletter,{" "}
                {activeJob.failed_count} misslyckade
              </p>
            )}
          </div>
        )}
      </div>

      {/* Search + filters */}
      <div className="flex flex-col md:flex-row gap-3">
        <div className="flex flex-1 gap-2">
          <label htmlFor="library-search" className="sr-only">
            Sök i kunskapsbiblioteket
          </label>
          <input
            id="library-search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleSearch()}
            placeholder="Sök semantiskt eller på exakt text…"
            className="flex-1 rounded-lg border border-border bg-panel px-4 py-2 text-sm outline-none focus:border-accent"
          />
          <button onClick={handleSearch} className="rounded-lg bg-accent px-4 py-2 text-sm font-medium">
            Sök
          </button>
          {searchResults !== null && (
            <button onClick={clearSearch} className="rounded-lg border border-border px-4 py-2 text-sm">
              Rensa
            </button>
          )}
        </div>

        <div className="flex gap-2 flex-wrap">
          <label className="sr-only" htmlFor="filter-classification">
            Klassificering
          </label>
          <select
            id="filter-classification"
            value={classification}
            onChange={(e) => setClassification(e.target.value)}
            className="rounded-lg border border-border bg-panel px-3 py-2 text-sm"
          >
            <option value="">Alla klassificeringar</option>
            {CLASSIFICATIONS.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>

          <label className="sr-only" htmlFor="filter-status">
            Status
          </label>
          <select
            id="filter-status"
            value={truthStatus}
            onChange={(e) => setTruthStatus(e.target.value)}
            className="rounded-lg border border-border bg-panel px-3 py-2 text-sm"
          >
            <option value="">Alla statusar</option>
            {TRUTH_STATUSES.map((s) => (
              <option key={s} value={s}>
                {STATUS_LABELS[s]}
              </option>
            ))}
          </select>

          <label className="sr-only" htmlFor="filter-project">
            Projekt
          </label>
          <select
            id="filter-project"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            className="rounded-lg border border-border bg-panel px-3 py-2 text-sm"
          >
            <option value="">Alla projekt</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      {searchResults !== null ? (
        <div className="space-y-3">
          <h2 className="text-sm font-medium text-white/70">
            {searching ? "Söker…" : `${searchResults.length} träffar`}
          </h2>
          {searchResults.map((hit, i) => (
            <Link
              key={i}
              href={`/library/${hit.document_id}`}
              className="block rounded-xl border border-border bg-panel p-4 hover:border-accent/50"
            >
              <div className="flex justify-between text-sm gap-2 flex-wrap">
                <span className="font-medium">{hit.title}</span>
                <div className="flex gap-2 items-center shrink-0">
                  {hit.text_match && <span className="text-[10px] rounded bg-accent2/20 text-accent2 px-2 py-0.5">exakt text</span>}
                  <span className={`text-xs rounded px-2 py-0.5 ${STATUS_COLORS[hit.active_truth_status]}`}>
                    {STATUS_LABELS[hit.active_truth_status]}
                  </span>
                  <span className="text-white/40">{hit.score.toFixed(3)}</span>
                </div>
              </div>
              <p className="mt-2 text-sm text-white/60">{hit.text.slice(0, 300)}</p>
            </Link>
          ))}
          {searchResults.length === 0 && !searching && (
            <div className="text-white/30 text-sm">Inga träffar.</div>
          )}
        </div>
      ) : (
        <div className="rounded-xl border border-border overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">Källor i kunskapsbiblioteket</caption>
            <thead className="bg-panel text-white/50 text-xs uppercase">
              <tr>
                <th scope="col" className="text-left px-4 py-3">Titel</th>
                <th scope="col" className="text-left px-4 py-3">Klassificering</th>
                <th scope="col" className="text-left px-4 py-3">Status</th>
                <th scope="col" className="text-left px-4 py-3">Indexering</th>
                <th scope="col" className="text-left px-4 py-3">Projekt</th>
                <th scope="col" className="text-left px-4 py-3">Version</th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td colSpan={6} className="px-4 py-6 text-center text-white/30" role="status">
                    Laddar…
                  </td>
                </tr>
              )}
              {!loading &&
                sources.map((s) => (
                  <tr key={s.id} className="border-t border-border/60 hover:bg-white/5">
                    <td className="px-4 py-3">
                      <Link href={`/library/${s.id}`} className="text-white/90 hover:text-accent underline-offset-2 hover:underline">
                        {s.title}
                      </Link>
                    </td>
                    <td className="px-4 py-3 text-white/50">{s.classification}</td>
                    <td className="px-4 py-3">
                      <span className={`rounded px-2 py-1 text-xs ${STATUS_COLORS[s.active_truth_status]}`}>
                        {STATUS_LABELS[s.active_truth_status]}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-white/50">
                      {s.status} ({s.chunk_count})
                    </td>
                    <td className="px-4 py-3 text-white/50">{projectName(s.project_id) || "—"}</td>
                    <td className="px-4 py-3 text-white/50">v{s.version_number}</td>
                  </tr>
                ))}
              {!loading && sources.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-6 text-center text-white/30">
                    Inget material importerat ännu.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
