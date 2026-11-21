"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, AuthError, CurrentUser } from "@/lib/api";

type Props = {
  children: (user: CurrentUser) => React.ReactNode;
};

function sessionErrorMessage(err: unknown): string {
  return err instanceof Error && err.message ? err.message : "Kunde inte verifiera sessionen.";
}

export default function AuthGuard({ children }: Props) {
  const router = useRouter();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [checked, setChecked] = useState(false);
  // A failed session probe is not always an expired session. A temporary backend/network
  // failure must not send the founder to /login (and lose a perfectly valid HttpOnly
  // session); it gets a visible, retryable error instead. Only AuthError means "no session",
  // and lib/api.ts has already started that redirect.
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // There's no client-readable token to check up front (see docs/AUTH_THREAT_MODEL.md) —
    // the only way to know if a session exists is to ask the backend, which reads the
    // HttpOnly cookie itself. api.me() also transparently attempts a refresh on a 401
    // before giving up (see lib/api.ts), so a merely-expired access token doesn't force a
    // login round-trip here.
    let cancelled = false;
    api
      .me()
      .then((me) => {
        if (!cancelled) setUser(me);
      })
      .catch((err) => {
        if (!cancelled && !(err instanceof AuthError)) setError(sessionErrorMessage(err));
      })
      .finally(() => {
        if (!cancelled) setChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const retry = useCallback(async () => {
    setChecked(false);
    setError(null);
    try {
      setUser(await api.me());
    } catch (err) {
      if (!(err instanceof AuthError)) setError(sessionErrorMessage(err));
    } finally {
      setChecked(true);
    }
  }, []);

  if (checked && error) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base px-4" role="alert">
        <div className="w-full max-w-sm rounded-2xl border border-red-500/30 bg-panel p-6 text-sm">
          <div className="font-medium text-red-300">Kunde inte verifiera sessionen</div>
          <p className="mt-2 text-white/60">{error}</p>
          <button
            type="button"
            onClick={() => void retry()}
            className="mt-4 rounded-lg border border-border px-4 py-2 text-white/80 hover:bg-white/5"
          >
            Försök igen
          </button>
        </div>
      </div>
    );
  }

  if (!checked || !user) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base" role="status" aria-live="polite">
        <span className="text-sm text-white/40">Kontrollerar inloggning…</span>
      </div>
    );
  }

  return <>{children(user)}</>;
}
