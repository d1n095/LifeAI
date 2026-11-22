"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  MilestoneItem,
  ProjectItem,
  TaskDetailItem,
  TaskItem,
} from "@/lib/api";

const STATUS_LABELS: Record<string, string> = {
  todo: "Att göra",
  in_progress: "Pågår",
  blocked: "Blockerad",
  done: "Klar",
  cancelled: "Avbruten",
};

const PRIORITY_LABELS: Record<string, string> = {
  low: "Låg",
  medium: "Medel",
  high: "Hög",
  critical: "Kritisk",
};

const STATUS_OPTIONS = ["todo", "in_progress", "done", "cancelled"];
const PRIORITY_OPTIONS = ["low", "medium", "high", "critical"];

type CreateState = {
  title: string;
  description: string;
  project_id: string;
  milestone_id: string;
  priority: string;
  status: string;
  due_at: string;
  depends_on: string;
};

type EditState = {
  title: string;
  description: string;
  status: string;
  priority: string;
  due_at: string;
  milestone_id: string;
  expected_version: number;
};

function toDateTimeLocal(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  const pad = (number: number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}`;
}

function toIso(value: string): string | null {
  return value ? new Date(value).toISOString() : null;
}

function formatDate(value: string | null | undefined): string {
  if (!value) return "Ingen deadline";
  return new Date(value).toLocaleString("sv-SE", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export default function TasksPage() {
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [filterQ, setFilterQ] = useState("");
  const [filterStatus, setFilterStatus] = useState("");
  const [filterPriority, setFilterPriority] = useState("");
  const [filterProject, setFilterProject] = useState("");
  const [blockedOnly, setBlockedOnly] = useState(false);

  const [form, setForm] = useState<CreateState>({
    title: "",
    description: "",
    project_id: "",
    milestone_id: "",
    priority: "medium",
    status: "todo",
    due_at: "",
    depends_on: "",
  });
  const [createMilestones, setCreateMilestones] = useState<MilestoneItem[]>([]);

  const [selected, setSelected] = useState<TaskDetailItem | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [edit, setEdit] = useState<EditState | null>(null);
  const [editMilestones, setEditMilestones] = useState<MilestoneItem[]>([]);
  const [dependencyToAdd, setDependencyToAdd] = useState("");

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const [taskRows, projectRows] = await Promise.all([
        api.listTasks({
          project_id: filterProject || undefined,
          status: filterStatus || undefined,
          priority: filterPriority || undefined,
          q: filterQ.trim() || undefined,
          blocked_only: blockedOnly || undefined,
        }),
        api.listProjects(),
      ]);
      setTasks(taskRows);
      setProjects(projectRows);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // Documented fetch-on-mount pattern used throughout the shell.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!form.project_id) return;
    api
      .listMilestones(form.project_id)
      .then(setCreateMilestones)
      .catch(() => setCreateMilestones([]));
  }, [form.project_id]);

  async function openTask(taskId: string) {
    setDetailLoading(true);
    setError(null);
    try {
      const detail = await api.getTask(taskId);
      setSelected(detail);
      setEdit({
        title: detail.title,
        description: detail.description ?? "",
        status: detail.status,
        priority: detail.priority,
        due_at: toDateTimeLocal(detail.due_at),
        milestone_id: detail.milestone_id ?? "",
        expected_version: detail.version ?? 1,
      });
      setDependencyToAdd("");
      if (detail.project_id) {
        api.listMilestones(detail.project_id).then(setEditMilestones).catch(() => setEditMilestones([]));
      } else {
        setEditMilestones([]);
      }
    } catch (e: any) {
      setError(e.message);
    } finally {
      setDetailLoading(false);
    }
  }

  async function createTask(event: React.FormEvent) {
    event.preventDefault();
    if (!form.title.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.createTask({
        title: form.title.trim(),
        description: form.description.trim() || null,
        project_id: form.project_id || null,
        milestone_id: form.milestone_id || null,
        priority: form.priority,
        status: form.status,
        due_at: toIso(form.due_at),
        depends_on_task_ids: form.depends_on ? [form.depends_on] : [],
      });
      setForm((current) => ({
        ...current,
        title: "",
        description: "",
        due_at: "",
        depends_on: "",
        milestone_id: "",
      }));
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function saveTask(event: React.FormEvent) {
    event.preventDefault();
    if (!selected || !edit || busy) return;
    if (!edit.title.trim()) {
      setError("Uppgiftens titel får inte vara tom.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.updateTask(selected.id, {
        title: edit.title.trim(),
        description: edit.description.trim() || null,
        status: edit.status,
        priority: edit.priority,
        due_at: toIso(edit.due_at),
        milestone_id: edit.milestone_id || null,
        expected_version: edit.expected_version,
      });
      await refresh();
      await openTask(selected.id);
    } catch (e: any) {
      setError(e.message);
      if (e?.status === 409) {
        await refresh();
        await openTask(selected.id);
      }
    } finally {
      setBusy(false);
    }
  }

  async function quickStatus(task: TaskItem, status: string) {
    setBusy(true);
    setError(null);
    try {
      await api.updateTask(task.id, { status, expected_version: task.version });
      await refresh();
      if (selected?.id === task.id) await openTask(task.id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function addDependency() {
    if (!selected || !dependencyToAdd || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.addTaskDependency(selected.id, dependencyToAdd);
      await openTask(selected.id);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function removeDependency(dependsOnTaskId: string) {
    if (!selected || busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.removeTaskDependency(selected.id, dependsOnTaskId);
      await openTask(selected.id);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function deleteTask() {
    if (!selected || busy) return;
    if (!window.confirm("Ta bort uppgiften? Historiken behålls i databasen som en borttagningshändelse.")) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await api.deleteTask(selected.id);
      setSelected(null);
      setEdit(null);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const dependencyCandidates = selected
    ? tasks.filter(
        (task) =>
          task.id !== selected.id &&
          !selected.dependencies.some((dependency) => dependency.depends_on_task_id === task.id)
      )
    : [];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Uppgifter</h1>
          <p className="mt-1 max-w-3xl text-sm text-white/50">
            Dina egna uppgifter med prioriteringar, beroenden, milstolpar och historik. De är
            separata från MainAI:s auktoriserade exekveringsjobb och startar ingen körning.
          </p>
        </div>
        <Link href="/projects" className="rounded-lg border border-border px-3 py-2 text-sm text-white/60 hover:text-white">
          Projekt & milstolpar →
        </Link>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      <section aria-labelledby="task-create" className="rounded-xl border border-border bg-panel p-4">
        <h2 id="task-create" className="text-sm font-medium text-white/70">
          Ny uppgift
        </h2>
        <form onSubmit={createTask} className="mt-3 grid gap-3 md:grid-cols-2">
          <div className="md:col-span-2">
            <label htmlFor="task-title" className="mb-1 block text-xs text-white/40">
              Titel
            </label>
            <input
              id="task-title"
              required
              value={form.title}
              onChange={(event) => setForm({ ...form, title: event.target.value })}
              placeholder="Vad ska göras?"
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
          <div className="md:col-span-2">
            <label htmlFor="task-description" className="mb-1 block text-xs text-white/40">
              Beskrivning (valfritt)
            </label>
            <textarea
              id="task-description"
              value={form.description}
              onChange={(event) => setForm({ ...form, description: event.target.value })}
              rows={2}
              className="w-full resize-none rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
          <div>
            <label htmlFor="task-project" className="mb-1 block text-xs text-white/40">
              Projekt
            </label>
            <select
              id="task-project"
              value={form.project_id}
              onChange={(event) => {
                setCreateMilestones([]);
                setForm({ ...form, project_id: event.target.value, milestone_id: "" });
              }}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            >
              <option value="">Inget projekt</option>
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="task-milestone" className="mb-1 block text-xs text-white/40">
              Milstolpe
            </label>
            <select
              id="task-milestone"
              value={form.milestone_id}
              onChange={(event) => setForm({ ...form, milestone_id: event.target.value })}
              disabled={!form.project_id}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm disabled:opacity-50"
            >
              <option value="">Ingen milstolpe</option>
              {createMilestones.map((milestone) => (
                <option key={milestone.id} value={milestone.id}>
                  {milestone.title}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="task-priority" className="mb-1 block text-xs text-white/40">
              Prioritet
            </label>
            <select
              id="task-priority"
              value={form.priority}
              onChange={(event) => setForm({ ...form, priority: event.target.value })}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            >
              {PRIORITY_OPTIONS.map((priority) => (
                <option key={priority} value={priority}>
                  {PRIORITY_LABELS[priority]}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="task-status" className="mb-1 block text-xs text-white/40">
              Status
            </label>
            <select
              id="task-status"
              value={form.status}
              onChange={(event) => setForm({ ...form, status: event.target.value })}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            >
              {STATUS_OPTIONS.map((status) => (
                <option key={status} value={status}>
                  {STATUS_LABELS[status]}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="task-due" className="mb-1 block text-xs text-white/40">
              Deadline
            </label>
            <input
              id="task-due"
              type="datetime-local"
              value={form.due_at}
              onChange={(event) => setForm({ ...form, due_at: event.target.value })}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label htmlFor="task-depends" className="mb-1 block text-xs text-white/40">
              Beror på
            </label>
            <select
              id="task-depends"
              value={form.depends_on}
              onChange={(event) => setForm({ ...form, depends_on: event.target.value })}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            >
              <option value="">Ingen dependency</option>
              {tasks.map((task) => (
                <option key={task.id} value={task.id}>
                  {task.title}
                </option>
              ))}
            </select>
          </div>
          <div className="flex items-end md:col-span-2">
            <button
              type="submit"
              disabled={busy || !form.title.trim()}
              className="rounded-lg bg-accent px-4 py-2 text-sm disabled:opacity-50"
            >
              {busy ? "Sparar…" : "Skapa uppgift"}
            </button>
          </div>
        </form>
      </section>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          refresh();
        }}
        className="grid gap-2 rounded-xl border border-border bg-panel p-3 sm:grid-cols-2 lg:grid-cols-6"
      >
        <div className="lg:col-span-2">
          <label htmlFor="filter-q" className="sr-only">
            Sök uppgift
          </label>
          <input
            id="filter-q"
            value={filterQ}
            onChange={(event) => setFilterQ(event.target.value)}
            placeholder="Sök titel eller beskrivning…"
            className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
          />
        </div>
        <select
          aria-label="Filtrera på status"
          value={filterStatus}
          onChange={(event) => setFilterStatus(event.target.value)}
          className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
        >
          <option value="">Alla statusar</option>
          {STATUS_OPTIONS.concat("blocked").map((status) => (
            <option key={status} value={status}>
              {STATUS_LABELS[status]}
            </option>
          ))}
        </select>
        <select
          aria-label="Filtrera på prioritet"
          value={filterPriority}
          onChange={(event) => setFilterPriority(event.target.value)}
          className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
        >
          <option value="">Alla prioriteter</option>
          {PRIORITY_OPTIONS.map((priority) => (
            <option key={priority} value={priority}>
              {PRIORITY_LABELS[priority]}
            </option>
          ))}
        </select>
        <select
          aria-label="Filtrera på projekt"
          value={filterProject}
          onChange={(event) => setFilterProject(event.target.value)}
          className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
        >
          <option value="">Alla projekt</option>
          {projects.map((project) => (
            <option key={project.id} value={project.id}>
              {project.name}
            </option>
          ))}
        </select>
        <div className="flex items-center justify-between gap-2">
          <label className="flex items-center gap-2 text-xs text-white/60">
            <input
              type="checkbox"
              checked={blockedOnly}
              onChange={(event) => setBlockedOnly(event.target.checked)}
            />
            Endast blockerade
          </label>
          <button type="submit" className="rounded-lg border border-border px-3 py-2 text-sm text-white/70 hover:text-white">
            Filtrera
          </button>
        </div>
      </form>

      <div className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <section aria-labelledby="task-list" className="rounded-xl border border-border bg-panel p-4">
          <div className="flex items-baseline justify-between gap-2">
            <h2 id="task-list" className="text-sm font-medium text-white/70">
              Uppgifter
            </h2>
            <span className="text-xs text-white/40">{tasks.length} visas</span>
          </div>

          {loading ? (
            <p role="status" className="mt-3 text-sm text-white/40">
              Läser uppgifter…
            </p>
          ) : tasks.length === 0 ? (
            <div className="mt-3 rounded-lg border border-dashed border-border p-6 text-center text-sm text-white/40">
              Inga uppgifter matchar filtret. Skapa en ovan eller rensa filtret.
            </div>
          ) : (
            <ul className="mt-3 space-y-2">
              {tasks.map((task) => {
                const isBlocked = task.is_blocked || task.status === "blocked";
                return (
                  <li
                    key={task.id}
                    className={`rounded-lg border px-3 py-2 text-sm ${
                      selected?.id === task.id ? "border-accent/60 bg-accent/5" : "border-border/60"
                    }`}
                  >
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <button
                        type="button"
                        onClick={() => openTask(task.id)}
                        className={`min-w-0 text-left ${
                          task.status === "done" || task.status === "cancelled"
                            ? "text-white/35 line-through"
                            : "text-white/85 hover:text-accent"
                        }`}
                      >
                        {task.title}
                      </button>
                      <div className="flex shrink-0 items-center gap-1.5">
                        {isBlocked && (
                          <span className="rounded-full border border-amber-500/40 bg-amber-500/10 px-2 py-0.5 text-[10px] text-amber-200">
                            Blockerad
                          </span>
                        )}
                        {task.suggested_by_ai && (
                          <span className="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 text-[10px] text-accent2">
                            AI-förslag
                          </span>
                        )}
                        <span className="rounded-full border border-border px-2 py-0.5 text-[10px] text-white/50">
                          {PRIORITY_LABELS[task.priority] ?? task.priority}
                        </span>
                      </div>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-white/40">
                      <span>{STATUS_LABELS[task.status] ?? task.status}</span>
                      <span>·</span>
                      <span>{formatDate(task.due_at)}</span>
                      {(task.dependency_count ?? 0) > 0 && (
                        <>
                          <span>·</span>
                          <span>{task.dependency_count} beroende</span>
                        </>
                      )}
                    </div>
                    <div className="mt-2 flex flex-wrap items-center gap-2">
                      <label htmlFor={`quick-${task.id}`} className="sr-only">
                        Ändra status för {task.title}
                      </label>
                      <select
                        id={`quick-${task.id}`}
                        value={task.status === "blocked" ? "todo" : task.status}
                        onChange={(event) => quickStatus(task, event.target.value)}
                        disabled={busy}
                        className="rounded border border-border bg-base px-2 py-1 text-xs disabled:opacity-50"
                      >
                        {STATUS_OPTIONS.map((status) => (
                          <option key={status} value={status}>
                            {STATUS_LABELS[status]}
                          </option>
                        ))}
                      </select>
                      <button
                        type="button"
                        onClick={() => openTask(task.id)}
                        className="text-xs text-accent2 hover:underline"
                      >
                        Öppna detalj
                      </button>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </section>

        <section aria-labelledby="task-detail" className="rounded-xl border border-border bg-panel p-4">
          <h2 id="task-detail" className="text-sm font-medium text-white/70">
            Uppgiftsdetalj
          </h2>
          {detailLoading ? (
            <p role="status" className="mt-3 text-sm text-white/40">
              Läser uppgiften…
            </p>
          ) : !selected || !edit ? (
            <p className="mt-3 text-sm text-white/30">Välj en uppgift för att se historik och beroenden.</p>
          ) : (
            <div className="mt-3 space-y-4">
              <div className="rounded-lg border border-accent/20 bg-accent/5 p-3 text-xs text-white/55">
                <span className="font-medium text-white/75">Användaruppgift</span> · exekveringsbehörighet:{" "}
                <span className="font-mono">none</span>. MainAI-jobb är separata och startas inte härifrån.
              </div>

              <form onSubmit={saveTask} className="space-y-3">
                <div>
                  <label htmlFor="detail-title" className="mb-1 block text-xs text-white/40">
                    Titel
                  </label>
                  <input
                    id="detail-title"
                    required
                    value={edit.title}
                    onChange={(event) => setEdit({ ...edit, title: event.target.value })}
                    className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
                  />
                </div>
                <div>
                  <label htmlFor="detail-description" className="mb-1 block text-xs text-white/40">
                    Beskrivning
                  </label>
                  <textarea
                    id="detail-description"
                    value={edit.description}
                    onChange={(event) => setEdit({ ...edit, description: event.target.value })}
                    rows={3}
                    className="w-full resize-none rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
                  />
                </div>
                <div className="grid gap-2 sm:grid-cols-2">
                  <div>
                    <label htmlFor="detail-status" className="mb-1 block text-xs text-white/40">
                      Status
                    </label>
                    <select
                      id="detail-status"
                      value={edit.status === "blocked" ? "todo" : edit.status}
                      onChange={(event) => setEdit({ ...edit, status: event.target.value })}
                      className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
                    >
                      {STATUS_OPTIONS.map((status) => (
                        <option key={status} value={status}>
                          {STATUS_LABELS[status]}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label htmlFor="detail-priority" className="mb-1 block text-xs text-white/40">
                      Prioritet
                    </label>
                    <select
                      id="detail-priority"
                      value={edit.priority}
                      onChange={(event) => setEdit({ ...edit, priority: event.target.value })}
                      className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
                    >
                      {PRIORITY_OPTIONS.map((priority) => (
                        <option key={priority} value={priority}>
                          {PRIORITY_LABELS[priority]}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label htmlFor="detail-due" className="mb-1 block text-xs text-white/40">
                      Deadline
                    </label>
                    <input
                      id="detail-due"
                      type="datetime-local"
                      value={edit.due_at}
                      onChange={(event) => setEdit({ ...edit, due_at: event.target.value })}
                      className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
                    />
                  </div>
                  <div>
                    <label htmlFor="detail-milestone" className="mb-1 block text-xs text-white/40">
                      Milstolpe
                    </label>
                    <select
                      id="detail-milestone"
                      value={edit.milestone_id}
                      onChange={(event) => setEdit({ ...edit, milestone_id: event.target.value })}
                      disabled={!selected.project_id}
                      className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm disabled:opacity-50"
                    >
                      <option value="">Ingen milstolpe</option>
                      {editMilestones.map((milestone) => (
                        <option key={milestone.id} value={milestone.id}>
                          {milestone.title}
                        </option>
                      ))}
                    </select>
                  </div>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="submit"
                    disabled={busy}
                    className="rounded-lg bg-accent px-3 py-2 text-sm disabled:opacity-50"
                  >
                    {busy ? "Sparar…" : "Spara ändringar"}
                  </button>
                  <button
                    type="button"
                    onClick={deleteTask}
                    disabled={busy}
                    className="rounded-lg border border-red-500/30 px-3 py-2 text-sm text-red-300 disabled:opacity-50"
                  >
                    Ta bort
                  </button>
                </div>
              </form>

              {selected.is_blocked && (
                <div className="rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
                  Uppgiften är blockerad av ett beroende som ännu inte är klart.
                  {selected.blocked_reason ? ` ${selected.blocked_reason}` : ""}
                </div>
              )}

              <div className="rounded-lg border border-border/60 p-3">
                <h3 className="text-xs font-medium uppercase tracking-wide text-white/50">Beroenden</h3>
                <ul className="mt-2 space-y-1 text-sm">
                  {selected.dependencies.map((dependency) => (
                    <li key={dependency.id} className="flex items-center justify-between gap-2">
                      <span className="min-w-0 truncate text-white/75">
                        {dependency.depends_on_title}{" "}
                        <span className="text-xs text-white/40">({dependency.depends_on_status})</span>
                      </span>
                      <button
                        type="button"
                        onClick={() => removeDependency(dependency.depends_on_task_id)}
                        disabled={busy}
                        className="shrink-0 text-xs text-white/40 hover:text-red-300 disabled:opacity-50"
                      >
                        Ta bort
                      </button>
                    </li>
                  ))}
                  {selected.dependencies.length === 0 && (
                    <li className="text-sm text-white/30">Inga beroenden.</li>
                  )}
                </ul>
                <div className="mt-2 flex gap-2">
                  <label htmlFor="dependency-add" className="sr-only">
                    Lägg till beroende
                  </label>
                  <select
                    id="dependency-add"
                    value={dependencyToAdd}
                    onChange={(event) => setDependencyToAdd(event.target.value)}
                    className="min-w-0 flex-1 rounded-lg border border-border bg-base px-2 py-1.5 text-sm"
                  >
                    <option value="">Välj uppgift…</option>
                    {dependencyCandidates.map((task) => (
                      <option key={task.id} value={task.id}>
                        {task.title}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    onClick={addDependency}
                    disabled={busy || !dependencyToAdd}
                    className="rounded-lg border border-border px-3 py-1.5 text-xs text-white/70 disabled:opacity-50"
                  >
                    Lägg till
                  </button>
                </div>
                {selected.dependents.length > 0 && (
                  <p className="mt-2 text-xs text-white/40">
                    Blockerar: {selected.dependents.map((item) => item.depends_on_title).join(", ")}
                  </p>
                )}
              </div>

              <div className="rounded-lg border border-border/60 p-3">
                <h3 className="text-xs font-medium uppercase tracking-wide text-white/50">
                  Historik ({selected.events.length})
                </h3>
                <ul className="mt-2 space-y-1 text-xs text-white/45">
                  {selected.events.slice(0, 20).map((event) => (
                    <li key={event.id} className="flex flex-wrap justify-between gap-2">
                      <span>
                        {event.event_type}
                        {event.from_status && event.to_status
                          ? `: ${event.from_status} → ${event.to_status}`
                          : ""}
                      </span>
                      <span>{new Date(event.created_at).toLocaleString("sv-SE")}</span>
                    </li>
                  ))}
                  {selected.events.length === 0 && <li>Ingen historik ännu.</li>}
                </ul>
              </div>

              {selected.milestone && (
                <p className="text-xs text-white/45">
                  Milstolpe: <span className="text-white/70">{selected.milestone.title}</span> ·{" "}
                  {selected.milestone.completed_task_count}/{selected.milestone.task_count} uppgifter klara
                </p>
              )}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
