import type { FounderWorkStatus, MainAITask, MainAITaskLiveness } from "@/lib/api";

export const FOUNDER_STATUS_LABELS: Record<FounderWorkStatus, string> = {
  queued: "Köad",
  assigned: "Tilldelad",
  acknowledged: "Kvitterad",
  running: "Kör",
  waiting_on_dependency: "Väntar på beroende",
  failed: "Misslyckades",
  completed: "Klar",
  independently_verified: "Självständigt verifierad",
  cancelled: "Avbruten",
};

export const FOUNDER_STATUS_COLORS: Record<FounderWorkStatus, string> = {
  queued: "text-white/50",
  assigned: "text-white/70",
  acknowledged: "text-sky-300",
  running: "text-blue-300",
  waiting_on_dependency: "text-amber-300",
  failed: "text-red-300",
  completed: "text-emerald-300",
  independently_verified: "text-emerald-200",
  cancelled: "text-white/45",
};

/** Client mirror of backend/app/mainai_founder_ops/status.py for the execution list.
 * Assignment existence is never treated as running. */
export function founderStatusForExecutionTask(task: MainAITask): FounderWorkStatus {
  const liveness: MainAITaskLiveness = task.liveness;
  if (task.status === "completed") {
    return task.independently_verified ? "independently_verified" : "completed";
  }
  if (task.status === "cancelled") return "cancelled";
  if (task.status === "failed" || task.status === "retryable_failed") {
    return "failed";
  }
  if (liveness === "stalled" || liveness === "dead") return "failed";
  if (liveness === "running") return "running";
  if (
    task.status === "pending" ||
    task.status === "blocked" ||
    task.status === "waiting_external" ||
    task.status === "waiting_ci"
  ) {
    return "waiting_on_dependency";
  }
  if (task.status === "ready" && task.mainai_job_id) return "acknowledged";
  return "queued";
}
