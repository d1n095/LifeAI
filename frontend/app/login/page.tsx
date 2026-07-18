"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { api } from "@/lib/api";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [unverified, setUnverified] = useState(false);
  const [resendStatus, setResendStatus] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    // No client-readable token to check (see docs/AUTH_THREAT_MODEL.md) — ask the backend
    // directly. A failure here just means "not logged in", which is the expected state for
    // anyone actually looking at this page, so it's silently ignored.
    api
      .me()
      .then(() => router.replace("/"))
      .catch(() => {});
  }, [router]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setUnverified(false);
    setResendStatus(null);
    setLoading(true);
    try {
      // Login sets the session entirely via Set-Cookie (HttpOnly access/refresh tokens) —
      // the response body carries only non-sensitive user info plus the CSRF value, nothing
      // this code needs to store beyond what lib/api.ts already captures.
      await api.login(email, password);
      router.replace("/");
    } catch (err: any) {
      const status = err?.status;
      if (status === 403) {
        // Correct password, just not verified yet — a distinct case from "wrong password"
        // (see backend app/routers/auth.py), offered a way forward instead of a dead end.
        setUnverified(true);
        setError(err.message);
      } else if (status === 429) {
        setError(err.message);
      } else {
        setError("Fel e-post eller lösenord.");
      }
    } finally {
      setLoading(false);
    }
  }

  async function handleResend() {
    setResendStatus(null);
    try {
      const res = await api.resendVerification(email);
      setResendStatus(res.detail);
    } catch (err: any) {
      setResendStatus(err?.message || "Något gick fel. Försök igen.");
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm rounded-2xl border border-border bg-panel p-8"
        aria-label="Inloggning"
      >
        <div className="mb-6 text-center">
          <div className="text-sm uppercase tracking-widest text-accent2">Life OS</div>
          <h1 className="mt-1 text-xl font-semibold">Logga in på MainAI</h1>
        </div>

        <label htmlFor="email" className="mb-1 block text-sm text-white/60">
          E-post
        </label>
        <input
          id="email"
          type="email"
          required
          autoComplete="username"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className="mb-4 w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
        />

        <label htmlFor="password" className="mb-1 block text-sm text-white/60">
          Lösenord
        </label>
        <input
          id="password"
          type="password"
          required
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className="mb-4 w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-accent"
        />

        {error && (
          <div role="alert" className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 p-2 text-sm text-red-300">
            {error}
            {unverified && (
              <button type="button" onClick={handleResend} className="mt-2 block text-accent2 underline">
                Skicka bekräftelsemail igen
              </button>
            )}
          </div>
        )}

        {resendStatus && (
          <div role="status" className="mb-4 rounded-lg border border-border bg-base p-2 text-sm text-white/60">
            {resendStatus}
          </div>
        )}

        <button
          type="submit"
          disabled={loading}
          className="w-full rounded-lg bg-accent px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          {loading ? "Loggar in…" : "Logga in"}
        </button>

        <div className="mt-4 flex justify-between text-sm text-white/40">
          <Link href="/register" className="text-accent2 hover:underline">
            Skapa konto
          </Link>
          <Link href="/forgot-password" className="text-accent2 hover:underline">
            Glömt lösenord?
          </Link>
        </div>
      </form>
    </div>
  );
}
