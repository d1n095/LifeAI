"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, KnowledgeSourceItem, ProjectItem, WorkbenchAnalysis, WorkbenchLabel } from "@/lib/api";
import ConfidenceBadge from "@/components/ConfidenceBadge";

const LABELS: { value: WorkbenchLabel; label: string }[] = [
  { value: "idea", label: "Idé" },
  { value: "proposal", label: "Förslag" },
  { value: "decision", label: "Beslut" },
  { value: "history", label: "Historik" },
];

export default function WorkbenchPage() {
  const [projects, setProjects] = useState<ProjectItem[]>([]);
  const [sources, setSources] = useState<KnowledgeSourceItem[]>([]);
  const [projectId, setProjectId] = useState("");
  const [documentId, setDocumentId] = useState("");
  const [question, setQuestion] = useState("");

  const [analyzing, setAnalyzing] = useState(false);
  const [result, setResult] = useState<WorkbenchAnalysis | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [label, setLabel] = useState<WorkbenchLabel>("idea");
  const [saving, setSaving] = useState(false);
  const [savedId, setSavedId] = useState<string | null>(null);

  const [taskTitle, setTaskTitle] = useState("");
  const [creatingTask, setCreatingTask] = useState(false);
  const [taskCreated, setTaskCreated] = useState(false);

  useEffect(() => {
    api.listProjects().then(setProjects).catch(() => {});
    api.listLibrary().then(setSources).catch(() => {});
  }, []);

  async function analyze() {
    if (!question.trim() || analyzing) return;
    setAnalyzing(true);
    setError(null);
    setResult(null);
    setSavedId(null);
    setTaskCreated(false);
    try {
      setResult(await api.analyzeWorkbench(question.trim(), projectId || undefined, documentId || undefined));
    } catch (e: any) {
      setError(e.message);
    } finally {
      setAnalyzing(false);
    }
  }

  async function save() {
    if (!result || saving) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await api.saveWorkbenchResult({
        question: result.question,
        conclusion: result.conclusion,
        critique: result.critique,
        label,
        project_id: projectId || undefined,
        source_document_ids: result.sources.map((s) => s.document_id),
      });
      setSavedId(saved.id);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  async function createFollowUpTask() {
    if (!taskTitle.trim() || creatingTask) return;
    setCreatingTask(true);
    setError(null);
    try {
      await api.createTask({ title: taskTitle.trim(), project_id: projectId || null });
      setTaskCreated(true);
      setTaskTitle("");
    } catch (e: any) {
      setError(e.message);
    } finally {
      setCreatingTask(false);
    }
  }

  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <h1 className="text-xl font-semibold">Founder Workbench</h1>
        <p className="text-white/50 text-sm mt-1">
          Ställ en fråga mot ett projekt eller en specifik källa, granska MainAI:s slutsats och kritik, och spara
          resultatet som ett nytt kunskapsobjekt.
        </p>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      <div className="rounded-xl border border-border bg-panel p-4 space-y-3">
        <div className="flex gap-2 flex-wrap">
          <label className="sr-only" htmlFor="wb-project">Projekt</label>
          <select
            id="wb-project"
            value={projectId}
            onChange={(e) => setProjectId(e.target.value)}
            className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
          >
            <option value="">Alla projekt</option>
            {projects.map((p) => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>

          <label className="sr-only" htmlFor="wb-document">Källa</label>
          <select
            id="wb-document"
            value={documentId}
            onChange={(e) => setDocumentId(e.target.value)}
            className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
          >
            <option value="">Alla källor</option>
            {sources.map((s) => (
              <option key={s.id} value={s.id}>{s.title}</option>
            ))}
          </select>
        </div>

        <label htmlFor="wb-question" className="sr-only">Fråga till analysen</label>
        <textarea
          id="wb-question"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Vad vill du att MainAI ska analysera?"
          rows={3}
          className="w-full rounded-lg border border-border bg-base px-4 py-3 text-sm outline-none focus:border-accent"
        />
        <button
          onClick={analyze}
          disabled={analyzing || !question.trim()}
          className="rounded-lg bg-accent px-5 py-2.5 text-sm font-medium disabled:opacity-50"
        >
          {analyzing ? "Analyserar…" : "Starta analys"}
        </button>
      </div>

      {result && (
        <div className="rounded-xl border border-border bg-panel p-4 space-y-4">
          <div>
            <h2 className="text-sm font-medium text-white/70 mb-1">Fråga</h2>
            <p className="text-sm">{result.question}</p>
          </div>

          <div>
            <div className="flex items-center gap-2 mb-1">
              <h2 className="text-sm font-medium text-white/70">Slutsats</h2>
              <ConfidenceBadge confidence={result.confidence} score={result.confidence_score} />
              {result.conflicts_detected && (
                <span className="text-[11px] rounded bg-red-500/20 text-red-300 px-2 py-0.5">
                  Motstridiga källor upptäckta
                </span>
              )}
            </div>
            <p className="text-sm whitespace-pre-wrap">{result.conclusion}</p>
          </div>

          {result.critique && (
            <div>
              <h2 className="text-sm font-medium text-white/70 mb-1">Kritik / alternativ</h2>
              <p className="text-sm whitespace-pre-wrap text-white/70">{result.critique}</p>
            </div>
          )}

          <div>
            <h2 className="text-sm font-medium text-white/70 mb-1">Källor</h2>
            {result.sources.length === 0 && <p className="text-sm text-white/30">Ingen relevant källa hittades.</p>}
            <div className="space-y-1">
              {result.sources.map((s, i) => (
                <Link
                  key={i}
                  href={`/library/${s.document_id}`}
                  className="block text-sm pl-2 border-l border-border hover:border-accent hover:text-white/90"
                >
                  {s.title} ({s.score.toFixed(2)})
                  {s.active_truth_status && s.active_truth_status !== "active" && (
                    <span className="ml-1 text-amber-300/70">[{s.active_truth_status}]</span>
                  )}
                </Link>
              ))}
            </div>
          </div>

          <div className="border-t border-border pt-4 space-y-2">
            <h2 className="text-sm font-medium text-white/70">Spara resultatet</h2>
            <div className="flex gap-2 flex-wrap items-center">
              <label className="sr-only" htmlFor="wb-label">Märk som</label>
              <select
                id="wb-label"
                value={label}
                onChange={(e) => setLabel(e.target.value as WorkbenchLabel)}
                className="rounded-lg border border-border bg-base px-3 py-2 text-sm"
              >
                {LABELS.map((l) => (
                  <option key={l.value} value={l.value}>{l.label}</option>
                ))}
              </select>
              <button
                onClick={save}
                disabled={saving || !!savedId}
                className="rounded-lg border border-border px-4 py-2 text-sm disabled:opacity-50"
              >
                {savedId ? "Sparat" : saving ? "Sparar…" : "Spara som kunskapsobjekt"}
              </button>
              {savedId && (
                <Link href={`/library/${savedId}`} className="text-sm text-accent hover:underline">
                  Öppna det sparade objektet
                </Link>
              )}
            </div>
          </div>

          <div className="border-t border-border pt-4 space-y-2">
            <h2 className="text-sm font-medium text-white/70">Skapa uppföljningsuppgift</h2>
            <div className="flex gap-2 flex-wrap">
              <label className="sr-only" htmlFor="wb-task-title">Uppgiftens titel</label>
              <input
                id="wb-task-title"
                value={taskTitle}
                onChange={(e) => setTaskTitle(e.target.value)}
                placeholder="T.ex. Verifiera slutsatsen med teamet"
                className="flex-1 min-w-[200px] rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
              />
              <button
                onClick={createFollowUpTask}
                disabled={creatingTask || !taskTitle.trim()}
                className="rounded-lg border border-border px-4 py-2 text-sm disabled:opacity-50"
              >
                {creatingTask ? "Skapar…" : "Skapa uppgift"}
              </button>
            </div>
            {taskCreated && <p className="text-xs text-emerald-300">Uppgiften skapades i projektlistan.</p>}
          </div>
        </div>
      )}
    </div>
  );
}
