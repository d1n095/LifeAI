"use client";

import { useEffect, useState } from "react";
import { api, ProviderStatus } from "@/lib/api";

const MODEL_SUGGESTIONS: Record<string, string> = {
  openai: "gpt-4o-mini",
  anthropic: "claude-sonnet-5",
  gemini: "gemini-2.5-flash",
  deepseek: "deepseek-chat",
  openrouter: "openrouter/auto",
  ollama: "llama3.1",
};

export default function AdminPage() {
  const [providers, setProviders] = useState<ProviderStatus[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);

  async function refresh() {
    try {
      setProviders(await api.providerStatus());
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  async function activate(role: "chat" | "embedding", provider: string) {
    setSaving(provider);
    try {
      await api.setProviderConfig(role, provider, MODEL_SUGGESTIONS[provider] || "");
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSaving(null);
    }
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-semibold">AI-adminpanel</h1>
      <p className="text-white/50 text-sm">
        Aktivera leverantör per roll. API-nycklar sätts som miljövariabler i backend (.env) — de
        visas eller lagras aldrig här.
      </p>

      {error && <div className="text-sm text-red-300">{error}</div>}

      <div className="rounded-xl border border-border overflow-hidden">
        <table className="w-full text-sm">
          <thead className="bg-panel text-white/50 text-xs uppercase">
            <tr>
              <th className="text-left px-4 py-3">Leverantör</th>
              <th className="text-left px-4 py-3">Konfigurerad</th>
              <th className="text-left px-4 py-3">Aktiv (chat)</th>
              <th className="text-left px-4 py-3">Aktiv (embedding)</th>
              <th className="px-4 py-3" />
            </tr>
          </thead>
          <tbody>
            {providers.map((p) => (
              <tr key={p.name} className="border-t border-border/60">
                <td className="px-4 py-3 capitalize">{p.name}</td>
                <td className="px-4 py-3">
                  <Dot ok={p.configured} />
                </td>
                <td className="px-4 py-3">
                  <Dot ok={p.active_chat} />
                </td>
                <td className="px-4 py-3">
                  <Dot ok={p.active_embedding} />
                </td>
                <td className="px-4 py-3 text-right space-x-2">
                  <button
                    disabled={!p.configured || saving === p.name}
                    onClick={() => activate("chat", p.name)}
                    className="text-xs rounded border border-border px-2 py-1 disabled:opacity-30"
                  >
                    Använd för chat
                  </button>
                  <button
                    disabled={!p.configured || saving === p.name}
                    onClick={() => activate("embedding", p.name)}
                    className="text-xs rounded border border-border px-2 py-1 disabled:opacity-30"
                  >
                    Använd för embedding
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Dot({ ok }: { ok: boolean }) {
  return <span className={`inline-block h-2.5 w-2.5 rounded-full ${ok ? "bg-emerald-400" : "bg-white/15"}`} />;
}
