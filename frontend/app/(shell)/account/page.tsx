"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { api } from "@/lib/api";

export default function AccountPage() {
  const router = useRouter();
  const [exportError, setExportError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);

  const [logoutAllStatus, setLogoutAllStatus] = useState<string | null>(null);
  const [loggingOutAll, setLoggingOutAll] = useState(false);

  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deletePassword, setDeletePassword] = useState("");
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);

  async function handleExport() {
    setExportError(null);
    setExporting(true);
    try {
      const data = await api.exportAccount();
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "mina-uppgifter.json";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err: any) {
      setExportError(err?.message || "Kunde inte exportera dina uppgifter. Försök igen.");
    } finally {
      setExporting(false);
    }
  }

  async function handleLogoutAll() {
    setLogoutAllStatus(null);
    setLoggingOutAll(true);
    try {
      await api.logoutAll();
      // This session dies too — the server just revoked it along with every other one.
      router.replace("/login");
    } catch (err: any) {
      setLogoutAllStatus(err?.message || "Kunde inte logga ut från alla enheter. Försök igen.");
      setLoggingOutAll(false);
    }
  }

  async function handleDelete(e: React.FormEvent) {
    e.preventDefault();
    setDeleteError(null);
    setDeleting(true);
    try {
      await api.deleteAccount(deletePassword);
      router.replace("/login");
    } catch (err: any) {
      setDeleteError(err?.message || "Kunde inte radera kontot. Försök igen.");
      setDeleting(false);
    }
  }

  return (
    <div className="max-w-xl space-y-8">
      <h1 className="text-xl font-semibold">Mitt konto</h1>

      <section aria-labelledby="export-heading" className="space-y-3 rounded-xl border border-border p-5">
        <h2 id="export-heading" className="text-sm font-medium text-white/70">
          Exportera mina uppgifter
        </h2>
        <p className="text-sm text-white/50">
          Ladda ner en JSON-fil med din kontoinformation, dina konversationer och din aktivitetslogg.
        </p>
        {exportError && (
          <div role="alert" className="text-sm text-red-300">
            {exportError}
          </div>
        )}
        <button
          type="button"
          onClick={handleExport}
          disabled={exporting}
          className="rounded-lg border border-border px-4 py-2 text-sm disabled:opacity-50"
        >
          {exporting ? "Exporterar…" : "Exportera mina uppgifter"}
        </button>
      </section>

      <section aria-labelledby="logout-all-heading" className="space-y-3 rounded-xl border border-border p-5">
        <h2 id="logout-all-heading" className="text-sm font-medium text-white/70">
          Logga ut från alla enheter
        </h2>
        <p className="text-sm text-white/50">
          Avslutar alla inloggade sessioner för ditt konto omedelbart, inklusive den här. Använd om du tror att
          någon annan har åtkomst till ditt konto.
        </p>
        {logoutAllStatus && (
          <div role="alert" className="text-sm text-red-300">
            {logoutAllStatus}
          </div>
        )}
        <button
          type="button"
          onClick={handleLogoutAll}
          disabled={loggingOutAll}
          className="rounded-lg border border-border px-4 py-2 text-sm disabled:opacity-50"
        >
          {loggingOutAll ? "Loggar ut…" : "Logga ut från alla enheter"}
        </button>
      </section>

      <section aria-labelledby="delete-heading" className="space-y-3 rounded-xl border border-red-500/30 p-5">
        <h2 id="delete-heading" className="text-sm font-medium text-red-300">
          Radera mitt konto
        </h2>
        <p className="text-sm text-white/50">
          Raderar ditt konto och dina konversationer permanent. Det går inte att ångra. Delat företagsinnehåll du
          har skapat (dokument, projekt, uppgifter) finns kvar men kopplas inte längre till dig.
        </p>

        {!confirmingDelete && (
          <button
            type="button"
            onClick={() => setConfirmingDelete(true)}
            className="rounded-lg border border-red-500/50 px-4 py-2 text-sm text-red-300"
          >
            Radera mitt konto
          </button>
        )}

        {confirmingDelete && (
          <form onSubmit={handleDelete} className="space-y-3" aria-label="Bekräfta radering av konto">
            <label htmlFor="delete-password" className="block text-sm text-white/60">
              Ange ditt lösenord för att bekräfta permanent radering
            </label>
            <input
              id="delete-password"
              type="password"
              required
              autoComplete="current-password"
              value={deletePassword}
              onChange={(e) => setDeletePassword(e.target.value)}
              className="w-full rounded-lg border border-border bg-base px-3 py-2 text-sm outline-none focus:border-red-400"
            />

            {deleteError && (
              <div role="alert" className="text-sm text-red-300">
                {deleteError}
              </div>
            )}

            <div className="flex gap-2">
              <button
                type="submit"
                disabled={deleting}
                className="rounded-lg bg-red-500/80 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
              >
                {deleting ? "Raderar…" : "Radera permanent"}
              </button>
              <button
                type="button"
                onClick={() => {
                  setConfirmingDelete(false);
                  setDeletePassword("");
                  setDeleteError(null);
                }}
                className="rounded-lg border border-border px-4 py-2 text-sm"
              >
                Avbryt
              </button>
            </div>
          </form>
        )}
      </section>
    </div>
  );
}
