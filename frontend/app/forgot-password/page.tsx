"use client";

import { useState } from "react";
import Link from "next/link";
import { api } from "@/lib/api";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [done, setDone] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      await api.forgotPassword(email);
      // Shown regardless of whether the address is registered — see
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
            Om e-postadressen finns registrerad har vi skickat instruktioner för att återställa lösenordet.
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
        className="w-full max-w-sm rounded-2xl border border-border bg-panel p-8"
        aria-label="Glömt lösenord"
      >
        <div className="mb-6 text-center">
          <div className="text-sm uppercase tracking-widest text-accent2">Life OS</div>
          <h1 className="mt-1 text-xl font-semibold">Återställ lösenord</h1>
          <p className="mt-2 text-sm text-white/60">
            Ange din e-postadress så skickar vi instruktioner för att välja ett nytt lösenord.
          </p>
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
          {loading ? "Skickar…" : "Skicka återställningslänk"}
        </button>

        <p className="mt-4 text-center text-sm text-white/40">
          <Link href="/login" className="text-accent2 hover:underline">
            Tillbaka till inloggning
          </Link>
        </p>
      </form>
    </div>
  );
}
