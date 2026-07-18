"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";

// useSearchParams() requires a Suspense boundary above it (Next.js App Router) so the page
// can still be statically prerendered.
export default function ResetPasswordPage() {
  return (
    <Suspense fallback={null}>
      <ResetPasswordInner />
    </Suspense>
  );
}

function ResetPasswordInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [token, setToken] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  useEffect(() => {
    const t = searchParams.get("token");
    // Reading the one-time token out of the URL at mount is exactly the documented
    // exception (see docs/SECURITY_BLOCKERS.md #1) — there's no external store to
    // subscribe to here, just a single value that must be captured once before the URL is
    // scrubbed on the next line.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setToken(t);
    // Scrub the token out of the visible URL / browser history — it only needs to survive
    // in memory long enough for the form submit below.
    router.replace("/reset-password");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (!token) {
      setError("Länken saknar en giltig token. Begär en ny återställning.");
      return;
    }
    if (password !== confirmPassword) {
      setError("Lösenorden stämmer inte överens.");
      return;
    }

    setLoading(true);
    try {
      await api.resetPassword(token, password);
      setDone(true);
    } catch (err: any) {
      setError(err?.message || "Något gick fel. Försök igen.");
    } finally {
      setLoading(false);
    }
  }

  if (done) {
    return (
      <div className="flex min-h-screen items-center justify-center px-4">
        <div className="w-full max-w-sm rounded-2xl border border-border bg-panel p-8 text-center" role="status">
          <div className="text-sm uppercase tracking-widest text-accent2">Life OS</div>
          <h1 className="mt-1 mb-4 text-xl font-semibold">Lösenordet är återställt</h1>
          <p className="text-sm text-white/60">
            Alla tidigare inloggade enheter har loggats ut. Logga in igen med ditt nya lösenord.
          </p>
          <Link href="/login" className="mt-6 inline-block rounded-lg bg-accent px-4 py-2 text-sm font-medium">
            Till inloggningen
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm rounded-2xl border border-border bg-panel p-8"
        aria-label="Välj nytt lösenord"
      >
        <div className="mb-6 text-center">
          <div className="text-sm uppercase tracking-widest text-accent2">Life OS</div>
          <h1 className="mt-1 text-xl font-semibold">Välj nytt lösenord</h1>
        </div>

        <label htmlFor="password" className="mb-1 block text-sm text-white/60">
          Nytt lösenord
        </label>
        <input
          id="password"
          type="password"
          required
          autoComplete="new-password"
          aria-describedby="password-hint"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
        />
        <p id="password-hint" className="mb-4 mt-1 text-xs text-white/40">
          Minst 12 tecken, med minst en bokstav och en siffra.
        </p>

        <label htmlFor="confirm-password" className="mb-1 block text-sm text-white/60">
          Bekräfta nytt lösenord
        </label>
        <input
          id="confirm-password"
          type="password"
          required
          autoComplete="new-password"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          className="mb-4 w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
        />

        {error && (
          <div role="alert" className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 p-2 text-sm text-red-300">
            {error}
          </div>
        )}

        <button
          type="submit"
          disabled={loading}
          className="w-full rounded-lg bg-accent px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          {loading ? "Sparar…" : "Spara nytt lösenord"}
        </button>
      </form>
    </div>
  );
}
