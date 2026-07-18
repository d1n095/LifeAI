"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";

type Status = "verifying" | "success" | "error";

// useSearchParams() requires a Suspense boundary above it (Next.js App Router) so the page
// can still be statically prerendered — the actual token-reading logic lives in the inner
// component below.
export default function VerifyEmailPage() {
  return (
    <Suspense fallback={null}>
      <VerifyEmailInner />
    </Suspense>
  );
}

function VerifyEmailInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [status, setStatus] = useState<Status>("verifying");
  const [error, setError] = useState<string | null>(null);
  const ranOnce = useRef(false);

  useEffect(() => {
    if (ranOnce.current) return;
    ranOnce.current = true;

    const token = searchParams.get("token");
    // Scrub the token out of the visible URL / browser history immediately, before the
    // request even resolves — it's only ever needed once, right here.
    router.replace("/verify-email");

    if (!token) {
      // Synchronous mount-time state read, same documented exception as reset-password's
      // effect above (see docs/SECURITY_BLOCKERS.md #1) — nothing external to subscribe to.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setStatus("error");
      setError("Länken saknar en giltig token.");
      return;
    }

    api
      .verifyEmail(token)
      .then(() => setStatus("success"))
      .catch((err: any) => {
        setStatus("error");
        setError(err?.message || "Länken är ogiltig eller har gått ut.");
      });
    // Intentionally run once on mount only — router/searchParams identity churn must not
    // re-trigger a second verification attempt against an already-spent token.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div
        className="w-full max-w-sm rounded-2xl border border-border bg-panel p-8 text-center"
        role="status"
        aria-live="polite"
      >
        <div className="text-sm uppercase tracking-widest text-accent2">Life OS</div>

        {status === "verifying" && <p className="mt-4 text-sm text-white/60">Bekräftar din e-postadress…</p>}

        {status === "success" && (
          <>
            <h1 className="mt-1 mb-4 text-xl font-semibold">Kontot är bekräftat</h1>
            <p className="text-sm text-white/60">Du kan nu logga in.</p>
            <Link href="/login" className="mt-6 inline-block rounded-lg bg-accent px-4 py-2 text-sm font-medium">
              Till inloggningen
            </Link>
          </>
        )}

        {status === "error" && (
          <>
            <h1 className="mt-1 mb-4 text-xl font-semibold">Kunde inte bekräfta kontot</h1>
            <div role="alert" className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 p-2 text-sm text-red-300">
              {error}
            </div>
            <Link href="/login" className="text-sm text-accent2 hover:underline">
              Till inloggningen
            </Link>
          </>
        )}
      </div>
    </div>
  );
}
