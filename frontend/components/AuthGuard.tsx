"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api, AuthError, CurrentUser } from "@/lib/api";
import { clearToken, getToken } from "@/lib/auth";

type Props = {
  children: (user: CurrentUser) => React.ReactNode;
};

export default function AuthGuard({ children }: Props) {
  const router = useRouter();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    api
      .me()
      .then((me) => setUser(me))
      .catch((err) => {
        if (!(err instanceof AuthError)) {
          clearToken();
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
