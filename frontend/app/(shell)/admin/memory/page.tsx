"use client";

import { useEffect, useState } from "react";
import {
  api,
  ProjectBranchPRStatus,
  ProjectCheckpointDetail,
  ProjectConflicts,
  ProjectNote,
  ProjectNoteKind,
} from "@/lib/api";

// Fas 4: minimal founder-only view onto the MainAI Project Memory & Coordination Loop
// (see backend/app/project_memory.py). Deliberately plain — the point of this screen is
// that a founder (or a new agent session) can see current project state, blockers,
// decisions, and the exact next safe step at a glance, not that it looks polished.

const KIND_LABELS: Record<ProjectNoteKind, string> = {
  fact: "Fakta",
  decision: "Beslut",
  blocker: "Blockerare",
  next_step: "Nästa steg",
  uncertainty: "Osäkerhet",
  idea: "Idé (ej beslutad)",
};

export default function ProjectMemoryPage() {
  const [checkpoint, setCheckpoint] = useState<ProjectCheckpointDetail | null>(null);
  const [checkpointMissing, setCheckpointMissing] = useState(false);
  const [stale, setStale] = useState<{ stale: boolean; reasons: string[] } | null>(null);
  const [notes, setNotes] = useState<ProjectNote[]>([]);
  const [branchPrStatus, setBranchPrStatus] = useState<ProjectBranchPRStatus[]>([]);
  const [conflicts, setConflicts] = useState<ProjectConflicts | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [showBrief, setShowBrief] = useState(false);

  const [summary, setSummary] = useState("");
  const [branchName, setBranchName] = useState("");
  const [prRefs, setPrRefs] = useState("");

  async function refresh() {
    setError(null);
    try {
      const [n, b, c] = await Promise.all([api.memoryNotes("open"), api.memoryBranchPrStatus(), api.memoryConflicts()]);
      setNotes(n);
      setBranchPrStatus(b);
      setConflicts(c);
    } catch (e: any) {
      setError(e.message);
    }
    try {
      const cp = await api.memoryLatestCheckpoint();
      setCheckpoint(cp);
      setCheckpointMissing(false);
      const s = await api.memoryCheckpointStale(cp.id);
      setStale(s);
    } catch (e: any) {
      if (e.status === 404) {
        setCheckpoint(null);
        setCheckpointMissing(true);
      } else {
        setError(e.message);
      }
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
  }, []);

  async function createCheckpoint() {
    if (!summary.trim() || !branchName.trim()) return;
    setCreating(true);
    setError(null);
    try {
      const refs = prRefs
        .split(",")
        .map((r) => r.trim())
        .filter(Boolean);
      await api.memoryCreateCheckpoint({ summary, branch_name: branchName, open_pr_refs: refs });
      setSummary("");
      setBranchName("");
      setPrRefs("");
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setCreating(false);
    }
  }

  const notesByKind = (kind: ProjectNoteKind) => notes.filter((n) => n.kind === kind);
  const hasConflicts =
    conflicts && (conflicts.duplicate_work_candidates.length > 0 || conflicts.data_integrity_issues.length > 0);

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-xl font-semibold">Projektminne</h1>
        <p className="text-sm text-white/50 mt-1">
          MainAI Project Memory &amp; Coordination Loop — projektets eget varaktiga minne av tillstånd, beslut,
          blockerare och nästa steg. Lagrar och hämtar; fattar aldrig beslut själv.
        </p>
      </div>

      {error && (
        <div role="alert" className="text-sm text-red-300">
          {error}
        </div>
      )}

      <section aria-labelledby="checkpoint-heading" className="space-y-3">
        <h2 id="checkpoint-heading" className="text-sm font-medium text-white/70">
          Senaste checkpoint
        </h2>

        {checkpointMissing && <p className="text-sm text-white/40">Ingen checkpoint finns ännu.</p>}

        {checkpoint && (
          <div className="rounded-xl border border-border p-4 space-y-2">
            <div className="flex items-start justify-between gap-4">
              <div>
                <div className="text-sm text-white">{checkpoint.summary}</div>
                <div className="text-xs text-white/40 mt-1">
                  Branch: <span className="text-white/60">{checkpoint.branch_name}</span>
                  {checkpoint.open_pr_refs && (
                    <>
                      {" · "}Öppna PR:er: <span className="text-white/60">{checkpoint.open_pr_refs}</span>
                    </>
                  )}
                  {" · "}
                  {new Date(checkpoint.created_at).toLocaleString("sv-SE")} av {checkpoint.created_by}
                </div>
              </div>
              {stale && (
                <span
                  className={`shrink-0 rounded px-2 py-1 text-xs whitespace-nowrap ${
                    stale.stale ? "bg-amber-500/20 text-amber-300" : "bg-emerald-500/20 text-emerald-300"
                  }`}
                  title={stale.reasons.join("; ")}
                >
                  {stale.stale ? "Inaktuell" : "Aktuell"}
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={() => setShowBrief((v) => !v)}
              className="text-xs rounded border border-border px-2 py-1"
            >
              {showBrief ? "Dölj återupptagningsbrief" : "Visa återupptagningsbrief"}
            </button>
            {showBrief && (
              <pre className="whitespace-pre-wrap text-xs text-white/70 bg-panel rounded-lg p-4 max-h-96 overflow-y-auto">
                {checkpoint.brief}
              </pre>
            )}
          </div>
        )}

        <div className="rounded-xl border border-border p-4 space-y-2">
          <div className="text-xs uppercase tracking-wide text-white/40">Skapa ny checkpoint</div>
          <textarea
            value={summary}
            onChange={(e) => setSummary(e.target.value)}
            placeholder="Sammanfattning av nuläget…"
            rows={2}
            className="w-full rounded-lg bg-panel border border-border px-3 py-2 text-sm"
          />
          <div className="flex flex-wrap gap-2">
            <input
              value={branchName}
              onChange={(e) => setBranchName(e.target.value)}
              placeholder="Branch-namn"
              className="flex-1 min-w-[10rem] rounded-lg bg-panel border border-border px-3 py-2 text-sm"
            />
            <input
              value={prRefs}
              onChange={(e) => setPrRefs(e.target.value)}
              placeholder="Öppna PR:er (kommaseparerat, t.ex. 13, 15)"
              className="flex-1 min-w-[10rem] rounded-lg bg-panel border border-border px-3 py-2 text-sm"
            />
          </div>
          <button
            type="button"
            disabled={creating || !summary.trim() || !branchName.trim()}
            onClick={createCheckpoint}
            className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
          >
            {creating ? "Skapar…" : "Skapa checkpoint"}
          </button>
        </div>
      </section>

      <section aria-labelledby="branches-heading" className="space-y-3">
        <h2 id="branches-heading" className="text-sm font-medium text-white/70">
          Brancher &amp; PR:er
        </h2>
        <div className="rounded-xl border border-border overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="bg-panel text-white/50 text-xs uppercase">
              <tr>
                <th scope="col" className="text-left px-4 py-3">
                  Typ
                </th>
                <th scope="col" className="text-left px-4 py-3">
                  Referens
                </th>
                <th scope="col" className="text-left px-4 py-3">
                  Status
                </th>
                <th scope="col" className="text-left px-4 py-3">
                  Bas → Head
                </th>
                <th scope="col" className="text-left px-4 py-3">
                  CI
                </th>
              </tr>
            </thead>
            <tbody>
              {branchPrStatus.map((row) => (
                <tr key={row.id} className="border-t border-border/60">
                  <td className="px-4 py-3 text-white/50">{row.kind}</td>
                  <td className="px-4 py-3">{row.title ? `${row.ref} — ${row.title}` : row.ref}</td>
                  <td className="px-4 py-3">{row.status}</td>
                  <td className="px-4 py-3 text-white/50 text-xs">
                    {row.base_ref || row.head_ref ? `${row.base_ref ?? "—"} → ${row.head_ref ?? "—"}` : "—"}
                  </td>
                  <td className="px-4 py-3 text-white/50">{row.ci_status ?? "—"}</td>
                </tr>
              ))}
              {branchPrStatus.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-4 py-6 text-center text-white/30">
                    Ingen branch-/PR-status ingesterad ännu.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>

      <section aria-labelledby="conflicts-heading" className="space-y-3">
        <h2 id="conflicts-heading" className="text-sm font-medium text-white/70">
          Konflikter &amp; dubbelarbete
        </h2>
        <p className="text-xs text-white/40">
          Heuristisk flaggning för mänsklig granskning — fattar aldrig beslut, mergar aldrig, löser aldrig
          automatiskt.
        </p>
        {!hasConflicts && <p className="text-sm text-white/40">Inget flaggat just nu.</p>}
        {conflicts && conflicts.duplicate_work_candidates.length > 0 && (
          <div className="rounded-xl border border-amber-500/30 p-4 space-y-2">
            <div className="text-xs uppercase tracking-wide text-amber-300">Möjligt dubbelarbete</div>
            <pre className="whitespace-pre-wrap text-xs text-white/70">
              {JSON.stringify(conflicts.duplicate_work_candidates, null, 2)}
            </pre>
          </div>
        )}
        {conflicts && conflicts.data_integrity_issues.length > 0 && (
          <div className="rounded-xl border border-red-500/30 p-4 space-y-2">
            <div className="text-xs uppercase tracking-wide text-red-300">Dataintegritetsproblem</div>
            <pre className="whitespace-pre-wrap text-xs text-white/70">
              {JSON.stringify(conflicts.data_integrity_issues, null, 2)}
            </pre>
          </div>
        )}
      </section>

      <section aria-labelledby="notes-heading" className="space-y-4">
        <h2 id="notes-heading" className="text-sm font-medium text-white/70">
          Öppna noteringar
        </h2>
        {(Object.keys(KIND_LABELS) as ProjectNoteKind[]).map((kind) => {
          const items = notesByKind(kind);
          if (items.length === 0) return null;
          return (
            <div key={kind} className="space-y-2">
              <div className="text-xs uppercase tracking-wide text-white/40">{KIND_LABELS[kind]}</div>
              <ul className="space-y-2">
                {items.map((note) => (
                  <li key={note.id} className="rounded-lg border border-border/60 p-3 text-sm">
                    <div>{note.content}</div>
                    <div className="text-xs text-white/40 mt-1">
                      Källa: {note.source_type} {note.source_ref}
                      {note.classification && (
                        <span className="ml-2 rounded bg-white/10 px-1.5 py-0.5 text-white/50">
                          {note.classification === "needs_founder_decision" ? "Kräver grundarbeslut" : note.classification}
                        </span>
                      )}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          );
        })}
        {notes.length === 0 && <p className="text-sm text-white/40">Inga öppna noteringar.</p>}
      </section>
    </div>
  );
}
