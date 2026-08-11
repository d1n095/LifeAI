"use client";

import { useEffect, useRef, useState } from "react";
import { api, MainAIGoal, MainAIGoalDetail, MainAIGoalReport, MainAITaskDetail } from "@/lib/api";

// MainAI Execution Loop V0.1 (see backend/docs/MAINAI_EXECUTION_LOOP_V0_1.md,
// backend/app/mainai_execution/*). Deliberately plain, function-before-design, same
// convention as /admin/jobs: real, persisted goal/task state polled from the server, never an
// animated stand-in for progress that isn't actually known yet. A goal created here has NO
// plan until "Skapa plan (AI)" is pressed; once it does, app/worker.py's own auto-advance tick
// dispatches every `ready` task automatically -- there is no "run now" button here because
// there is nothing for it to do that the worker doesn't already do on its own next poll cycle.

const TASK_STATUS_LABELS: Record<string, string> = {
  pending: "Väntar på beroende",
  ready: "Redo",
  running: "Kör",
  waiting_external: "Väntar (externt)",
  waiting_ci: "Väntar (CI)",
  blocked: "Blockerad",
  retryable_failed: "Misslyckades (kan försökas igen)",
  failed: "Misslyckades",
  completed: "Klar",
  cancelled: "Avbruten",
};

const LIVENESS_LABELS: Record<string, string> = {
  idle: "Inaktiv",
  running: "Kör",
  stalled: "Fastnat (lease utgången)",
  dead: "Död (inget aktivt jobb)",
  waiting_external: "Väntar (externt)",
  waiting_ci: "Väntar (CI)",
  done: "Klar",
};

const LIVENESS_COLORS: Record<string, string> = {
  idle: "text-white/40",
  running: "text-blue-300",
  stalled: "text-amber-300",
  dead: "text-red-300",
  waiting_external: "text-amber-300",
  waiting_ci: "text-amber-300",
  done: "text-emerald-300",
};

const POLL_INTERVAL_MS = 4000;

