"use client";

/* Life Library upload consolidation package (DEL 5): the shared two-step delete state
 * machine every delete button in the app should use, so "confirm, then delete, show 'Tar
 * bort…', block double-click, restore + show an error on failure" is written once instead
 * of once per page. Callers decide what actually happens on success/failure (remove a row,
 * navigate away, etc.) — this hook only owns the confirm/deleting/error state. */

import { useCallback, useRef, useState } from "react";

export interface TwoStepDelete {
  confirming: boolean;
  deleting: boolean;
  error: string | null;
  requestConfirm: () => void;
  cancelConfirm: () => void;
  confirmDelete: () => void;
}

export function useTwoStepDelete(onDelete: () => Promise<void>): TwoStepDelete {
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Belt-and-suspenders against double-click alongside the `deleting` state check below: a
  // ref is synchronous (no waiting for React to re-render with deleting=true), so two clicks
  // in the same event-loop tick can't both slip through before the first setState commits.
  const inFlight = useRef(false);

  const requestConfirm = useCallback(() => {
    setError(null);
    setConfirming(true);
  }, []);

  const cancelConfirm = useCallback(() => {
    setConfirming(false);
    setError(null);
  }, []);

  const confirmDelete = useCallback(() => {
    if (inFlight.current) return;
    inFlight.current = true;
    setDeleting(true);
    setError(null);
    onDelete()
      .catch((e: any) => {
        setError(e?.message || "Radering misslyckades.");
      })
      .finally(() => {
        inFlight.current = false;
        setDeleting(false);
      });
  }, [onDelete]);

  return { confirming, deleting, error, requestConfirm, cancelConfirm, confirmDelete };
}
