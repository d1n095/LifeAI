"use client";

import { useEffect, useState } from "react";
import {
  api,
  CANCELLABLE_MAINAI_JOB_STATUSES,
  DocumentItem,
  MainAIJob,
  MainAIJobDetail,
  MainAIJobProposal,
  RETRYABLE_MAINAI_JOB_STATUSES,
} from "@/lib/api";

// MainAI Runtime Truthfulness and Durable Job Foundation (see
// backend/docs/MAINAI_JOB_RUNTIME.md). Deliberately plain, same convention as /admin/agents:
// shows real, persisted job rows and their real progress/status — never a fake progress bar
// animation, only what the server actually reports on the last poll. Cancel is only ever
// shown for a status the API would actually accept (CANCELLABLE_MAINAI_JOB_STATUSES), so the
// button itself can never lie about what's possible.

const STATUS_LABELS: Record<string, string> = {
  queued: "I kö",
  running: "Pågår",
  paused: "Pausad",
  completed: "Klar",
  failed: "Misslyckades",
  cancelled: "Avbruten",
};

const STATUS_COLORS: Record<string, string> = {
  queued: "text-white/50",
  running: "text-blue-300",
  paused: "text-amber-300",
  completed: "text-emerald-300",
  failed: "text-red-300",
  cancelled: "text-white/40",
};

const POLL_INTERVAL_MS = 4000;

