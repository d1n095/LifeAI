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
    await api.deleteDocument(id);
    await refresh();
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Dokumenthantering</h1>
        <label className="rounded-lg bg-accent px-4 py-2 text-sm font-medium cursor-pointer">
          {uploading ? "Laddar upp…" : "Ladda upp dokument"}
          <input
            ref={fileInput}
            type="file"
            multiple
            className="hidden"
            onChange={(e) => handleUpload(e.target.files)}
          />
        </label>
      </div>

      {error && <div className="text-sm text-red-300">{error}</div>}

      <div className="rounded-xl border border-border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-panel text-white/50 text-xs uppercase">
            <tr>
              <th className="text-left px-4 py-3">Titel</th>
              <th className="text-left px-4 py-3">Källa</th>
              <th className="text-left px-4 py-3">Status</th>
              <th className="text-left px-4 py-3">Chunkar</th>
              <th className="px-4 py-3" />
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
                  <button onClick={() => handleDelete(d.id)} className="text-white/40 hover:text-red-300 text-xs">
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
