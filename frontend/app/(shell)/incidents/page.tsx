"use client";

import { useCallback, useEffect, useState } from "react";
import { api, RuntimeHealth, RuntimeIncident } from "@/lib/api";

const SEVERITY_STYLE: Record<RuntimeIncident["severity"], string> = {
  low: "border-sky-500/30 bg-sky-500/10 text-sky-200",
  medium: "border-amber-500/30 bg-amber-500/10 text-amber-200",
  high: "border-orange-500/30 bg-orange-500/10 text-orange-200",
  critical: "border-red-500/40 bg-red-500/15 text-red-200",
};

const HEALTH_STYLE: Record<RuntimeHealth["state"], string> = {
  healthy: "border-emerald-500/30 bg-emerald-500/10 text-emerald-200",
  recovered: "border-sky-500/30 bg-sky-500/10 text-sky-200",
  degraded: "border-amber-500/30 bg-amber-500/10 text-amber-200",
  blocked: "border-red-500/40 bg-red-500/15 text-red-200",
};

const HEALTH_LABEL: Record<RuntimeHealth["state"], string> = {
  healthy: "Frisk",
  recovered: "Återställd",
  degraded: "Degraderad",
  blocked: "Blockerad",
};

function formatTime(value: string) {
  try {
    return new Date(value).toLocaleString("sv-SE");
  } catch {
    return value;
  }
}

export default function IncidentsPage() {
  const [incidents, setIncidents] = useState<RuntimeIncident[]>([]);
  const [health, setHealth] = useState<RuntimeHealth | null>(null);
  const [status, setStatus] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (nextStatus: string) => {
    setLoading(true);
    setError(null);
    try {
      const [items, nextHealth] = await Promise.all([
        api.listIncidents(nextStatus || undefined),
        api.incidentHealth().catch(() => null),
      ]);
      setIncidents(items);
      setHealth(nextHealth);
    } catch (e: any) {
      setError(e?.message ?? "Kunde inte läsa incidenthistoriken.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Documented fetch-on-mount pattern used by the other shell pages.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load("");
  }, [load]);

  async function changeStatus(id: string, action: "acknowledge" | "resolve") {
    try {
      const updated =
        action === "acknowledge" ? await api.acknowledgeIncident(id) : await api.resolveIncident(id);
      setIncidents((prev) => prev.map((item) => (item.id === updated.id ? updated : item)));
    } catch (e: any) {
      setError(e?.message ?? "Kunde inte uppdatera incidenten.");
    }
  }

  function selectStatus(next: string) {
    setStatus(next);
    load(next);
  }

  return (
    <div className="max-w-4xl">
      <div className="mb-5">
        <h1 className="text-xl font-semibold">Driftstatus</h1>
        <p className="mt-1 text-sm text-white/50">
          Lokala incidenter som LifeAI själv har upptäckt. Ingen information skickas automatiskt
          till någon extern tjänst.
        </p>
      </div>

      {health && (
        <section
          className={`mb-4 rounded-xl border px-4 py-3 ${HEALTH_STYLE[health.state]}`}
          aria-live="polite"
        >
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-medium">
              Systemstatus: {HEALTH_LABEL[health.state]}
            </div>
            <div className="text-[11px] opacity-80">
              {health.open_incidents} öppna · {health.stalled_jobs} stillastående jobb · worker:{" "}
              {health.worker_heartbeat === "alive" ? "aktiv" : "okänd"}
            </div>
          </div>
          <p className="mt-1 text-xs opacity-75">
            {health.state === "blocked"
              ? "Kritiska incidenter kräver ägarens granskning innan arbete fortsätter."
              : health.state === "degraded"
                ? "Tjänsten fungerar delvis; granska de öppna incidenterna."
                : health.state === "recovered"
                  ? "Tidigare incidenter är lösta och inga nya är öppna."
                  : "Inga öppna incidenter och inga stillastående jobb har upptäckts."}
          </p>
        </section>
      )}

      <div className="mb-4 flex flex-wrap gap-2" aria-label="Filtrera incidenter">
        {[
          ["", "Alla"],
          ["open", "Öppna"],
          ["acknowledged", "Kvitterade"],
          ["resolved", "Lösta"],
        ].map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => selectStatus(value)}
            aria-pressed={status === value}
            className={`rounded-lg border px-3 py-1.5 text-xs ${
              status === value
                ? "border-accent/50 bg-accent/20 text-white"
                : "border-border text-white/60 hover:bg-white/5"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {error && (
        <div role="alert" className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">
          {error}
        </div>
      )}
      {loading && <div className="text-sm text-white/40">Läser incidenthistorik…</div>}
      {!loading && incidents.length === 0 && (
        <div className="rounded-xl border border-border bg-panel px-4 py-6 text-sm text-white/50">
          Inga registrerade incidenter för det här filtret.
        </div>
      )}

      <div className="space-y-3">
        {incidents.map((incident) => (
          <article key={incident.id} className="rounded-xl border border-border bg-panel p-4">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`rounded-full border px-2 py-0.5 text-[11px] uppercase tracking-wide ${SEVERITY_STYLE[incident.severity]}`}>
                    {incident.severity}
                  </span>
                  <span className="text-xs text-white/40">{incident.component}</span>
                  <span className="text-xs text-white/30">·</span>
                  <span className="text-xs text-white/40">{incident.status}</span>
                </div>
                <h2 className="mt-2 text-sm font-medium">{incident.summary}</h2>
                <div className="mt-1 text-xs text-white/40">
                  {incident.error_code} · {incident.occurrence_count} förekomst
                  {incident.occurrence_count === 1 ? "" : "er"} · senast {formatTime(incident.last_seen_at)}
                </div>
              </div>
              <div className="flex gap-2">
                {incident.status === "open" && (
                  <button
                    type="button"
                    onClick={() => changeStatus(incident.id, "acknowledge")}
                    className="rounded border border-border px-2.5 py-1 text-xs text-white/60 hover:bg-white/5"
                  >
                    Kvittera
                  </button>
                )}
                {incident.status !== "resolved" && (
                  <button
                    type="button"
                    onClick={() => changeStatus(incident.id, "resolve")}
                    className="rounded border border-emerald-500/30 px-2.5 py-1 text-xs text-emerald-200 hover:bg-emerald-500/10"
                  >
                    Markera löst
                  </button>
                )}
              </div>
            </div>

            {incident.analysis?.reason && (
              <div className="mt-3 rounded-lg border border-border/70 bg-black/10 px-3 py-2 text-xs text-white/60">
                <span className="text-white/40">Möjlig orsak: </span>
                {incident.analysis.reason}
                {incident.analysis.confidence && (
                  <span className="text-white/30"> (säkerhet: {incident.analysis.confidence})</span>
                )}
              </div>
            )}

            {incident.recovery_options.length > 0 && (
              <div className="mt-3">
                <div className="text-xs text-white/40">Säkra nästa steg</div>
                <ul className="mt-1 space-y-1 text-xs text-white/60">
                  {incident.recovery_options.map((option) => (
                    <li key={option.action} className="flex gap-2">
                      <span aria-hidden="true">•</span>
                      <span>{option.description}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-white/30">
              <span>Först: {formatTime(incident.first_seen_at)}</span>
              <span>Korrelation: {incident.correlation_id ?? "saknas"}</span>
              <span>Version: {incident.app_version ?? "okänd"}</span>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