export default function MainAIExecutionPage() {
  const [goals, setGoals] = useState<MainAIGoal[]>([]);
  const [selectedGoal, setSelectedGoal] = useState<MainAIGoalDetail | null>(null);
  const [selectedTask, setSelectedTask] = useState<MainAITaskDetail | null>(null);
  const [report, setReport] = useState<MainAIGoalReport | null>(null);
  const [title, setTitle] = useState("");
  const [instruction, setInstruction] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const latestGoalRequestId = useRef(0);

  async function refreshGoals() {
    try {
      setGoals(await api.mainaiExecutionGoals());
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function openGoal(id: string) {
    const requestId = ++latestGoalRequestId.current;
    try {
      const detail = await api.mainaiExecutionGoalDetail(id);
      if (requestId !== latestGoalRequestId.current) return;
      setSelectedGoal(detail);
      setSelectedTask(null);
      setReport(null);
    } catch (e: any) {
      if (requestId !== latestGoalRequestId.current) return;
      setError(e.message);
    }
  }

  async function openTask(id: string) {
    try {
      setSelectedTask(await api.mainaiExecutionTaskDetail(id));
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refreshGoals();
  }, []);

  // Real polling of the server's own state -- see admin/jobs/page.tsx's identical convention.
  useEffect(() => {
    const interval = setInterval(() => {
      refreshGoals();
      if (selectedGoal) openGoal(selectedGoal.id);
      if (selectedTask) openTask(selectedTask.id);
    }, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedGoal?.id, selectedTask?.id]);

  async function createGoal() {
    if (!title.trim() || !instruction.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const goal = await api.mainaiExecutionCreateGoal({ title, original_instruction: instruction });
      setTitle("");
      setInstruction("");
      await refreshGoals();
      await openGoal(goal.id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function createPlan() {
    if (!selectedGoal) return;
    setBusy(true);
    setError(null);
    try {
      await api.mainaiExecutionCreatePlan(selectedGoal.id);
      await openGoal(selectedGoal.id);
      await refreshGoals();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function loadReport() {
    if (!selectedGoal) return;
    setError(null);
    try {
      setReport(await api.mainaiExecutionGoalReport(selectedGoal.id));
    } catch (e: any) {
      setError(e.message);
    }
  }

  async function taskAction(action: "approve" | "reject" | "cancel" | "retry") {
    if (!selectedTask) return;
    setBusy(true);
    setError(null);
    try {
      const fn = {
        approve: api.mainaiExecutionApproveTask,
        reject: api.mainaiExecutionRejectTask,
        cancel: api.mainaiExecutionCancelTask,
        retry: api.mainaiExecutionRetryTask,
      }[action];
      await fn(selectedTask.id);
      await openTask(selectedTask.id);
      if (selectedGoal) await openGoal(selectedGoal.id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-xl font-semibold">MainAI Execution Loop</h1>
        <p className="text-sm text-white/50 mt-1">
          Riktiga, varaktiga mål/planer/tasks -- inte MainAI:s egna påståenden. Varje rad här motsvarar en rad i databasen. Ett mål utan
          plan är precis det: inget MainAI ännu har gjort något med. Godkännande, verifiering och slutförande styrs av backend, aldrig
          av den här vyn.
        </p>
      </div>

      {error && (
        <div role="alert" className="text-sm text-red-300">
          {error}
        </div>
      )}

      <section className="grid gap-6 md:grid-cols-3">
        <div className="space-y-4">
          <div className="rounded-xl border border-border p-4 space-y-2">
            <div className="text-xs uppercase tracking-wide text-white/40">Nytt mål</div>
            <input
              className="w-full rounded border border-border bg-transparent px-2 py-1.5 text-sm"
              placeholder="Titel"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
            <textarea
              className="w-full rounded border border-border bg-transparent px-2 py-1.5 text-sm"
              placeholder="Fullständig instruktion"
              rows={3}
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
            />
            <button
              type="button"
              disabled={busy || !title.trim() || !instruction.trim()}
              onClick={createGoal}
              className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
            >
              Skapa mål
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
                  <th scope="col" className="text-left px-4 py-3">
                    Plan
                  </th>
                </tr>
              </thead>
              <tbody>
                {goals.map((g) => (
                  <tr
                    key={g.id}
                    onClick={() => openGoal(g.id)}
                    className={`border-t border-border/60 cursor-pointer hover:bg-white/5 ${selectedGoal?.id === g.id ? "bg-white/5" : ""}`}
                  >
                    <td className="px-4 py-3">{g.title}</td>
                    <td className="px-4 py-3 text-white/60">{g.status}</td>
                    <td className="px-4 py-3 text-white/40">v{g.current_plan_version}</td>
                  </tr>
                ))}
                {goals.length === 0 && (
                  <tr>
                    <td colSpan={3} className="px-4 py-6 text-center text-white/30">
                      Inga mål ännu.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>

        <div className="space-y-3">
          {!selectedGoal && <p className="text-sm text-white/40">Välj ett mål för att se plan och tasks.</p>}
          {selectedGoal && (
            <div className="rounded-xl border border-border p-4 space-y-4">
              <div>
                <div className="text-sm font-medium">{selectedGoal.title}</div>
                <div className="text-xs text-white/40 mt-1 space-y-0.5">
                  <div>Status: {selectedGoal.status}</div>
                  <div>Riskläge: {selectedGoal.risk_level}</div>
                  <div>Godkännandepolicy: {selectedGoal.approval_policy}</div>
                  {selectedGoal.plan && (
                    <div>
                      Plan v{selectedGoal.plan.version} ({selectedGoal.plan.status}): {selectedGoal.plan.rationale}
                    </div>
                  )}
                </div>
                <div className="flex flex-wrap gap-2 mt-3">
                  {!selectedGoal.plan && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={createPlan}
                      className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
                    >
                      Skapa plan (AI)
                    </button>
                  )}
                  <button type="button" onClick={loadReport} className="text-xs rounded border border-border px-3 py-1.5">
                    Visa slutrapport
                  </button>
                </div>
              </div>

              <div className="space-y-2">
                <div className="text-xs uppercase tracking-wide text-white/40">Tasks ({selectedGoal.tasks.length})</div>
                <ul className="space-y-2 max-h-96 overflow-y-auto">
                  {selectedGoal.tasks.map((t) => (
                    <li
                      key={t.id}
                      onClick={() => openTask(t.id)}
                      className={`rounded-lg border border-border/60 p-3 text-xs cursor-pointer hover:bg-white/5 ${selectedTask?.id === t.id ? "bg-white/5" : ""}`}
                    >
                      <div className="flex justify-between">
                        <span>{t.task_type}</span>
                        <span className={LIVENESS_COLORS[t.liveness] ?? ""}>{LIVENESS_LABELS[t.liveness] ?? t.liveness}</span>
                      </div>
                      <div className="text-white/50 mt-1">{TASK_STATUS_LABELS[t.status] ?? t.status}</div>
                      {t.blocker_reason && <div className="text-amber-300/80 mt-1">Blockerare: {t.blocker_reason}</div>}
                    </li>
                  ))}
                  {selectedGoal.tasks.length === 0 && <li className="text-white/30">Ingen plan ännu -- inga tasks.</li>}
                </ul>
              </div>

              {report && (
                <div className="space-y-2">
                  <div className="text-xs uppercase tracking-wide text-white/40">
                    Slutrapport ({report.summary.total_tasks} tasks, {report.summary.unresolved_risk_count} olöst risk)
                  </div>
                  <pre className="text-xs text-white/60 whitespace-pre-wrap max-h-48 overflow-y-auto">
                    {JSON.stringify(report.summary.by_outcome, null, 2)}
                  </pre>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="space-y-3">
          {!selectedTask && <p className="text-sm text-white/40">Välj en task för detaljer.</p>}
          {selectedTask && (
            <div className="rounded-xl border border-border p-4 space-y-4">
              <div>
                <div className="text-sm font-medium">{selectedTask.description}</div>
                <div className="text-xs text-white/40 mt-1 space-y-0.5">
                  <div>Typ: {selectedTask.task_type}</div>
                  <div>Status: {TASK_STATUS_LABELS[selectedTask.status] ?? selectedTask.status}</div>
                  <div className={LIVENESS_COLORS[selectedTask.liveness] ?? ""}>
                    Aktivitet: {LIVENESS_LABELS[selectedTask.liveness] ?? selectedTask.liveness}
                  </div>
                  <div>
                    Försök: {selectedTask.attempts}/{selectedTask.max_attempts}
                  </div>
                  <div>
                    Godkännande: {selectedTask.approval_required ? (selectedTask.approval_granted ? "Beviljat" : "Krävs, ej beviljat") : "Krävs inte"}
                  </div>
                </div>

                <div className="flex flex-wrap gap-2 mt-3">
                  {selectedTask.approval_required && !selectedTask.approval_granted && (
                    <>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => taskAction("approve")}
                        className="text-xs rounded border border-emerald-500/40 text-emerald-300 px-3 py-1.5 disabled:opacity-30"
                      >
                        Godkänn
                      </button>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => taskAction("reject")}
                        className="text-xs rounded border border-red-500/40 text-red-300 px-3 py-1.5 disabled:opacity-30"
                      >
                        Avvisa
                      </button>
                    </>
                  )}
                  {["pending", "ready", "blocked", "retryable_failed"].includes(selectedTask.status) && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => taskAction("cancel")}
                      className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
                    >
                      Avbryt
                    </button>
                  )}
                  {selectedTask.status === "retryable_failed" && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => taskAction("retry")}
                      className="text-xs rounded border border-border px-3 py-1.5 disabled:opacity-30"
                    >
                      Försök igen
                    </button>
                  )}
                </div>
              </div>

              <div className="space-y-2">
                <div className="text-xs uppercase tracking-wide text-white/40">Checkpoints ({selectedTask.checkpoints.length})</div>
                <ul className="space-y-2 max-h-40 overflow-y-auto">
                  {selectedTask.checkpoints.map((c) => (
                    <li key={c.id} className="rounded-lg border border-border/60 p-3 text-xs">
                      <div className="flex justify-between text-white/50">
                        <span>{String(c.executor_state?.step ?? "?")}</span>
                        <span>{new Date(c.created_at).toLocaleString("sv-SE")}</span>
                      </div>
                    </li>
                  ))}
                  {selectedTask.checkpoints.length === 0 && <li className="text-white/30">Inga checkpoints ännu.</li>}
                </ul>
              </div>

              <div className="space-y-2">
                <div className="text-xs uppercase tracking-wide text-white/40">Händelsehistorik</div>
                <ul className="space-y-2 max-h-64 overflow-y-auto">
                  {selectedTask.events.map((ev) => (
                    <li key={ev.id} className="rounded-lg border border-border/60 p-3 text-xs">
                      <div className="flex justify-between text-white/50">
                        <span>{ev.event_type}</span>
                        <span>{new Date(ev.created_at).toLocaleString("sv-SE")}</span>
                      </div>
                    </li>
                  ))}
                  {selectedTask.events.length === 0 && <li className="text-white/30">Inga händelser ännu.</li>}
                </ul>
              </div>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
