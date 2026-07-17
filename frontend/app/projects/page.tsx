"use client";

import { useEffect, useState } from "react";
import { api, ProjectItem, TaskItem } from "@/lib/api";

export default function ProjectsPage() {
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [newProject, setNewProject] = useState("");
  const [newTask, setNewTask] = useState("");
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const [p, t] = await Promise.all([api.listProjects(), api.listTasks()]);
      setProjects(p);
      setTasks(t);
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function addProject() {
    if (!newProject.trim()) return;
    await api.createProject({ name: newProject.trim(), status: "active" });
    setNewProject("");
    await refresh();
  }

  async function addTask() {
    if (!newTask.trim()) return;
    await api.createTask({ title: newTask.trim(), status: "todo", priority: "medium" });
    setNewTask("");
    await refresh();
  }

  async function toggleTask(task: TaskItem) {
    const status = task.status === "done" ? "todo" : "done";
    await api.updateTask(task.id, { ...task, status });
    await refresh();
  }

  return (
    <div className="space-y-8">
      <h1 className="text-xl font-semibold">Projekt & roadmap</h1>
      {error && <div className="text-sm text-red-300">{error}</div>}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="rounded-xl border border-border bg-panel p-4">
          <div className="flex justify-between items-center mb-3">
            <div className="text-sm font-medium">Projekt</div>
          </div>
          <div className="flex gap-2 mb-4">
            <input
              value={newProject}
              onChange={(e) => setNewProject(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addProject()}
              placeholder="Nytt projekt…"
              className="flex-1 rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
            <button onClick={addProject} className="rounded-lg bg-accent px-3 py-2 text-sm">
              Lägg till
            </button>
          </div>
          <div className="space-y-2">
            {projects.map((p) => (
              <div key={p.id} className="flex justify-between text-sm border-b border-border/60 py-2 last:border-0">
                <span>{p.name}</span>
                <span className="text-white/40">{p.status}</span>
              </div>
            ))}
            {projects.length === 0 && <div className="text-white/30 text-sm">Inga projekt ännu.</div>}
          </div>
        </div>

        <div className="rounded-xl border border-border bg-panel p-4">
          <div className="text-sm font-medium mb-3">Uppgifter</div>
          <div className="flex gap-2 mb-4">
            <input
              value={newTask}
              onChange={(e) => setNewTask(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && addTask()}
              placeholder="Ny uppgift…"
              className="flex-1 rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
            />
            <button onClick={addTask} className="rounded-lg bg-accent px-3 py-2 text-sm">
              Lägg till
            </button>
          </div>
          <div className="space-y-2">
            {tasks.map((t) => (
              <label key={t.id} className="flex items-center gap-3 text-sm border-b border-border/60 py-2 last:border-0">
                <input type="checkbox" checked={t.status === "done"} onChange={() => toggleTask(t)} />
                <span className={t.status === "done" ? "line-through text-white/30" : ""}>{t.title}</span>
                {t.suggested_by_ai && <span className="text-[10px] text-accent2">AI-förslag</span>}
              </label>
            ))}
            {tasks.length === 0 && <div className="text-white/30 text-sm">Inga uppgifter ännu.</div>}
          </div>
        </div>
      </div>
    </div>
  );
}