export default function MainAIJobsPage() {
  const [scope, setScope] = useState<"mine" | "admin">("mine");
  const [jobs, setJobs] = useState<MainAIJob[]>([]);
  const [selected, setSelected] = useState<MainAIJobDetail | null>(null);
  const [proposals, setProposals] = useState<MainAIJobProposal[]>([]);
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [selectedDocIds, setSelectedDocIds] = useState<string[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function refreshJobs() {
    try {
      setJobs(scope === "admin" ? await api.mainaiJobsAdminAll() : await api.mainaiJobs());
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function openJob(id: string) {
    try {
      const [detail, jobProposals] = await Promise.all([api.mainaiJobDetail(id), api.mainaiJobProposals(id)]);
      setSelected(detail);
      setProposals(jobProposals);
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refreshJobs();
    api
      .listDocuments()
      .then((docs) => setDocuments(docs.filter((d) => d.status === "indexed")))
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope]);

  // Real polling of the server's own state — not a client-side animation. Progress/phase/
  // heartbeat shown below always reflect the last successful poll, nothing interpolated.
  useEffect(() => {
    const interval = setInterval(() => {
      refreshJobs();
      if (selected) openJob(selected.id);
    }, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, selected?.id]);

  function toggleDoc(id: string) {
    setSelectedDocIds((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  async function createJob() {
    if (selectedDocIds.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const job = await api.mainaiCreateJob({
        job_type: "corpus_review",
        input_refs: selectedDocIds.map((id) => ({ type: "document", id })),
      });
      setSelectedDocIds([]);
      await refreshJobs();
      await openJob(job.id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function cancelJob(id: string) {
    setBusy(true);
    setError(null);
    try {
      await api.mainaiCancelJob(id);
      await refreshJobs();
      if (selected?.id === id) await openJob(id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function retryJob(id: string) {
    setBusy(true);
    setError(null);
    try {
      await api.mainaiRetryJob(id);
      await refreshJobs();
      if (selected?.id === id) await openJob(id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-8">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Jobb &amp; Aktivitet</h1>
          <p className="text-sm text-white/50 mt-1">
            Riktiga, varaktiga jobb — inte MainAI:s egna påståenden om vad den gör. Varje rad här motsvarar en rad i databasen som
            MainAI inte kan låtsas om. Ingen animerad låtsas-förloppsindikator: siffrorna nedan är exakt vad servern senast
            rapporterade.
          </p>
        </div>
        <div className="flex gap-1 rounded-lg border border-border p-0.5 text-xs shrink-0">
          <button
            type="button"
            onClick={() => setScope("mine")}
            className={`rounded px-3 py-1.5 ${scope === "mine" ? "bg-white/10" : "text-white/50"}`}
          >
            Mina jobb
          </button>
          <button
            type="button"
            onClick={() => setScope("admin")}
            className={`rounded px-3 py-1.5 ${scope === "admin" ? "bg-white/10" : "text-white/50"}`}
            title="Adminvy — visar jobb från alla ägare, separat från den normala vyn."
          >
            Alla ägare (admin)
          </button>
        </div>
      </div>

      {error && (
        <div role="alert" className="text-sm text-red-300">
          {error}
        </div>
      )}

      <section className="grid gap-6 md:grid-cols-2">
        <div className="space-y-4">
          {scope === "mine" && (
            <div className="rounded-xl border border-border p-4 space-y-2">
              <div className="text-xs uppercase tracking-wide text-white/40">Nytt granskningsjobb (corpus_review)</div>
              <p className="text-xs text-white/40">Välj indexerade dokument att granska. MainAI läser dem — den skriver aldrig om dem.</p>
              <ul className="max-h-40 overflow-y-auto space-y-1">
                {documents.map((d) => (
                  <li key={d.id}>
                    <label className="flex items-center gap-2 text-sm">
                      <input type="checkbox" checked={selectedDocIds.includes(d.id)} onChange={() => toggleDoc(d.id)} />
                      {d.title}
                    </label>
                  </li>
                ))}
                {documents.length === 0 && <li className="text-xs text-white/30">Inga indexerade dokument ännu.</li>}
              </ul>
              <button
                type="button"
                disabled={busy || selectedDocIds.length === 0}
                onClick={createJob}
                className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
              >
                Starta granskning ({selectedDocIds.length} dokument)
              </button>
            </div>
          )}

          <div className="rounded-xl border border-border overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-panel text-white/50 text-xs uppercase">
                <tr>
                  <th scope="col" className="text-left px-4 py-3">
                    Typ
                  </th>
                  <th scope="col" className="text-left px-4 py-3">
                    Status
                  </th>
                  <th scope="col" className="text-left px-4 py-3">
                    Förlopp
                  </th>
                  <th scope="col" className="text-left px-4 py-3">
                    Startad
                  </th>
                </tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr
                    key={j.id}
                    onClick={() => openJob(j.id)}
                    className={`border-t border-border/60 cursor-pointer hover:bg-white/5 ${selected?.id === j.id ? "bg-white/5" : ""}`}
                  >
                    <td className="px-4 py-3">{j.job_type}</td>
                    <td className={`px-4 py-3 ${STATUS_COLORS[j.status] ?? ""}`}>{STATUS_LABELS[j.status] ?? j.status}</td>
                    <td className="px-4 py-3 text-white/50">
                      {j.progress_total ? `${j.progress_current}/${j.progress_total}` : j.progress_current}
                    </td>
                    <td className="px-4 py-3 text-white/40">{j.started_at ? new Date(j.started_at).toLocaleString("sv-SE") : "—"}</td>
                  </tr>
                ))}
                {jobs.length === 0 && (
                  <tr>
                    <td colSpan={4} className="px-4 py-6 text-center text-white/30">
                      Inga jobb ännu.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="space-y-3">
          {!selected && <p className="text-sm text-white/40">Välj ett jobb i listan för att se detaljer.</p>}
          {selected && (
            <div className="rounded-xl border border-border p-4 space-y-4">
              <div>
                <div className="flex items-center justify-between">
                  <div className="text-sm font-medium">{selected.job_type}</div>
                  <div className={`text-xs ${STATUS_COLORS[selected.status] ?? ""}`}>{STATUS_LABELS[selected.status] ?? selected.status}</div>
                </div>
                <div className="text-xs text-white/40 mt-1 space-y-0.5">
                  <div>Skapad: {new Date(selected.created_at).toLocaleString("sv-SE")}</div>
                  {selected.started_at && <div>Startad: {new Date(selected.started_at).toLocaleString("sv-SE")}</div>}
                  {selected.last_heartbeat_at && <div>Senaste heartbeat: {new Date(selected.last_heartbeat_at).toLocaleString("sv-SE")}</div>}
                  {selected.completed_at && <div>Avslutad: {new Date(selected.completed_at).toLocaleString("sv-SE")}</div>}
                  {selected.current_phase && <div>Fas: {selected.current_phase}</div>}
                  <div>
                    Förlopp: {selected.progress_current}
                    {selected.progress_total ? ` / ${selected.progress_total}` : ""}
                  </div>
                  {selected.provider && (
                    <div>
                      Utförare: {selected.provider} / {selected.model}
                    </div>
                  )}
                </div>

                {selected.status === "failed" && (
                  <div className="mt-3 rounded-lg border border-red-500/30 bg-red-500/5 p-3 text-xs text-red-300">
                    <div className="font-medium">Misslyckades ({selected.error_category ?? "okänd kategori"})</div>
                    <div className="mt-1 text-red-300/80">{selected.public_message}</div>
                  </div>
                )}
                {selected.status === "cancelled" && (
                  <div className="mt-3 rounded-lg border border-border p-3 text-xs text-white/50">{selected.public_message}</div>
                )}

                <div className="flex flex-wrap gap-2 mt-3">
                  {CANCELLABLE_MAINAI_JOB_STATUSES.includes(selected.status) && (
                    <button
                      type="button"
                      disabled={busy || selected.cancel_requested}
                      onClick={() => cancelJob(selected.id)}
                      className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
                    >
                      {selected.cancel_requested ? "Avbrytning begärd…" : "Avbryt"}
                    </button>
                  )}
                  {RETRYABLE_MAINAI_JOB_STATUSES.includes(selected.status) && selected.retry_count < selected.max_retries && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => retryJob(selected.id)}
                      className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
                    >
                      Försök igen ({selected.retry_count}/{selected.max_retries})
                    </button>
                  )}
                </div>
              </div>

              {proposals.length > 0 && (
                <div className="space-y-2">
                  <div className="text-xs uppercase tracking-wide text-white/40">
                    Förslag ({proposals.length}) — inte godkänd kunskap, bara MainAI:s tolkning
                  </div>
                  <ul className="space-y-2 max-h-64 overflow-y-auto">
                    {proposals.map((p) => (
                      <li key={p.id} className="rounded-lg border border-border/60 p-3 text-xs">
                        <div className="text-white/40">{p.status === "proposed" ? "Föreslaget" : "Avfärdat"}</div>
                        <div className="text-white/70 mt-1 whitespace-pre-wrap">{p.proposal_text}</div>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              <div className="space-y-2">
                <div className="text-xs uppercase tracking-wide text-white/40">Händelsehistorik</div>
                <ul className="space-y-2 max-h-64 overflow-y-auto">
                  {selected.events.map((ev) => (
                    <li key={ev.id} className="rounded-lg border border-border/60 p-3 text-xs">
                      <div className="flex justify-between text-white/50">
                        <span>{ev.event_type}</span>
                        <span>{new Date(ev.created_at).toLocaleString("sv-SE")}</span>
                      </div>
                    </li>
                  ))}
                  {selected.events.length === 0 && <li className="text-white/30">Inga händelser ännu.</li>}
                </ul>
              </div>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
