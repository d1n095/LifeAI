"use client";

import { useEffect, useState } from "react";
import { api, ProviderStatus, ProviderVerification, UsageSummaryRow } from "@/lib/api";

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
  const [usage, setUsage] = useState<UsageSummaryRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState<string | null>(null);
  // P1: keyed "provider:role" — which "Testa nu" button is mid-flight, so only that one
  // shows a busy state instead of disabling the whole table during a single check.
  const [verifying, setVerifying] = useState<string | null>(null);

  async function refresh() {
    try {
      const [p, u] = await Promise.all([api.providerStatus(), api.usageSummary()]);
      setProviders(p);
      setUsage(u);
    } catch (e: any) {
      setError(e.message);
    }
  }

  useEffect(() => {
    // See justification in app/(shell)/documents/page.tsx — documented fetch-on-mount
    // pattern, setState happens after an await inside refresh().
    // eslint-disable-next-line react-hooks/set-state-in-effect
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

  // P1: a real, minimal API call against the provider — not just a key-presence check.
  // Explicit and founder-initiated only; never run automatically for every provider.
  async function verifyNow(role: "chat" | "embedding", provider: string) {
    const key = `${provider}:${role}`;
    setVerifying(key);
    try {
      await api.verifyProvider(provider, role);
      await refresh();
    } catch (e: any) {
      setError(e.message);
    } finally {
      setVerifying(null);
    }
  }

  const totalKnownCost = usage.reduce((sum, row) => sum + (row.cost_usd ?? 0), 0);
  const hasUnknownCost = usage.some((row) => row.cost_usd === null);

  return (
    <div className="space-y-8">
      <h1 className="text-xl font-semibold">AI-adminpanel</h1>

      <section aria-labelledby="providers-heading" className="space-y-3">
        <h2 id="providers-heading" className="text-sm font-medium text-white/70">
          Leverantörer
        </h2>
        <p className="text-white/50 text-sm">
          Aktivera leverantör per roll. API-nycklar sätts som miljövariabler i backend (.env) —
          de visas eller lagras aldrig här.
        </p>

        {error && (
          <div role="alert" className="text-sm text-red-300">
            {error}
          </div>
        )}

        <div className="rounded-xl border border-border overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">Status och aktivering per AI-leverantör</caption>
            <thead className="bg-panel text-white/50 text-xs uppercase">
              <tr>
                <th scope="col" className="text-left px-4 py-3">
                  Leverantör
                </th>
                <th scope="col" className="text-left px-4 py-3">
                  Konfigurerad
                </th>
                <th scope="col" className="text-left px-4 py-3">
                  Aktiv (chat)
                </th>
                <th scope="col" className="text-left px-4 py-3">
                  Aktiv (embedding)
                </th>
                <th scope="col" className="text-left px-4 py-3">
                  Verifiering (chat)
                </th>
                <th scope="col" className="text-left px-4 py-3">
                  Verifiering (embedding)
                </th>
                <th scope="col" className="px-4 py-3">
                  <span className="sr-only">Åtgärder</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {providers.map((p) => (
                <tr key={p.name} className="border-t border-border/60">
                  <td className="px-4 py-3 capitalize">{p.name}</td>
                  <td className="px-4 py-3">
                    <Dot ok={p.configured} label={p.configured ? "Konfigurerad" : "Ej konfigurerad"} />
                  </td>
                  <td className="px-4 py-3">
                    <Dot ok={p.active_chat} label={p.active_chat ? "Aktiv för chat" : "Inte aktiv för chat"} />
                  </td>
                  <td className="px-4 py-3">
                    <Dot
                      ok={p.active_embedding}
                      label={p.active_embedding ? "Aktiv för embedding" : "Inte aktiv för embedding"}
                    />
                  </td>
                  <td className="px-4 py-3">
                    <VerificationBadge
                      verification={p.chat_verification}
                      onVerify={() => verifyNow("chat", p.name)}
                      busy={verifying === `${p.name}:chat`}
                    />
                  </td>
                  <td className="px-4 py-3">
                    <VerificationBadge
                      verification={p.embedding_verification}
                      onVerify={() => verifyNow("embedding", p.name)}
                      busy={verifying === `${p.name}:embedding`}
                    />
                  </td>
                  <td className="px-4 py-3 text-right space-x-2 whitespace-nowrap">
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
      </section>

      <section aria-labelledby="usage-heading" className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h2 id="usage-heading" className="text-sm font-medium text-white/70">
            Användning &amp; kostnad
          </h2>
          <div className="text-sm text-white/60">
            Totalt: <span className="font-medium text-white">${totalKnownCost.toFixed(4)}</span>
            {hasUnknownCost && <span className="text-white/40"> (+ okänd kostnad för vissa modeller)</span>}
          </div>
        </div>

        <div className="rounded-xl border border-border overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">Sammanställd token- och kostnadsanvändning per leverantör och modell</caption>
            <thead className="bg-panel text-white/50 text-xs uppercase">
              <tr>
                <th scope="col" className="text-left px-4 py-3">
                  Leverantör
                </th>
                <th scope="col" className="text-left px-4 py-3">
                  Modell
                </th>
                <th scope="col" className="text-left px-4 py-3">
                  Roll
                </th>
                <th scope="col" className="text-right px-4 py-3">
                  Anrop
                </th>
                <th scope="col" className="text-right px-4 py-3">
                  Prompt-tokens
                </th>
                <th scope="col" className="text-right px-4 py-3">
                  Svars-tokens
                </th>
                <th scope="col" className="text-right px-4 py-3">
                  Kostnad (USD)
                </th>
              </tr>
            </thead>
            <tbody>
              {usage.map((row, i) => (
                <tr key={i} className="border-t border-border/60">
                  <td className="px-4 py-3 capitalize">{row.provider}</td>
                  <td className="px-4 py-3 text-white/70">{row.model}</td>
                  <td className="px-4 py-3 text-white/50">{row.role}</td>
                  <td className="px-4 py-3 text-right">{row.request_count}</td>
                  <td className="px-4 py-3 text-right">{row.prompt_tokens.toLocaleString("sv-SE")}</td>
                  <td className="px-4 py-3 text-right">{row.completion_tokens.toLocaleString("sv-SE")}</td>
                  <td className="px-4 py-3 text-right">
                    {row.cost_usd === null ? <span className="text-white/30">okänd</span> : `$${row.cost_usd.toFixed(4)}`}
                  </td>
                </tr>
              ))}
              {usage.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-6 text-center text-white/30">
                    Ingen användning registrerad ännu.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <p className="text-xs text-white/30">
          Kostnader är uppskattade utifrån ungefärliga listpriser (se backend/app/providers/pricing.py) —
          inte fakturagrundande. Okänd betyder att modellen saknar prisdata ännu.
        </p>
      </section>
    </div>
  );
}

function Dot({ ok, label }: { ok: boolean; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`inline-block h-2.5 w-2.5 rounded-full ${ok ? "bg-emerald-400" : "bg-white/15"}`} aria-hidden="true" />
      <span className="sr-only">{label}</span>
    </span>
  );
}

const VERIFICATION_LABELS: Record<ProviderVerification["result"], string> = {
  ok: "Verifierad",
  invalid_key: "Ogiltig nyckel",
  unreachable: "Ej nåbar",
  rate_limited: "Rate-limited",
  unsupported: "Stöds inte för rollen",
  not_configured: "Ingen nyckel",
};

const VERIFICATION_COLORS: Record<ProviderVerification["result"], string> = {
  ok: "bg-emerald-500/20 text-emerald-300",
  invalid_key: "bg-red-500/20 text-red-300",
  unreachable: "bg-amber-500/20 text-amber-300",
  rate_limited: "bg-amber-500/20 text-amber-300",
  unsupported: "bg-white/10 text-white/50",
  not_configured: "bg-white/10 text-white/50",
};

function VerificationBadge({
  verification,
  onVerify,
  busy,
}: {
  verification: ProviderVerification | null;
  onVerify: () => void;
  busy: boolean;
}) {
  return (
    <div className="flex items-center gap-2">
      {verification ? (
        <span
          title={`${verification.message} (${new Date(verification.checked_at).toLocaleString("sv-SE")}, av ${verification.checked_by === "founder" ? "dig" : "systemet"})`}
          className={`rounded px-2 py-1 text-xs whitespace-nowrap ${VERIFICATION_COLORS[verification.result]}`}
        >
          {VERIFICATION_LABELS[verification.result]}
        </span>
      ) : (
        <span className="rounded px-2 py-1 text-xs whitespace-nowrap bg-white/5 text-white/30">Ej verifierad än</span>
      )}
      <button
        onClick={onVerify}
        disabled={busy}
        className="text-xs rounded border border-border px-2 py-1 disabled:opacity-30 whitespace-nowrap"
      >
        {busy ? "Testar…" : "Testa nu"}
      </button>
    </div>
  );
}
