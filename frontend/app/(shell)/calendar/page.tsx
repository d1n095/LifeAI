"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  api,
  CalendarEventItem,
  MilestoneItem,
  ProjectItem,
  TaskItem,
} from "@/lib/api";

const CATEGORY_LABELS: Record<string, string> = {
  personal: "Personligt",
  work: "Arbete",
  school: "Skola",
};

const RECURRENCE_LABELS: Record<string, string> = {
  none: "Ingen upprepning",
  daily: "Dagligen",
  weekly: "Veckovis",
  monthly: "Månadsvis",
};

type EventForm = {
  title: string;
  starts_at: string;
  ends_at: string;
  category: string;
  timezone: string;
  recurrence_rule: string;
  recurrence_until: string;
  project_id: string;
  task_id: string;
  milestone_id: string;
  apply_to_series: boolean;
};

function pad(number: number): string {
  return String(number).padStart(2, "0");
}

function toLocalInput(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}`;
}

function toDateInput(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function defaultStart(): string {
  const date = new Date();
  date.setDate(date.getDate() + 1);
  date.setHours(10, 0, 0, 0);
  return toLocalInput(date.toISOString());
}

function addHour(value: string): string {
  if (!value) return "";
  const date = new Date(value);
  date.setHours(date.getHours() + 1);
  return toLocalInput(date.toISOString());
}

function isoDate(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function formatDateTime(value: string): string {
  return new Date(value).toLocaleString("sv-SE", {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

export default function CalendarPage() {
  const now = new Date();
  const [from, setFrom] = useState(isoDate(now));
  const [to, setTo] = useState(isoDate(new Date(now.getTime() + 30 * 24 * 60 * 60 * 1000)));
  const [categoryFilter, setCategoryFilter] = useState("");
  const [events, setEvents] = useState<CalendarEventItem[]>([]);
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [milestones, setMilestones] = useState<MilestoneItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [conflicts, setConflicts] = useState<CalendarEventItem[]>([]);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingVersion, setEditingVersion] = useState<number | null>(null);
  const [form, setForm] = useState<EventForm>({
    title: "",
    starts_at: defaultStart(),
    ends_at: addHour(defaultStart()),
    category: "work",
    timezone: "Europe/Stockholm",
    recurrence_rule: "none",
    recurrence_until: "",
    project_id: "",
    task_id: "",
    milestone_id: "",
    apply_to_series: false,
  });

  async function refresh() {
    setLoading(true);
    setError(null);
    try {
      const rows = await api.listCalendarEvents({
        starts_after: new Date(`${from}T00:00:00`).toISOString(),
        starts_before: new Date(`${to}T23:59:59`).toISOString(),
        category: categoryFilter || undefined,
      });
      setEvents(rows);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // Documented fetch-on-mount pattern used throughout the shell.
    Promise.all([api.listProjects(), api.listTasks()])
      .then(([projectRows, taskRows]) => {
        setProjects(projectRows);
        setTasks(taskRows);
      })
      .catch((e: any) => setError(e.message));
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!form.project_id) return;
    api
      .listMilestones(form.project_id)
      .then(setMilestones)
      .catch(() => setMilestones([]));
  }, [form.project_id]);

  function resetForm() {
    setEditingId(null);
    setEditingVersion(null);
    setConflicts([]);
    setForm({
      title: "",
      starts_at: defaultStart(),
      ends_at: addHour(defaultStart()),
      category: "work",
      timezone: "Europe/Stockholm",
      recurrence_rule: "none",
      recurrence_until: "",
      project_id: "",
      task_id: "",
      milestone_id: "",
      apply_to_series: false,
    });
  }

  function editEvent(event: CalendarEventItem) {
    setEditingId(event.id);
    setEditingVersion(event.version);
    setConflicts([]);
    setForm({
      title: event.title,
      starts_at: toLocalInput(event.starts_at),
      ends_at: toLocalInput(event.ends_at),
      category: event.category,
      timezone: event.timezone,
      recurrence_rule: event.recurrence_rule,
      recurrence_until: toDateInput(event.recurrence_until),
      project_id: event.project_id ?? "",
      task_id: event.task_id ?? "",
      milestone_id: event.milestone_id ?? "",
      apply_to_series: false,
    });
  }

  async function checkConflicts(): Promise<CalendarEventItem[]> {
    if (!form.starts_at) return [];
    try {
      const rows = await api.calendarConflicts({
        starts_at: new Date(form.starts_at).toISOString(),
        ends_at: form.ends_at ? new Date(form.ends_at).toISOString() : undefined,
        exclude_event_id: editingId ?? undefined,
      });
      setConflicts(rows);
      return rows;
    } catch (e: any) {
      setError(e.message);
      return [];
    }
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!form.title.trim() || busy) return;
    setBusy(true);
    setError(null);
    try {
      const found = await checkConflicts();
      if (found.length > 0) {
        const proceed = window.confirm(
          `Tiden krockar med ${found.length} befintligt evenemang. Spara ändå?`
        );
        if (!proceed) return;
      }
      if (editingId) {
        await api.updateCalendarEvent(editingId, {
          title: form.title.trim(),
          starts_at: new Date(form.starts_at).toISOString(),
          ends_at: form.ends_at ? new Date(form.ends_at).toISOString() : null,
          category: form.category as "personal" | "work" | "school",
          timezone: form.timezone,
          project_id: form.project_id || null,
          task_id: form.task_id || null,
          milestone_id: form.milestone_id || null,
          expected_version: editingVersion ?? undefined,
          apply_to_series: form.apply_to_series,
        });
      } else {
        await api.createCalendarEvent({
          title: form.title.trim(),
          starts_at: new Date(form.starts_at).toISOString(),
          ends_at: form.ends_at ? new Date(form.ends_at).toISOString() : null,
          category: form.category as "personal" | "work" | "school",
          timezone: form.timezone,
          recurrence_rule: form.recurrence_rule as "none" | "daily" | "weekly" | "monthly",
          recurrence_until:
            form.recurrence_rule !== "none" && form.recurrence_until
              ? new Date(`${form.recurrence_until}T23:59:59`).toISOString()
              : null,
          project_id: form.project_id || null,
          task_id: form.task_id || null,
          milestone_id: form.milestone_id || null,
        });
      }
      resetForm();
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function deleteEvent() {
    if (!editingId || busy) return;
    const scope = form.apply_to_series ? "hela serien" : "det här tillfället";
    if (!window.confirm(`Ta bort ${scope}?`)) return;
    setBusy(true);
    setError(null);
    try {
      await api.deleteCalendarEvent(editingId, form.apply_to_series);
      resetForm();
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const formTasks = form.project_id
    ? tasks.filter((task) => task.project_id === form.project_id)
    : tasks;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">Kalender</h1>
          <p className="mt-1 max-w-3xl text-sm text-white/50">
            Skapa, ändra och ta bort egna evenemang med tidszon, kategori och upprepning.
            Koppla dem till projekt, uppgifter och milstolpar. Inga externa kalendrar används och
            inga ändringar sker automatiskt.
          </p>
        </div>
        <Link href="/tasks" className="rounded-lg border border-border px-3 py-2 text-sm text-white/60 hover:text-white">
          Uppgifter & deadlines →
        </Link>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      <section aria-labelledby="calendar-create" className="rounded-xl border border-border bg-panel p-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h2 id="calendar-create" className="text-sm font-medium text-white/70">
            {editingId ? "Redigera evenemang" : "Nytt evenemang"}
          </h2>
          {editingId && (
            <button type="button" onClick={resetForm} className="text-xs text-white/50 hover:text-white">
              Avbryt redigering
            </button>
          )}
        </div>
        <form onSubmit={submit} className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          <div className="md:col-span-2">
            <label htmlFor="event-title" className="mb-1 block text-xs text-white/40">
              Titel
            </label>
            <input
              id="event-title"
              required
              value={form.title}
              onChange={(event) => setForm({ ...form, title: event.target.value })}
              placeholder="Vad händer?"
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
          <div>
            <label htmlFor="event-category" className="mb-1 block text-xs text-white/40">
              Kategori
            </label>
            <select
              id="event-category"
              value={form.category}
              onChange={(event) => setForm({ ...form, category: event.target.value })}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            >
              {Object.entries(CATEGORY_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="event-timezone" className="mb-1 block text-xs text-white/40">
              Tidszon
            </label>
            <input
              id="event-timezone"
              value={form.timezone}
              onChange={(event) => setForm({ ...form, timezone: event.target.value })}
              placeholder="Europe/Stockholm"
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
          </div>
          <div>
            <label htmlFor="event-start" className="mb-1 block text-xs text-white/40">
              Start
            </label>
            <input
              id="event-start"
              required
              type="datetime-local"
              value={form.starts_at}
              onChange={(event) =>
                setForm({
                  ...form,
                  starts_at: event.target.value,
                  ends_at: form.ends_at ? addHour(event.target.value) : "",
                })
              }
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label htmlFor="event-end" className="mb-1 block text-xs text-white/40">
              Slut
            </label>
            <input
              id="event-end"
              type="datetime-local"
              value={form.ends_at}
              onChange={(event) => setForm({ ...form, ends_at: event.target.value })}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label htmlFor="event-project" className="mb-1 block text-xs text-white/40">
              Projekt
            </label>
            <select
              id="event-project"
              value={form.project_id}
              onChange={(event) => {
                setMilestones([]);
                setForm({ ...form, project_id: event.target.value, task_id: "", milestone_id: "" });
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
            <label htmlFor="event-task" className="mb-1 block text-xs text-white/40">
              Uppgift
            </label>
            <select
              id="event-task"
              value={form.task_id}
              onChange={(event) => setForm({ ...form, task_id: event.target.value })}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
            >
              <option value="">Ingen uppgift</option>
              {formTasks.map((task) => (
                <option key={task.id} value={task.id}>
                  {task.title}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label htmlFor="event-milestone" className="mb-1 block text-xs text-white/40">
              Milstolpe
            </label>
            <select
              id="event-milestone"
              value={form.milestone_id}
              onChange={(event) => setForm({ ...form, milestone_id: event.target.value })}
              disabled={!form.project_id}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm disabled:opacity-50"
            >
              <option value="">Ingen milstolpe</option>
              {milestones.map((milestone) => (
                <option key={milestone.id} value={milestone.id}>
                  {milestone.title}
                </option>
              ))}
            </select>
          </div>
          {!editingId && (
            <>
              <div>
                <label htmlFor="event-recurrence" className="mb-1 block text-xs text-white/40">
                  Upprepning
                </label>
                <select
                  id="event-recurrence"
                  value={form.recurrence_rule}
                  onChange={(event) => setForm({ ...form, recurrence_rule: event.target.value })}
                  className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm"
                >
                  {Object.entries(RECURRENCE_LABELS).map(([value, label]) => (
                    <option key={value} value={value}>
                      {label}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor="event-until" className="mb-1 block text-xs text-white/40">
                  Upprepa till
                </label>
                <input
                  id="event-until"
                  type="date"
                  value={form.recurrence_until}
                  disabled={form.recurrence_rule === "none"}
                  onChange={(event) => setForm({ ...form, recurrence_until: event.target.value })}
                  className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm disabled:opacity-50"
                />
              </div>
            </>
          )}
          {editingId && (
            <label className="flex items-end gap-2 pb-2 text-xs text-white/60">
              <input
                type="checkbox"
                checked={form.apply_to_series}
                onChange={(event) => setForm({ ...form, apply_to_series: event.target.checked })}
              />
              Gäller hela serien
            </label>
          )}
          <div className="flex items-end gap-2 md:col-span-2 xl:col-span-4">
            <button
              type="button"
              onClick={checkConflicts}
              className="rounded-lg border border-border px-3 py-2 text-sm text-white/70 hover:text-white"
            >
              Kontrollera konflikter
            </button>
            <button
              type="submit"
              disabled={busy || !form.title.trim()}
              className="rounded-lg bg-accent px-4 py-2 text-sm disabled:opacity-50"
            >
              {busy ? "Sparar…" : editingId ? "Spara ändringar" : "Skapa evenemang"}
            </button>
            {editingId && (
              <button
                type="button"
                onClick={deleteEvent}
                disabled={busy}
                className="rounded-lg border border-red-500/30 px-3 py-2 text-sm text-red-300 disabled:opacity-50"
              >
                Ta bort
              </button>
            )}
          </div>
        </form>
        {conflicts.length > 0 && (
          <div role="status" className="mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 p-3 text-xs text-amber-200">
            <div className="font-medium">Tidskonflikt med {conflicts.length} evenemang:</div>
            <ul className="mt-1 space-y-0.5">
              {conflicts.map((conflict) => (
                <li key={conflict.id}>
                  {conflict.title} · {formatDateTime(conflict.starts_at)}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <section aria-labelledby="calendar-range" className="rounded-xl border border-border bg-panel p-4">
        <div className="flex flex-wrap items-end gap-3">
          <h2 id="calendar-range" className="mr-auto text-sm font-medium text-white/70">
            Evenemang
          </h2>
          <div>
            <label htmlFor="range-from" className="mb-1 block text-xs text-white/40">
              Från
            </label>
            <input
              id="range-from"
              type="date"
              value={from}
              onChange={(event) => setFrom(event.target.value)}
              className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label htmlFor="range-to" className="mb-1 block text-xs text-white/40">
              Till
            </label>
            <input
              id="range-to"
              type="date"
              value={to}
              onChange={(event) => setTo(event.target.value)}
              className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
            />
          </div>
          <div>
            <label htmlFor="range-category" className="mb-1 block text-xs text-white/40">
              Kategori
            </label>
            <select
              id="range-category"
              value={categoryFilter}
              onChange={(event) => setCategoryFilter(event.target.value)}
              className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
            >
              <option value="">Alla</option>
              {Object.entries(CATEGORY_LABELS).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            onClick={refresh}
            className="rounded-lg border border-border px-3 py-2 text-sm text-white/70 hover:text-white"
          >
            Visa
          </button>
        </div>

        {loading ? (
          <p role="status" className="mt-4 text-sm text-white/40">
            Läser kalendern…
          </p>
        ) : events.length === 0 ? (
          <div className="mt-4 rounded-lg border border-dashed border-border p-8 text-center text-sm text-white/40">
            Inga evenemang i perioden. Skapa ett ovan eller välj en bredare period.
          </div>
        ) : (
          <ul className="mt-4 space-y-2">
            {events.map((event) => (
              <li
                key={event.id}
                className={`rounded-lg border px-3 py-2 text-sm ${
                  editingId === event.id ? "border-accent/60 bg-accent/5" : "border-border/60"
                }`}
              >
                <div className="flex flex-wrap items-start justify-between gap-2">
                  <button
                    type="button"
                    onClick={() => editEvent(event)}
                    className="min-w-0 text-left text-white/85 hover:text-accent"
                  >
                    {event.title}
                  </button>
                  <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                    <span className="rounded-full border border-border px-2 py-0.5 text-[10px] text-white/50">
                      {CATEGORY_LABELS[event.category] ?? event.category}
                    </span>
                    {event.series_id && (
                      <span className="rounded-full border border-accent/30 bg-accent/10 px-2 py-0.5 text-[10px] text-accent2">
                        Serie #{event.recurrence_index + 1}
                      </span>
                    )}
                  </div>
                </div>
                <div className="mt-1 text-xs text-white/45">
                  {formatDateTime(event.starts_at)}
                  {event.ends_at ? ` – ${formatDateTime(event.ends_at)}` : ""} · {event.timezone}
                </div>
                {(event.project_id || event.task_id || event.milestone_id) && (
                  <div className="mt-1 text-xs text-white/40">
                    {event.project_id ? "Projekt kopplat" : ""}
                    {event.task_id ? " · Uppgift kopplad" : ""}
                    {event.milestone_id ? " · Milstolpe kopplad" : ""}
                  </div>
                )}
                <div className="mt-2 flex gap-3 text-xs">
                  <button type="button" onClick={() => editEvent(event)} className="text-accent2 hover:underline">
                    Redigera
                  </button>
                  <span className="text-white/25">v{event.version}</span>
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
