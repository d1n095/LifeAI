"use client";

import { useState } from "react";
import { api } from "@/lib/api";

type Hit = { document_id: string; title: string; text: string; score: number };

export default function KnowledgePage() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Hit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function search() {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      setResults(await api.searchKnowledge(query));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">Kunskapsdatabas</h1>
      <p className="text-white/50 text-sm">
        Sök semantiskt i allt indexerat material — dokument, kod och webbplats.
      </p>

      <div className="flex gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder="Sök i kunskapsbiblioteket…"
          className="flex-1 rounded-lg border border-border bg-panel px-4 py-3 text-sm outline-none focus:border-accent"
        />
        <button onClick={search} className="rounded-lg bg-accent px-5 py-3 text-sm font-medium">
          Sök
        </button>
      </div>

      {loading && <div className="text-white/40 text-sm">Söker…</div>}
      {error && <div className="text-red-300 text-sm">{error}</div>}

      <div className="space-y-3">
        {results.map((r, i) => (
          <div key={i} className="rounded-xl border border-border bg-panel p-4">
            <div className="flex justify-between text-sm">
              <span className="font-medium">{r.title}</span>
              <span className="text-white/40">{r.score.toFixed(3)}</span>
            </div>
            <p className="mt-2 text-sm text-white/60">{r.text.slice(0, 400)}</p>
          </div>
        ))}
        {results.length === 0 && !loading && (
          <div className="text-white/30 text-sm">Inga resultat att visa ännu.</div>
        )}
      </div>
    </div>
  );
}
