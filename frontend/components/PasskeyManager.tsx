"use client";

import { useEffect, useState } from "react";
import { api, PasskeyItem } from "@/lib/api";
import { enrollPasskey, passkeysSupported } from "@/lib/webauthn";

export default function PasskeyManager() {
  const [items, setItems] = useState<PasskeyItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [supported, setSupported] = useState(true);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deletePw, setDeletePw] = useState("");

  async function refresh() {
    try {
      setItems(await api.listPasskeys());
    } catch {
      setItems([]);
    }
  }

  useEffect(() => {
    setSupported(passkeysSupported());
    refresh();
  }, []);

  async function handleAdd() {
    setError(null);
    const label = window.prompt("Namnge den här passkeyn (t.ex. 'iPhone', 'MacBook'):", "");
    if (!label || !label.trim()) return;
    setBusy(true);
    try {
      await enrollPasskey(label.trim());
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Kunde inte lägga till passkey.");
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(id: string) {
    setError(null);
    if (!deletePw) {
      setError("Ange ditt lösenord för att ta bort passkeyn.");
      return;
    }
    setBusy(true);
    try {
      await api.deletePasskey(id, deletePw);
      setDeletingId(null);
      setDeletePw("");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Kunde inte ta bort passkey.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="passkeys-heading" className="space-y-3 rounded-xl border border-border p-5">
      <h2 id="passkeys-heading" className="text-sm font-medium text-white/70">
        Passkeys
      </h2>
      <p className="text-sm text-white/50">
        Logga in med fingeravtryck, ansikte eller skärmlås i stället för lösenord. Den privata
        nyckeln lämnar aldrig enheten.
      </p>
      {!supported && (
        <p className="text-sm text-amber-300">Den här webbläsaren stödjer inte passkeys.</p>
      )}
      {error && (
        <div role="alert" className="text-sm text-red-300">
          {error}
        </div>
      )}
      <ul className="space-y-2">
        {(items ?? []).map((p) => (
          <li
            key={p.id}
            className="flex items-center justify-between gap-2 rounded-lg border border-border px-3 py-2 text-sm"
          >
            <div>
              <div className="font-medium">{p.label}</div>
              <div className="text-xs text-white/40">
                Tillagd {new Date(p.created_at).toLocaleDateString("sv-SE")}
                {p.last_used_at
                  ? ` · Senast använd ${new Date(p.last_used_at).toLocaleDateString("sv-SE")}`
                  : ""}
              </div>
            </div>
            {deletingId === p.id ? (
              <div className="flex items-center gap-2">
                <input
                  type="password"
                  value={deletePw}
                  onChange={(e) => setDeletePw(e.target.value)}
                  placeholder="Lösenord"
                  aria-label="Lösenord för att ta bort passkey"
                  className="w-28 rounded-lg border border-border bg-base px-2 py-1 text-xs outline-none focus:border-accent"
                />
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => handleDelete(p.id)}
                  className="rounded-lg border border-red-500/50 px-3 py-1 text-xs text-red-300 disabled:opacity-50"
                >
                  Ta bort
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setDeletingId(null);
                    setDeletePw("");
                  }}
                  className="text-xs text-white/50"
                >
                  Avbryt
                </button>
              </div>
            ) : (
              <button
                type="button"
                onClick={() => {
                  setDeletingId(p.id);
                  setError(null);
                }}
                className="rounded-lg border border-border px-3 py-1 text-xs"
              >
                Ta bort
              </button>
            )}
          </li>
        ))}
        {items !== null && items.length === 0 && (
          <li className="text-sm text-white/40">Inga passkeys ännu.</li>
        )}
      </ul>
      <button
        type="button"
        disabled={busy || !supported}
        onClick={handleAdd}
        className="rounded-lg border border-border px-4 py-2 text-sm disabled:opacity-50"
      >
        {busy ? "Arbetar…" : "Lägg till passkey"}
      </button>
    </section>
  );
}
