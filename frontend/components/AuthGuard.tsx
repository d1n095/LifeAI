"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, AuthError, CurrentUser, NetworkError } from "@/lib/api";

type Props = {
  children: (user: CurrentUser) => React.ReactNode;
};

export default function AuthGuard({ children }: Props) {
  const router = useRouter();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [checked, setChecked] = useState(false);
  const [unreachable, setUnreachable] = useState(false);

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
        if (cancelled) return;
        if (err instanceof NetworkError) {
          // A down backend is not a logged-out session. Stay here and say so.
          setUnreachable(true);
          return;
        }
        if (!(err instanceof AuthError)) {
          setUnreachable(true);
        }
        // AuthError already redirected inside lib/api.ts
      })
      .finally(() => {
        if (!cancelled) setChecked(true);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  if (!checked || (!user && !unreachable)) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base" role="status" aria-live="polite">
        <span className="text-sm text-white/40">Kontrollerar inloggning…</span>
      </div>
    );
  }

  if (unreachable && !user) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base p-6">
        <div role="alert" className="max-w-md rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-200">
          Kunde inte nå servern. Sessionen antas inte vara utloggad. Försök igen när API:et svarar.
        </div>
      </div>
    );
  }

  if (!user) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base" role="status" aria-live="polite">
        <span className="text-sm text-white/40">Kontrollerar inloggning…</span>
      </div>
    );
  }

  return <>{children(user)}</>;
}
