"use client";

/* Life Library upload consolidation package (DEL 2): a global, persistent upload queue.
 * Mounted once in app/(shell)/layout.tsx (see UploadQueueProvider below) so its state
 * survives navigation between shell pages — leaving /library mid-upload must not lose the
 * queue, since the provider lives above the page, not inside it.
 *
 * At most MAX_CONCURRENT files are ever uploading/processing at once; the rest wait in a
 * plain FIFO array (queued items are simply the ones at the front of `items` not yet
 * started). Each item moves through: queued -> uploading -> received -> processing ->
 * completed | failed | cancelled. "completed" is only ever set from a real
 * GET /api/library/jobs/{id} response reaching a terminal server status — never assumed
 * client-side — so the UI can't show "done" before the server actually is.
 *
 * DEL 3 (server recovery): on mount, the provider also fetches the founder's still-active
 * (pending/running) server jobs and adds them as "processing" items, resuming polling — this
 * is what makes a page reload or fresh login not lose track of an upload still running
 * server-side. Recovered items have no local File (the browser never kept the original
 * bytes across a reload), so they cannot be retried — only queue items created via
 * enqueue() in this same session can be.
 */

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import { api, ImportJobItem } from "./api";

export type QueueItemStatus = "queued" | "uploading" | "received" | "processing" | "completed" | "failed" | "cancelled";

export interface UploadQueueItem {
  id: string;
  filename: string;
  status: QueueItemStatus;
  error: string | null;
  jobId: string | null;
  job: ImportJobItem | null;
  /** true for items reconstructed from GET /api/library/jobs (no local File — can't retry) */
  recovered: boolean;
}

interface UploadQueueContextValue {
  items: UploadQueueItem[];
  enqueue: (files: FileList | File[], projectId?: string) => void;
  retry: (id: string) => void;
  cancel: (id: string) => void;
  dismiss: (id: string) => void;
}

const MAX_CONCURRENT = 3;
const POLL_INTERVAL_MS = 700;
// ~4.5 minutes of polling per file before giving up and marking it failed — generous for a
// large ZIP package, but still bounded so a genuinely stuck job doesn't spin forever.
const POLL_MAX_ATTEMPTS = 400;

const UploadQueueContext = createContext<UploadQueueContextValue | null>(null);

export function useUploadQueue(): UploadQueueContextValue {
  const ctx = useContext(UploadQueueContext);
  if (!ctx) throw new Error("useUploadQueue måste användas inuti UploadQueueProvider.");
  return ctx;
}

let idCounter = 0;
function makeClientId(): string {
  idCounter += 1;
  return `upload-${Date.now()}-${idCounter}`;
}

