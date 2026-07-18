"use client";

import { useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";

export default function RegisterPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  // Honeypot: real users never see or fill this (visually hidden, aria-hidden, unreachable
  // by keyboard tab order). Anything filling it is almost certainly an automated submission
  // — see the backend comment in app/routers/auth.py for how it's handled.
  const [website, setWebsite] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (password !== confirmPassword) {
      setError("Lösenorden stämmer inte överens.");
      return;
    }

    setLoading(true);
    try {
      await api.register(email, password, website);
      // Always shown, regardless of whether the email was already registered — see
      // docs/AUTH_THREAT_MODEL.md for why the backend response is deliberately neutral.
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
          <h1 className="mt-1 mb-4 text-xl font-semibold">Kolla din e-post</h1>
          <p className="text-sm text-white/60">
            Om e-postadressen inte redan används har vi skickat ett bekräftelsemail. Klicka på länken i mejlet för
            att aktivera kontot.
          </p>
          <Link href="/login" className="mt-6 inline-block text-sm text-accent2 hover:underline">
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
        className="relative w-full max-w-sm rounded-2xl border border-border bg-panel p-8"
        aria-label="Skapa konto"
      >
        <div className="mb-6 text-center">
          <div className="text-sm uppercase tracking-widest text-accent2">Life OS</div>
          <h1 className="mt-1 text-xl font-semibold">Skapa konto på MainAI</h1>
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
          Bekräfta lösenord
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

        {/* Honeypot field: hidden from sighted users and screen readers alike, and never
            reachable via Tab — only a script that blindly fills every input would populate
            it. Clipped to zero size in place (not pushed off-screen with a large negative
            offset), which avoids expanding the page's scrollable area. Do not remove
            aria-hidden/tabIndex, they're what keeps real assistive-tech users from ever
            encountering it. */}
        <div
          className="absolute h-px w-px overflow-hidden whitespace-nowrap border-0 p-0"
          style={{ clip: "rect(0,0,0,0)", clipPath: "inset(50%)" }}
          aria-hidden="true"
        >
          <label htmlFor="website">Webbplats</label>
          <input
            id="website"
            name="website"
            type="text"
            tabIndex={-1}
            autoComplete="off"
            value={website}
            onChange={(e) => setWebsite(e.target.value)}
          />
        </div>

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
          {loading ? "Skapar konto…" : "Skapa konto"}
        </button>

        <p className="mt-4 text-center text-sm text-white/40">
          Har du redan ett konto?{" "}
          <Link href="/login" className="text-accent2 hover:underline">
            Logga in
          </Link>
        </p>
      </form>
    </div>
  );
}
