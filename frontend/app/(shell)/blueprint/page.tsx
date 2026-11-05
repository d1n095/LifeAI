"use client";

import { useCallback, useEffect, useState } from "react";
import { api, BlueprintDraft, BlueprintDraftSummary } from "@/lib/api";

// Founder Alpha item 8/9 — the Founder requests, inspects, regenerates and explicitly approves
// source-grounded Blueprint drafts. Sources are derived server-side from the founder's own
// indexed documents (backend/app/routers/blueprint.py); this page never supplies source ids.

function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : "Något gick fel.";
}

function statusBadge(status: string): string {
  return status === "approved"
    ? "bg-emerald-500/20 text-emerald-300"
    : "bg-amber-500/20 text-amber-300";
}

export default function BlueprintPage() {
  const [drafts, setDrafts] = useState<BlueprintDraftSummary[]>([]);
  const [selected, setSelected] = useState<BlueprintDraft | null>(null);
  const [title, setTitle] = useState("");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setDrafts(await api.blueprintDrafts());
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // Initial load. Done inline (not via refresh()) and await-first — no setState runs
  // synchronously in the effect body (react-hooks/set-state-in-effect); `loading` starts true.
  useEffect(() => {
    let active = true;
    (async () => {
      try {
        const d = await api.blueprintDrafts();
        if (active) {
          setDrafts(d);
          setError(null);
        }
      } catch (e) {
        if (active) setError(errorMessage(e));
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  async function open(id: string) {
    setError(null);
    setNotice(null);
    try {
      setSelected(await api.blueprintDraft(id));
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  async function generate(e: React.FormEvent) {
    e.preventDefault();
    if (!title.trim()) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const draft = await api.blueprintGenerate({ title: title.trim(), query: query.trim() || undefined });
      setSelected(draft);
      setNotice(`Utkast skapat (version ${draft.version}).`);
      setTitle("");
      setQuery("");
      await refresh();
    } catch (e) {
      // 422 = no indexed source matched -> the backend refuses an ungrounded draft.
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function regenerate() {
    if (!selected) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const draft = await api.blueprintRegenerate(selected.id, { query: query.trim() || undefined });
      setSelected(draft);
      setNotice(`Ny version skapad (version ${draft.version}).`);
      await refresh();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  async function approve() {
    if (!selected) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const draft = await api.blueprintApprove(selected.id);
      setSelected(draft);
      setNotice("Utkastet är godkänt.");
      await refresh();
    } catch (e) {
      // 409 = already approved / not in draft status.
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-6 p-4">
      <header>
        <h1 className="text-xl font-semibold text-white">Blueprint</h1>
        <p className="mt-1 text-sm text-white/60">
          Life sammanställer ett <strong>utkast</strong> till projektets Blueprint, grundat i dina
          egna indexerade källor. Ett utkast godkänns aldrig automatiskt — du granskar och godkänner
          det själv.
        </p>
      </header>

      {error && (
        <div className="rounded-lg bg-red-500/15 px-4 py-3 text-sm text-red-200" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="rounded-lg bg-emerald-500/15 px-4 py-3 text-sm text-emerald-200" role="status">
          {notice}
        </div>
      )}

      <form onSubmit={generate} className="flex flex-col gap-3 rounded-xl bg-white/5 p-4">
        <label className="flex flex-col gap-1 text-sm text-white/70">
          Titel
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="t.ex. Projektets Blueprint v1"
            className="rounded-lg bg-black/30 px-3 py-2 text-white outline-none ring-1 ring-white/10 focus:ring-white/30"
            required
          />
        </label>
        <label className="flex flex-col gap-1 text-sm text-white/70">
          Fokus / fråga (valfritt)
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="t.ex. tekniska beslut och historik"
            className="rounded-lg bg-black/30 px-3 py-2 text-white outline-none ring-1 ring-white/10 focus:ring-white/30"
          />
        </label>
        <button
          type="submit"
          disabled={busy || !title.trim()}
          className="self-start rounded-lg bg-white/90 px-4 py-2 text-sm font-medium text-black disabled:opacity-40"
        >
          {busy ? "Genererar…" : "Generera utkast"}
        </button>
      </form>

      <div className="grid gap-6 md:grid-cols-[18rem_1fr]">
        <aside className="flex flex-col gap-2">
          <h2 className="text-sm font-medium text-white/70">Utkast</h2>
          {loading && <p className="text-sm text-white/40">Laddar…</p>}
          {!loading && drafts.length === 0 && <p className="text-sm text-white/40">Inga utkast ännu.</p>}
          <ul className="flex flex-col gap-1">
            {drafts.map((d) => (
              <li key={d.id}>
                <button
                  onClick={() => open(d.id)}
                  className={`flex w-full flex-col rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                    selected?.id === d.id ? "bg-white/15 text-white" : "bg-white/5 text-white/80 hover:bg-white/10"
                  }`}
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate">{d.title}</span>
                    <span className="shrink-0 text-xs text-white/40">v{d.version}</span>
                  </span>
                  <span className="mt-1 flex items-center gap-2">
                    <span className={`rounded px-1.5 py-0.5 text-[11px] ${statusBadge(d.status)}`}>
                      {d.status === "approved" ? "Godkänd" : "Utkast"}
                    </span>
                    <span className="text-[11px] text-white/40">{d.source_count} källor</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        <section className="min-w-0">
          {!selected && <p className="text-sm text-white/40">Välj ett utkast för att granska det.</p>}
          {selected && (
            <article className="flex flex-col gap-4">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-lg font-semibold text-white">{selected.title}</h2>
                <span className="text-sm text-white/40">version {selected.version}</span>
                <span className={`rounded px-2 py-0.5 text-xs ${statusBadge(selected.status)}`}>
                  {selected.status === "approved" ? "Godkänd" : "Utkast — väntar på godkännande"}
                </span>
              </div>

              <div className="flex flex-wrap gap-2">
                <button
                  onClick={regenerate}
                  disabled={busy}
                  className="rounded-lg bg-white/10 px-3 py-1.5 text-sm text-white hover:bg-white/20 disabled:opacity-40"
                >
                  Regenerera (ny version)
                </button>
                <button
                  onClick={approve}
                  disabled={busy || selected.status === "approved"}
                  className="rounded-lg bg-emerald-500/80 px-3 py-1.5 text-sm font-medium text-black hover:bg-emerald-400 disabled:opacity-40"
                >
                  {selected.status === "approved" ? "Godkänd" : "Godkänn utkast"}
                </button>
              </div>

              <div className="rounded-xl bg-white/5 p-4">
                <h3 className="mb-2 text-sm font-medium text-white/70">Utkast</h3>
                <pre className="whitespace-pre-wrap break-words font-sans text-sm text-white/90">{selected.body}</pre>
              </div>

              <div className="rounded-xl bg-white/5 p-4">
                <h3 className="mb-2 text-sm font-medium text-white/70">
                  Källor ({selected.source_refs.length}) — det här utkastet är en AI-tolkning, grundad i
                  dina original (originalen ändras aldrig)
                </h3>
                <ul className="flex flex-col gap-2">
                  {selected.source_refs.map((ref, i) => (
                    <li key={`${ref.document_id}-${i}`} className="rounded-lg bg-black/20 px-3 py-2 text-sm">
                      <div className="flex items-center justify-between gap-2">
                        <code className="truncate text-xs text-white/60">{ref.document_id}</code>
                        {ref.similarity != null && (
                          <span className="shrink-0 text-xs text-white/40">
                            likhet {ref.similarity.toFixed(2)}
                          </span>
                        )}
                      </div>
                      {ref.excerpt && <p className="mt-1 text-white/70">{ref.excerpt}</p>}
                    </li>
                  ))}
                </ul>
              </div>

              {selected.status === "approved" && selected.approved_at && (
                <p className="text-xs text-white/40">Godkänd {new Date(selected.approved_at).toLocaleString("sv-SE")}.</p>
              )}
            </article>
          )}
        </section>
      </div>
    </div>
  );
}
