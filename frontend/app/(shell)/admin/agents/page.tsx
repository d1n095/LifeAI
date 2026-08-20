"use client";

import { useEffect, useState } from "react";
import { api, AgentTask, AgentTaskDetail } from "@/lib/api";

// MainAI Core: minimal founder-only view onto agent orchestration (see
// backend/app/agent_orchestration.py). Deliberately plain, same convention as
// /admin/memory — shows tasks, lets the founder walk one through
// dispatch -> test results -> review -> PR proposal -> (always-blocked) merge attempt, and
// shows the full event history so nothing is hidden state.

const STATUS_LABELS: Record<string, string> = {
  created: "Skapat",
  dispatched: "Skickat till agent",
  result_recorded: "Resultat registrerat",
  reviewed_approved: "Granskat — godkänt",
  reviewed_needs_correction: "Granskat — behöver rättas",
  reviewed_rejected: "Granskat — avvisat",
  pr_prepared: "PR förberedd",
  pr_opened: "PR öppnad",
  ready_for_human: "Redo för mänskligt beslut",
};

export default function AgentOrchestrationPage() {
  const [tasks, setTasks] = useState<AgentTask[]>([]);
  const [selected, setSelected] = useState<AgentTaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [acceptanceCriteria, setAcceptanceCriteria] = useState("");
  const [testPassed, setTestPassed] = useState(true);
  const [testOutput, setTestOutput] = useState("");
  const [branchName, setBranchName] = useState("");
  const [baseBranch, setBaseBranch] = useState("main");

  async function refreshTasks() {
    try {
      setTasks(await api.agentTasks());
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function openTask(id: string) {
    try {
      setSelected(await api.agentTaskDetail(id));
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refreshTasks();
  }, []);

  async function createTask() {
    if (!title.trim() || !description.trim() || !acceptanceCriteria.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const task = await api.agentCreateTask({ title, description, acceptance_criteria: acceptanceCriteria });
      setTitle("");
      setDescription("");
      setAcceptanceCriteria("");
      await refreshTasks();
      await openTask(task.id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function runStep(fn: () => Promise<unknown>) {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      await fn();
      await refreshTasks();
      await openTask(selected.id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-xl font-semibold">Agentuppdrag</h1>
        <p className="text-sm text-white/50 mt-1">
          MainAI Core: agentorkestrering — MainAI äger uppdraget, kontexten, reglerna och granskningshistoriken.
          Kod- och granskningsagenter är utbytbara motorer (befintliga provider-adaptrar). Ingen automatisk merge
          finns i denna version — endast förslag på PR-innehåll, eller (om explicit påslaget) en verklig PR.
        </p>
      </div>

      {error && (
        <div role="alert" className="text-sm text-red-300">
          {error}
        </div>
      )}

      <section className="grid gap-6 md:grid-cols-2">
        <div className="space-y-4">
          <div className="rounded-xl border border-border p-4 space-y-2">
            <div className="text-xs uppercase tracking-wide text-white/40">Nytt uppdrag</div>
            <input
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Titel"
              className="w-full rounded-lg bg-panel border border-border px-3 py-2 text-sm"
            />
            <textarea
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Beskrivning av uppdraget…"
              rows={3}
              className="w-full rounded-lg bg-panel border border-border px-3 py-2 text-sm"
            />
            <textarea
              value={acceptanceCriteria}
              onChange={(e) => setAcceptanceCriteria(e.target.value)}
              placeholder="Acceptanskriterier…"
              rows={2}
              className="w-full rounded-lg bg-panel border border-border px-3 py-2 text-sm"
            />
            <button
              type="button"
              disabled={busy || !title.trim() || !description.trim() || !acceptanceCriteria.trim()}
              onClick={createTask}
              className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
            >
              Skapa uppdrag
            </button>
          </div>

          <div className="rounded-xl border border-border overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-panel text-white/50 text-xs uppercase">
                <tr>
                  <th scope="col" className="text-left px-4 py-3">
                    Titel
                  </th>
                  <th scope="col" className="text-left px-4 py-3">
                    Status
                  </th>
                </tr>
              </thead>
              <tbody>
                {tasks.map((t) => (
                  <tr
                    key={t.id}
                    onClick={() => openTask(t.id)}
                    className={`border-t border-border/60 cursor-pointer hover:bg-white/5 ${selected?.id === t.id ? "bg-white/5" : ""}`}
                  >
                    <td className="px-4 py-3">{t.title}</td>
                    <td className="px-4 py-3 text-white/50">{STATUS_LABELS[t.status] ?? t.status}</td>
                  </tr>
                ))}
                {tasks.length === 0 && (
                  <tr>
                    <td colSpan={2} className="px-4 py-6 text-center text-white/30">
                      Inga uppdrag ännu.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="space-y-3">
          {!selected && <p className="text-sm text-white/40">Välj ett uppdrag i listan för att se detaljer.</p>}
          {selected && (
            <div className="rounded-xl border border-border p-4 space-y-4">
              <div>
                <div className="text-sm font-medium">{selected.title}</div>
                <div className="text-xs text-white/40 mt-1">{STATUS_LABELS[selected.status] ?? selected.status}</div>
                <p className="text-sm text-white/60 mt-2">{selected.description}</p>
                <p className="text-xs text-white/40 mt-2">Acceptanskriterier: {selected.acceptance_criteria}</p>
              </div>

              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => runStep(() => api.agentDispatchTask(selected.id))}
                  className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
                >
                  Skicka till kodagent
                </button>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <label className="text-xs text-white/50 flex items-center gap-1">
                  <input type="checkbox" checked={testPassed} onChange={(e) => setTestPassed(e.target.checked)} />
                  Tester gröna
                </label>
                <input
                  value={testOutput}
                  onChange={(e) => setTestOutput(e.target.value)}
                  placeholder="Testutdata…"
                  className="flex-1 min-w-[8rem] rounded-lg bg-panel border border-border px-3 py-1.5 text-xs"
                />
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => runStep(() => api.agentRecordTestResults(selected.id, { passed: testPassed, output: testOutput }))}
                  className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
                >
                  Registrera testresultat
                </button>
              </div>

              <div className="flex flex-wrap gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => runStep(() => api.agentReviewTask(selected.id))}
                  className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
                >
                  Granska
                </button>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <input
                  value={branchName}
                  onChange={(e) => setBranchName(e.target.value)}
                  placeholder="Branch-namn"
                  className="flex-1 min-w-[8rem] rounded-lg bg-panel border border-border px-3 py-1.5 text-xs"
                />
                <input
                  value={baseBranch}
                  onChange={(e) => setBaseBranch(e.target.value)}
                  placeholder="Bas-branch"
                  className="flex-1 min-w-[8rem] rounded-lg bg-panel border border-border px-3 py-1.5 text-xs"
                />
                <button
                  type="button"
                  disabled={busy || !branchName.trim()}
                  onClick={() => runStep(() => api.agentPreparePr(selected.id, { branch_name: branchName, base_branch: baseBranch }))}
                  className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
                >
                  Förbered PR
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => runStep(() => api.agentAttemptMerge(selected.id))}
                  className="text-xs rounded border border-amber-500/30 text-amber-300 px-3 py-1.5 disabled:opacity-30"
                  title="Alltid blockerad i denna version — ingen merge-kapacitet implementerad än."
                >
                  Försök auto-merga (alltid blockerad)
                </button>
              </div>

              <div className="space-y-2">
                <div className="text-xs uppercase tracking-wide text-white/40">Händelsehistorik</div>
                <ul className="space-y-2 max-h-96 overflow-y-auto">
                  {selected.events.map((ev) => (
                    <li key={ev.id} className="rounded-lg border border-border/60 p-3 text-xs">
                      <div className="flex justify-between text-white/50">
                        <span>{ev.event_type}</span>
                        <span>{new Date(ev.created_at).toLocaleString("sv-SE")}</span>
                      </div>
                      {ev.provider && (
                        <div className="text-white/40">
                          {ev.provider} / {ev.model}
                        </div>
                      )}
                      <pre className="whitespace-pre-wrap text-white/60 mt-1">{JSON.stringify(ev.payload, null, 2).slice(0, 2000)}</pre>
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
