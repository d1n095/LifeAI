"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, AuthError, CurrentUser } from "@/lib/api";

type Props = {
  children: (user: CurrentUser) => React.ReactNode;
};

export default function AuthGuard({ children }: Props) {
  const router = useRouter();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    // There's no client-readable token to check up front (see docs/AUTH_THREAT_MODEL.md) —
    // the only way to know if a session exists is to ask the backend, which reads the
    // HttpOnly cookie itself. api.me() also transparently attempts a refresh on a 401
    // before giving up (see lib/api.ts), so a merely-expired access token doesn't force a
    // login round-trip here.
    api
      .me()
      .then((me) => setUser(me))
      .catch((err) => {
        if (!(err instanceof AuthError)) {
          router.replace("/login");
        }
        // AuthError already redirected inside lib/api.ts
      })
      .finally(() => setChecked(true));
  }, [router]);

  if (!checked || !user) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-base" role="status" aria-live="polite">
        <span className="text-sm text-white/40">Kontrollerar inloggning…</span>
      </div>
    );
  }

  return <>{children(user)}</>;
}
