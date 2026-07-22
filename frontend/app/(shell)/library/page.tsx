"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { api, KnowledgeSourceItem, LibrarySearchHit, ProjectItem } from "@/lib/api";
import { useUploadQueue, UploadQueueItem } from "@/lib/uploadQueue";
import { useTwoStepDelete } from "@/lib/useTwoStepDelete";

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

const QUEUE_STATUS_LABELS: Record<UploadQueueItem["status"], string> = {
  queued: "I kö",
  uploading: "Laddar upp…",
  received: "Mottagen…",
  processing: "Bearbetar…",
  completed: "Klar",
  failed: "Misslyckades",
  cancelled: "Avbruten",
};

export default function LibraryPage() {
  const [sources, setSources] = useState<KnowledgeSourceItem[]>([]);
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const { items: queueItems, enqueue, retry, cancel, dismiss } = useUploadQueue();
  const [dragOver, setDragOver] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const refreshedForRef = useRef(new Set<string>());

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

  // A queue item reaching "completed" means a real server job finished — refresh the source
  // list so the newly indexed document shows up without the founder having to reload.
  // Tracked per queue-item id so the same completion doesn't trigger a refetch repeatedly.
  useEffect(() => {
    for (const item of queueItems) {
      if (item.status === "completed" && !refreshedForRef.current.has(item.id)) {
        refreshedForRef.current.add(item.id);
        refresh();
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queueItems]);

  function handleFilesChosen(files: FileList | null) {
    if (!files || files.length === 0) return;
    enqueue(files, projectId || undefined);
    if (fileInput.current) fileInput.current.value = "";
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    handleFilesChosen(e.dataTransfer.files);
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

  function handleSourceDeleted(id: string) {
    setSources((prev) => prev.filter((s) => s.id !== id));
  }

  const projectName = (id: string | null) => projects.find((p) => p.id === id)?.name;

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Life Library</h1>
          <p className="text-white/50 text-sm mt-1">Den enda inläsningscentralen: importera, organisera och sök i grundarens kunskapsbibliotek.</p>
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
          Dra och släpp en eller flera filer (ZIP, PDF, DOCX, TXT, Markdown, JSON, HTML, MP3, MP4) här
        </p>
        <label className="inline-block rounded-lg bg-accent px-4 py-2 text-sm font-medium cursor-pointer">
          Välj filer att importera
          <input
            ref={fileInput}
            type="file"
            multiple
            accept=".zip,.pdf,.docx,.txt,.md,.markdown,.json,.html,.htm,.mp3,.mp4"
            aria-label="Importera till kunskapsbiblioteket"
            className="hidden"
            onChange={(e) => handleFilesChosen(e.target.files)}
          />
        </label>
        <p className="text-xs text-white/30 mt-2">Du kan lägga till fler filer medan andra fortfarande bearbetas.</p>

        {queueItems.length > 0 && (
          <ul className="mt-4 text-left mx-auto max-w-lg space-y-2" aria-label="Uppladdningskö" aria-live="polite">
            {queueItems.map((item) => (
              <li key={item.id} className="rounded-lg border border-border bg-panel px-3 py-2 text-xs">
                <div className="flex items-center justify-between gap-2">
                  <span className="truncate text-white/80">{item.filename}</span>
                  <span className="shrink-0 text-white/50">{QUEUE_STATUS_LABELS[item.status]}</span>
                </div>
                {item.status === "failed" && (
                  <div className="mt-1 flex items-center justify-between gap-2">
                    <span role="alert" className="text-red-300">
                      {item.error || "Okänt fel."}
                    </span>
                    {!item.recovered && (
                      <button
                        type="button"
                        onClick={() => retry(item.id)}
                        className="shrink-0 rounded border border-border px-2 py-0.5 text-white/70 hover:text-white"
                      >
                        Försök igen
                      </button>
                    )}
                  </div>
                )}
                {item.status === "queued" && (
                  <div className="mt-1 flex justify-end">
                    <button
                      type="button"
                      onClick={() => cancel(item.id)}
                      className="rounded border border-border px-2 py-0.5 text-white/50 hover:text-white"
                    >
                      Avbryt
                    </button>
                  </div>
                )}
                {(item.status === "completed" || item.status === "cancelled") && (
                  <div className="mt-1 flex justify-end">
                    <button
                      type="button"
                      onClick={() => dismiss(item.id)}
                      className="rounded border border-border px-2 py-0.5 text-white/40 hover:text-white"
                    >
                      Dölj
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
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
        // relative: without a positioned ancestor here, the "Åtgärder" column header's
        // visually-hidden (absolutely positioned) span below escapes this scroll
        // container's clipping entirely and inflates document.documentElement.scrollWidth
        // at narrow viewports, even though nothing is actually visible outside the
        // viewport — found via a real mobile-viewport overflow check, not asserted.
        <div className="relative rounded-xl border border-border overflow-x-auto">
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
                <th scope="col" className="text-left px-4 py-3">
                  <span className="sr-only">Åtgärder</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {loading && (
                <tr>
                  <td colSpan={7} className="px-4 py-6 text-center text-white/30" role="status">
                    Laddar…
                  </td>
                </tr>
              )}
              {!loading &&
                sources.map((s) => (
                  <SourceRow key={s.id} source={s} projectName={projectName(s.project_id)} onDeleted={handleSourceDeleted} />
                ))}
              {!loading && sources.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-6 text-center text-white/30">
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

function SourceRow({
  source,
  projectName,
  onDeleted,
}: {
  source: KnowledgeSourceItem;
  projectName: string | undefined;
  onDeleted: (id: string) => void;
}) {
  const { confirming, deleting, error, requestConfirm, cancelConfirm, confirmDelete } = useTwoStepDelete(async () => {
    await api.deleteLibrarySource(source.id);
    onDeleted(source.id);
  });

  return (
    <tr className="border-t border-border/60 hover:bg-white/5">
      <td className="px-4 py-3">
        <Link href={`/library/${source.id}`} className="text-white/90 hover:text-accent underline-offset-2 hover:underline">
          {source.title}
        </Link>
      </td>
      <td className="px-4 py-3 text-white/50">{source.classification}</td>
      <td className="px-4 py-3">
        <span className={`rounded px-2 py-1 text-xs ${STATUS_COLORS[source.active_truth_status]}`}>
          {STATUS_LABELS[source.active_truth_status]}
        </span>
      </td>
      <td className="px-4 py-3 text-white/50">
        {source.status} ({source.chunk_count})
      </td>
      <td className="px-4 py-3 text-white/50">{projectName || "—"}</td>
      <td className="px-4 py-3 text-white/50">v{source.version_number}</td>
      <td className="px-4 py-3">
        {error && (
          <div role="alert" className="mb-1 text-xs text-red-300">
            {error}
          </div>
        )}
        {!confirming ? (
          <button
            type="button"
            onClick={requestConfirm}
            aria-label={`Ta bort ${source.title}`}
            className="rounded border border-red-500/40 px-2 py-1 text-xs text-red-300 hover:bg-red-500/10"
          >
            Ta bort
          </button>
        ) : (
          <div className="flex gap-1">
            <button
              type="button"
              onClick={confirmDelete}
              disabled={deleting}
              className="rounded bg-red-500/80 px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
            >
              {deleting ? "Tar bort…" : "Bekräfta"}
            </button>
            <button
              type="button"
              onClick={cancelConfirm}
              disabled={deleting}
              className="rounded border border-border px-2 py-1 text-xs disabled:opacity-50"
            >
              Avbryt
            </button>
          </div>
        )}
      </td>
    </tr>
  );
}
