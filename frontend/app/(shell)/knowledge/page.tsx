"use client";

import { useState } from "react";
import Link from "next/link";
import { api, LibrarySearchHit } from "@/lib/api";

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

export default function KnowledgePage() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<LibrarySearchHit[] | null>(null);
  const [degradedReason, setDegradedReason] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function search() {
    if (!query.trim()) {
      setResults(null);
      setDegradedReason(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const response = await api.searchLibrary(query.trim());
      setResults(response.results);
      setDegradedReason(
        response.semantic_search_available
          ? null
          : response.degraded_reason || "Semantisk sökning är inte tillgänglig just nu."
      );
    } catch (e: any) {
      setError(e.message);
      setResults([]);
      setDegradedReason(null);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Kunskapsdatabas</h1>
        <p className="mt-1 text-sm text-white/50">
          Sök i allt indexerat material. Varje träff visar sin riktiga källa, klassificering och
          kunskapsstatus — öppna källan för att granska originalet.
        </p>
      </div>

      <div className="flex gap-2">
        <label htmlFor="knowledge-search" className="sr-only">
          Sök i kunskapsbiblioteket
        </label>
        <input
          id="knowledge-search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder="Sök i kunskapsbiblioteket…"
          className="flex-1 rounded-lg border border-border bg-panel px-4 py-3 text-sm outline-none focus:border-accent"
        />
        <button
          type="button"
          onClick={search}
          disabled={loading || !query.trim()}
          className="rounded-lg bg-accent px-5 py-3 text-sm font-medium disabled:opacity-50"
        >
          {loading ? "Söker…" : "Sök"}
        </button>
      </div>

      {degradedReason && (
        <div role="alert" className="rounded-lg border border-amber-500/30 bg-amber-500/10 px-4 py-2 text-xs text-amber-200">
          Semantisk sökning är otillgänglig just nu ({degradedReason}) — visar endast exakta textträffar.
        </div>
      )}
      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      {results !== null && (
        <div className="text-xs text-white/40" role="status" aria-live="polite">
          {loading ? "Söker…" : `${results.length} träffar`}
        </div>
      )}

      <div className="space-y-3">
        {results?.map((result) => (
          <Link
            key={result.document_id}
            href={`/library/${result.document_id}`}
            className="block rounded-xl border border-border bg-panel p-4 hover:border-accent/50"
          >
            <div className="flex flex-wrap items-start justify-between gap-2 text-sm">
              <span className="font-medium text-white/90">{result.title}</span>
              <div className="flex shrink-0 flex-wrap items-center gap-2">
                {result.text_match && (
                  <span className="rounded bg-accent2/20 px-2 py-0.5 text-[10px] text-accent2">exakt text</span>
                )}
                <span className={`rounded px-2 py-0.5 text-xs ${STATUS_COLORS[result.active_truth_status]}`}>
                  {STATUS_LABELS[result.active_truth_status] ?? result.active_truth_status}
                </span>
                <span className="text-xs text-white/40">{result.score.toFixed(3)}</span>
              </div>
            </div>
            <div className="mt-1 text-[11px] text-white/35">
              {result.classification} · {result.media_type || "text"}
            </div>
            <p className="mt-2 text-sm text-white/60">{result.text.slice(0, 400)}</p>
          </Link>
        ))}

        {results === null && !loading && (
          <div className="rounded-xl border border-dashed border-border p-6 text-sm text-white/30">
            Skriv en fråga för att söka i kunskapsbiblioteket.
          </div>
        )}
        {results !== null && results.length === 0 && !loading && (
          <div className="rounded-xl border border-dashed border-border p-6 text-sm text-white/30">
            Inga träffar för den här frågan.
          </div>
        )}
      </div>
    </div>
  );
}