function isActive(status: QueueItemStatus): boolean {
  return status === "uploading" || status === "received" || status === "processing";
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function UploadQueueProvider({ children }: { children: React.ReactNode }) {
  const [items, setItems] = useState<UploadQueueItem[]>([]);
  const filesRef = useRef(new Map<string, { file: File; projectId?: string }>());
  const cancelledRef = useRef(new Set<string>());
  const startedRef = useRef(new Set<string>());

  const patch = useCallback((id: string, changes: Partial<UploadQueueItem>) => {
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, ...changes } : it)));
  }, []);

  // pump() and runUpload()/pollJob() are mutually recursive (pump starts uploads, uploads
  // call pump again when they finish to fill the freed slot) — kept in refs so each can
  // reference the other's latest closure without a dependency-cycle in useCallback.
  const pumpRef = useRef<() => void>(() => {});

  const pollJob = useCallback(
    async (id: string, jobId: string) => {
      patch(id, { status: "processing" });
      for (let attempt = 0; attempt < POLL_MAX_ATTEMPTS; attempt++) {
        if (cancelledRef.current.has(id)) {
          patch(id, { status: "cancelled" });
          pumpRef.current();
          return;
        }
        try {
          const job = await api.getImportJob(jobId);
          patch(id, { job });
          if (job.status === "completed" || job.status === "partial") {
            patch(id, { status: "completed" });
            pumpRef.current();
            return;
          }
          if (job.status === "failed") {
            patch(id, { status: "failed", error: job.failure_reason || "Importen misslyckades." });
            pumpRef.current();
            return;
          }
        } catch {
          // A transient network hiccup while polling must not kill the whole queue item —
          // just try again next tick, same as the rest of the UI's fail-soft conventions.
        }
        await sleep(POLL_INTERVAL_MS);
      }
      patch(id, { status: "failed", error: "Tidsgränsen för statuskontroll överskreds." });
      pumpRef.current();
    },
    [patch]
  );

  const runUpload = useCallback(
    async (id: string) => {
      const entry = filesRef.current.get(id);
      if (!entry) return;
      if (cancelledRef.current.has(id)) {
        patch(id, { status: "cancelled" });
        pumpRef.current();
        return;
      }
      patch(id, { status: "uploading", error: null });
      let job: ImportJobItem;
      try {
        job = await api.importToLibrary(entry.file, entry.projectId);
      } catch (e: any) {
        if (cancelledRef.current.has(id)) {
          patch(id, { status: "cancelled" });
        } else {
          patch(id, { status: "failed", error: e.message || "Uppladdningen misslyckades." });
        }
        pumpRef.current();
        return;
      }
      if (cancelledRef.current.has(id)) {
        patch(id, { status: "cancelled", jobId: job.id, job });
        pumpRef.current();
        return;
      }
      patch(id, { status: "received", jobId: job.id, job });
      await pollJob(id, job.id);
    },
    [patch, pollJob]
  );

  const pump = useCallback(() => {
    setItems((prev) => {
      const activeCount = prev.filter((it) => isActive(it.status)).length;
      let slots = MAX_CONCURRENT - activeCount;
      if (slots <= 0) return prev;
      for (const it of prev) {
        if (slots <= 0) break;
        if (it.status === "queued" && !startedRef.current.has(it.id)) {
          startedRef.current.add(it.id);
          slots -= 1;
          runUpload(it.id);
        }
      }
      return prev;
    });
  }, [runUpload]);

  useEffect(() => {
    pumpRef.current = pump;
  }, [pump]);

  const enqueue = useCallback(
    (files: FileList | File[], projectId?: string) => {
      const list = Array.from(files);
      if (list.length === 0) return;
      setItems((prev) => [
        ...prev,
        ...list.map((file) => {
          const id = makeClientId();
          filesRef.current.set(id, { file, projectId });
          return {
            id,
            filename: file.name,
            status: "queued" as QueueItemStatus,
            error: null,
            jobId: null,
            job: null,
            recovered: false,
          };
        }),
      ]);
      pump();
    },
    [pump]
  );

  const retry = useCallback(
    (id: string) => {
      if (!filesRef.current.has(id)) return; // recovered items have no local file to retry with
      cancelledRef.current.delete(id);
      startedRef.current.delete(id);
      setItems((prev) => prev.map((it) => (it.id === id ? { ...it, status: "queued", error: null } : it)));
      pump();
    },
    [pump]
  );

  const cancel = useCallback(
    (id: string) => {
      cancelledRef.current.add(id);
      setItems((prev) => prev.map((it) => (it.id === id && it.status === "queued" ? { ...it, status: "cancelled" } : it)));
      pump();
    },
    [pump]
  );

  const dismiss = useCallback((id: string) => {
    setItems((prev) => prev.filter((it) => it.id !== id));
    filesRef.current.delete(id);
    startedRef.current.delete(id);
    cancelledRef.current.delete(id);
  }, []);

  useEffect(() => {
    let stopped = false;
    api
      .listImportJobs()
      .then((jobs) => {
        if (stopped) return;
        const active = jobs.filter((j) => j.status === "pending" || j.status === "running");
        if (active.length === 0) return;
        setItems((prev) => [
          ...prev,
          ...active.map((job) => ({
            id: makeClientId(),
            filename: job.source_filename || "Okänd fil",
            status: "processing" as QueueItemStatus,
            error: null,
            jobId: job.id,
            job,
            recovered: true,
          })),
        ]);
      })
      .catch(() => {
        // No server jobs to recover (or the request failed) — the queue simply starts empty,
        // same as before this recovery existed.
      });
    return () => {
      stopped = true;
    };
  }, []);

  // Recovered items are added to state fully formed (status "processing") but haven't had
  // pollJob() actually started for them yet — do that here, once, per recovered item.
  useEffect(() => {
    for (const it of items) {
      if (it.recovered && it.jobId && !startedRef.current.has(it.id) && isActive(it.status)) {
        startedRef.current.add(it.id);
        pollJob(it.id, it.jobId);
      }
    }
  }, [items, pollJob]);

  return <UploadQueueContext.Provider value={{ items, enqueue, retry, cancel, dismiss }}>{children}</UploadQueueContext.Provider>;
}
