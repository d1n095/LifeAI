"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { api, MainAIGoal, ProjectDetail, ProjectStatus } from "@/lib/api";

const TERMINAL_GOAL_STATUSES = new Set(["completed", "failed", "cancelled"]);

const SOURCE_STATUS_LABELS: Record<string, string> = {
  indexed: "Indexerad",
  received: "Mottagen",
  original_stored: "Original sparat",
  extracting: "Extraherar",
  extracted: "Extraherad",
  embedding: "Skapar embedding",
  awaiting_provider: "Väntar på leverantör",
  blocked_provider: "Blockerad av leverantör",
  storage_failed: "Lagring misslyckades",
  extraction_failed: "Extrahering misslyckades",
  indexing_failed: "Indexering misslyckades",
  failed: "Misslyckades",
  cancelled: "Avbruten",
};

export default function ProjectDetailPage() {
  const params = useParams<{ id: string }>();
  const projectId = params.id;

  const [project, setProject] = useState<ProjectDetail | null>(null);
  const [status, setStatus] = useState<ProjectStatus | null>(null);
  const [goals, setGoals] = useState<MainAIGoal[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [newTask, setNewTask] = useState("");
  const [newTaskMilestone, setNewTaskMilestone] = useState("");
  const [creatingTask, setCreatingTask] = useState(false);
  const [newMilestone, setNewMilestone] = useState("");
  const [newMilestoneDue, setNewMilestoneDue] = useState("");
  const [creatingMilestone, setCreatingMilestone] = useState(false);

  async function refresh() {
    try {
      setError(null);
      setProject(await api.getProject(projectId));
      // These are supplementary reads: a failed global status must not make the project
      // itself look missing. Each failure is represented by its own empty/unknown state.
      api.projectStatus().then(setStatus).catch(() => setStatus(null));
      api.mainaiExecutionGoals().then(setGoals).catch(() => setGoals([]));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // Documented fetch-on-mount pattern, same as the other shell pages: state is set after
    // an await, not synchronously during render.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  async function addTask() {
    if (!newTask.trim() || creatingTask) return;
    setCreatingTask(true);
    setError(null);
    try {
      await api.createTask({
        title: newTask.trim(),
        status: "todo",
        priority: "medium",
        project_id: projectId,
        milestone_id: newTaskMilestone || null,
      });
      setNewTask("");
      setNewTaskMilestone("");
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setCreatingTask(false);
    }
  }

  async function addMilestone() {
    if (!newMilestone.trim() || creatingMilestone) return;
    setCreatingMilestone(true);
    setError(null);
    try {
      await api.createMilestone(projectId, {
        title: newMilestone.trim(),
        due_at: newMilestoneDue ? new Date(newMilestoneDue).toISOString() : null,
        status: "open",
      });
      setNewMilestone("");
      setNewMilestoneDue("");
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setCreatingMilestone(false);
    }
  }

  async function toggleMilestone(milestoneId: string, version: number, status: string) {
    setError(null);
    try {
      await api.updateMilestone(milestoneId, {
        status: status === "completed" ? "in_progress" : "completed",
        expected_version: version,
      });
      await refresh();
    } catch (e: any) {
      setError(e.message);
    }
  }

  if (loading) {
    return (
      <div className="text-sm text-white/40" role="status">
        Läser projektet…
      </div>
    );
  }

  if (error && !project) {
    return (
      <div className="space-y-4">
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
        <Link href="/projects" className="text-sm text-accent2 hover:underline">
          ← Tillbaka till projekten
        </Link>
      </div>
    );
  }

  if (!project) return null;

  const openTasks = project.tasks.filter((task) => task.status !== "done");
  const indexedSources = project.sources.filter((source) => source.status === "indexed");
  const activeGoals = goals.filter((goal) => !TERMINAL_GOAL_STATUSES.has(goal.status));

  return (
    <div className="space-y-6">
      <div>
        <Link href="/projects" className="text-sm text-white/50 hover:text-white">
          ← Tillbaka till projekten
        </Link>
        <div className="mt-3 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h1 className="text-xl font-semibold">{project.name}</h1>
            <p className="mt-1 text-sm text-white/50">{project.description || "Ingen beskrivning ännu."}</p>
          </div>
          <span className="rounded-full border border-border px-3 py-1 text-xs text-white/60">{project.status}</span>
        </div>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-4">
        <Stat label="Öppna uppgifter" value={openTasks.length} />
        <Stat label="Milstolpar" value={project.milestones.length} />
        <Stat label="Indexerade källor" value={indexedSources.length} total={project.sources.length} />
        <Stat label="Aktiva MainAI-mål" value={activeGoals.length} />
      </div>

      <section aria-labelledby="project-milestones" className="rounded-xl border border-border bg-panel p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h2 id="project-milestones" className="text-sm font-medium text-white/70">
              Milstolpar
            </h2>
            <p className="mt-1 text-xs text-white/35">
              Delmål som uppgifterna kan kopplas till. Statusen räknas fram från projektets uppgifter.
            </p>
          </div>
          <Link href="/tasks" className="text-xs text-accent2 hover:underline">
            Öppna uppgiftshanteraren →
          </Link>
        </div>
        <div className="mt-3 flex flex-col gap-2 sm:flex-row">
          <div className="min-w-0 flex-1">
            <label htmlFor="new-milestone" className="sr-only">
              Ny milstolpe
            </label>
            <input
              id="new-milestone"
              value={newMilestone}
              onChange={(event) => setNewMilestone(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && addMilestone()}
              placeholder="Ny milstolpe…"
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
          <div>
            <label htmlFor="new-milestone-due" className="sr-only">
              Milstolpens deadline
            </label>
            <input
              id="new-milestone-due"
              type="date"
              value={newMilestoneDue}
              onChange={(event) => setNewMilestoneDue(event.target.value)}
              className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
            />
          </div>
          <button
            type="button"
            onClick={addMilestone}
            disabled={creatingMilestone || !newMilestone.trim()}
            className="rounded-lg bg-accent px-3 py-2 text-sm disabled:opacity-50"
          >
            {creatingMilestone ? "Skapar…" : "Lägg till"}
          </button>
        </div>
        <ul className="mt-3 space-y-2">
          {project.milestones.map((milestone) => {
            const progress =
              milestone.task_count > 0
                ? Math.round((milestone.completed_task_count / milestone.task_count) * 100)
                : 0;
            return (
              <li key={milestone.id} className="rounded-lg border border-border/60 px-3 py-2 text-sm">
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <div className="min-w-0">
                    <div className="text-white/85">{milestone.title}</div>
                    <div className="mt-0.5 text-xs text-white/40">
                      {milestone.due_at
                        ? `Deadline ${new Date(milestone.due_at).toLocaleDateString("sv-SE")}`
                        : "Ingen deadline"}{" "}
                      · {milestone.completed_task_count}/{milestone.task_count} uppgifter klara
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <span className="rounded-full border border-border px-2 py-0.5 text-[10px] text-white/50">
                      {milestone.status}
                    </span>
                    <button
                      type="button"
                      onClick={() => toggleMilestone(milestone.id, milestone.version, milestone.status)}
                      className="text-xs text-accent2 hover:underline"
                    >
                      {milestone.status === "completed" ? "Återöppna" : "Markera klar"}
                    </button>
                  </div>
                </div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-white/10" aria-hidden="true">
                  <div className="h-full rounded-full bg-accent" style={{ width: `${progress}%` }} />
                </div>
              </li>
            );
          })}
          {project.milestones.length === 0 && (
            <li className="text-sm text-white/30">Inga milstolpar ännu.</li>
          )}
        </ul>
      </section>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <section aria-labelledby="project-knowledge" className="rounded-xl border border-border bg-panel p-4">
          <div className="mb-3 flex items-baseline justify-between gap-2">
            <h2 id="project-knowledge" className="text-sm font-medium text-white/70">
              Kunskap i projektet
            </h2>
            <Link href={`/library?project_id=${project.id}`} className="text-xs text-accent2 hover:underline">
              Öppna i biblioteket
            </Link>
          </div>
          <div className="space-y-2">
            {project.sources.map((source) => (
              <Link
                key={source.id}
                href={`/library/${source.id}`}
                className="block rounded-lg border border-border/60 px-3 py-2 text-sm hover:border-accent/50"
              >
                <div className="flex justify-between gap-2">
                  <span className="truncate text-white/85">{source.title}</span>
                  <span className="shrink-0 text-xs text-white/40">{source.classification}</span>
                </div>
                <div className="mt-1 text-xs text-white/40">
                  {SOURCE_STATUS_LABELS[source.status] ?? source.status} · {source.chunk_count} chunkar · v
                  {source.version_number}
                </div>
                {source.error_message && <div className="mt-1 text-xs text-red-300">{source.error_message}</div>}
              </Link>
            ))}
            {project.sources.length === 0 && (
              <p className="text-sm text-white/30">
                Inga källor är knutna till det här projektet ännu. Importera material och välj projektet i
                biblioteket.
              </p>
            )}
          </div>
        </section>

        <section aria-labelledby="project-tasks" className="rounded-xl border border-border bg-panel p-4">
          <h2 id="project-tasks" className="mb-3 text-sm font-medium text-white/70">
            Uppgifter i projektet
          </h2>
          <div className="mb-3 flex flex-col gap-2 sm:flex-row">
            <div className="min-w-0 flex-1">
              <label htmlFor="project-new-task" className="sr-only">
                Ny uppgift i projektet
              </label>
              <input
                id="project-new-task"
                value={newTask}
                onChange={(e) => setNewTask(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && addTask()}
                placeholder="Ny uppgift i projektet…"
                className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
              />
            </div>
            <label htmlFor="project-task-milestone" className="sr-only">
              Milstolpe för uppgiften
            </label>
            <select
              id="project-task-milestone"
              value={newTaskMilestone}
              onChange={(e) => setNewTaskMilestone(e.target.value)}
              className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
            >
              <option value="">Ingen milstolpe</option>
              {project.milestones.map((milestone) => (
                <option key={milestone.id} value={milestone.id}>
                  {milestone.title}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={addTask}
              disabled={creatingTask || !newTask.trim()}
              className="rounded-lg bg-accent px-3 py-2 text-sm disabled:opacity-50"
            >
              {creatingTask ? "Skapar…" : "Lägg till"}
            </button>
          </div>
          <ul className="space-y-2">
            {project.tasks.map((task) => (
              <li key={task.id} className="rounded-lg border border-border/60 px-3 py-2 text-sm">
                <div className="flex items-center justify-between gap-2">
                  <span className={task.status === "done" ? "text-white/30 line-through" : "text-white/85"}>
                    {task.title}
                  </span>
                  <span className="flex shrink-0 items-center gap-1.5">
                    {task.is_blocked && (
                      <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-200">
                        Blockerad
                      </span>
                    )}
                    <span className="text-xs text-white/40">{task.status}</span>
                  </span>
                </div>
                {task.description && <p className="mt-1 text-xs text-white/40">{task.description}</p>}
              </li>
            ))}
            {project.tasks.length === 0 && <li className="text-sm text-white/30">Inga uppgifter ännu.</li>}
          </ul>
        </section>
      </div>

      <section aria-labelledby="project-mainai" className="rounded-xl border border-border bg-panel p-4">
        <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h2 id="project-mainai" className="text-sm font-medium text-white/70">
              MainAI-läge
            </h2>
            <p className="mt-1 text-xs text-white/35">
              Globala mål och arbetskedjor. Målen är inte projektkopplade i datamodellen, så de visas som
              globala körningar — inte som påståenden om det här projektet.
            </p>
          </div>
          <Link href="/admin/mainai-execution" className="text-xs text-accent2 hover:underline">
            Öppna MainAI Execution →
          </Link>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="rounded-lg border border-border/60 p-3">
            <div className="text-xs uppercase tracking-wide text-white/35">Aktiva mål</div>
            <ul className="mt-2 space-y-2">
              {activeGoals.slice(0, 5).map((goal) => (
                <li key={goal.id} className="flex items-start justify-between gap-2 text-sm">
                  <span className="min-w-0 truncate text-white/80">{goal.title}</span>
                  <span className="shrink-0 text-xs text-white/40">
                    {goal.status} · plan v{goal.current_plan_version}
                  </span>
                </li>
              ))}
              {activeGoals.length === 0 && <li className="text-sm text-white/30">Inga aktiva MainAI-mål.</li>}
            </ul>
          </div>

          <div className="rounded-lg border border-border/60 p-3">
            <div className="text-xs uppercase tracking-wide text-white/35">Global arbetskedja</div>
            {status ? (
              <ul className="mt-2 space-y-1 text-sm text-white/70">
                <li>{status.work.goals.active_count} aktiva mål</li>
                <li className={status.work.tasks.awaiting_approval_count ? "text-amber-200" : ""}>
                  {status.work.tasks.awaiting_approval_count} väntar på godkännande
                </li>
                <li>
                  {status.work.tasks.counts.running ?? 0} uppgifter körs · {status.work.jobs.active} aktiva jobb
                </li>
                <li>{status.work.agents.open_assignments} öppna agentuppdrag</li>
              </ul>
            ) : (
              <p className="mt-2 text-sm text-amber-200">
                Arbetskedjan kunde inte verifieras just nu. Det betyder inte att den är tom.
              </p>
            )}
          </div>
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value, total }: { label: string; value: number; total?: number }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4">
      <div className="text-xs uppercase tracking-wide text-white/40">{label}</div>
      <div className="mt-2 text-2xl font-semibold">
        {value}
        {total !== undefined && <span className="text-base text-white/30"> / {total}</span>}
      </div>
    </div>
  );
}
