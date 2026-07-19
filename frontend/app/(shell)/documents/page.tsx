"use client";

import { useEffect, useRef, useState } from "react";
import { api, DocumentItem } from "@/lib/api";

export default function DocumentsPage() {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  async function refresh() {
    try {
      setDocuments(await api.listDocuments());
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    // react-hooks/set-state-in-effect fires here because it can't statically see that
    // refresh()'s setState calls happen after an await, not synchronously — this is React's
    // own documented "fetch data on mount" pattern (https://react.dev/learn/synchronizing-with-effects),
    // not the derived-state anti-pattern the rule targets. Covered by the Playwright E2E suite.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
  }, []);

  async function handleUpload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      for (const file of Array.from(files)) {
        await api.uploadDocument(file);
      }
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function handleDelete(id: string) {
    // Was previously un-caught: a failed delete (network error, session expired, ...) left
    // the row exactly where it was with zero feedback — indistinguishable from the click not
    // registering at all. Same error state and role="alert" surface already used above for
    // refresh()/handleUpload() failures.
    try {
      await api.deleteDocument(id);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <h1 className="text-xl font-semibold">Dokumenthantering</h1>
        <label className="rounded-lg bg-accent px-4 py-2 text-sm font-medium cursor-pointer text-center">
          {uploading ? "Laddar upp…" : "Ladda upp dokument"}
          <input
            ref={fileInput}
            type="file"
            multiple
            aria-label="Ladda upp dokument"
            className="hidden"
            onChange={(e) => handleUpload(e.target.files)}
          />
        </label>
      </div>

      {error && (
        <div role="alert" className="text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="rounded-xl border border-border overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">Uppladdade dokument och deras indexeringsstatus</caption>
          <thead className="bg-panel text-white/50 text-xs uppercase">
            <tr>
              <th scope="col" className="text-left px-4 py-3">
                Titel
              </th>
              <th scope="col" className="text-left px-4 py-3">
                Källa
              </th>
              <th scope="col" className="text-left px-4 py-3">
                Status
              </th>
              <th scope="col" className="text-left px-4 py-3">
                Chunkar
              </th>
              <th scope="col" className="px-4 py-3">
                <span className="sr-only">Åtgärder</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {documents.map((d) => (
              <tr key={d.id} className="border-t border-border/60">
                <td className="px-4 py-3">{d.title}</td>
                <td className="px-4 py-3 text-white/50">{d.source}</td>
                <td className="px-4 py-3">
                  <StatusBadge status={d.status} />
                </td>
                <td className="px-4 py-3 text-white/50">{d.chunk_count}</td>
                <td className="px-4 py-3 text-right">
                  <button
                    onClick={() => handleDelete(d.id)}
                    aria-label={`Ta bort dokumentet ${d.title}`}
                    className="text-white/40 hover:text-red-300 text-xs"
                  >
                    Ta bort
                  </button>
                </td>
              </tr>
            ))}
            {documents.length === 0 && (
              <tr>
                <td colSpan={5} className="px-4 py-6 text-center text-white/30">
                  Inga dokument uppladdade ännu.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    indexed: "bg-emerald-500/20 text-emerald-300",
    indexing: "bg-amber-500/20 text-amber-300",
    pending: "bg-white/10 text-white/50",
    failed: "bg-red-500/20 text-red-300",
  };
  return <span className={`rounded px-2 py-1 text-xs ${colors[status] || ""}`}>{status}</span>;
}
