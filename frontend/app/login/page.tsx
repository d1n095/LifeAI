"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { getToken, setToken } from "@/lib/auth";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (getToken()) router.replace("/");
  }, [router]);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const { access_token } = await api.login(email, password);
      setToken(access_token);
      router.replace("/");
    } catch (err: any) {
      setError("Fel e-post eller lösenord.");
    } finally {
      setLoading(false);
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
          </div>
        )}

        <button
          type="submit"
          disabled={loading}
          className="w-full rounded-lg bg-accent px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          {loading ? "Loggar in…" : "Logga in"}
        </button>
      </form>
    </div>
  );
}
