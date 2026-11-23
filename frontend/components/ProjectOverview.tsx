"use client";

import Link from "next/link";
import { ProjectStatus } from "@/lib/api";

// Founder Alpha R15 — the one panel that answers "var är vi?" from the registers instead of
// from the model's impression. Every number is rendered as the backend returned it; where a
// fact is missing the panel says so (a failed read is never displayed as an empty project).
const BUCKETS: { key: "implemented" | "verified" | "planned" | "blocked"; label: string; tone: string }[] = [
  { key: "implemented", label: "Implementerat", tone: "text-emerald-300" },
  { key: "verified", label: "Verifierat", tone: "text-sky-300" },
  { key: "planned", label: "Planerat", tone: "text-white/70" },
  { key: "blocked", label: "Blockerat", tone: "text-amber-300" },
];

export default function ProjectOverview({ status, error }: { status: ProjectStatus | null; error: string | null }) {
  if (error) {
    return (
      <section className="rounded-2xl border border-amber-400/30 bg-panel/70 p-5">
        <h2 className="text-sm font-medium text-white/70">Projektläget</h2>
        <p role="alert" className="mt-3 text-sm text-amber-200">
          Projektläget kunde inte läsas ({error}). Det betyder inte att projektet är tomt — bara att statusen
          inte kunde verifieras just nu.
        </p>
      </section>
    );
  }

  if (!status) {
    return (
      <section className="rounded-2xl border border-border bg-panel/70 p-5">
        <h2 className="text-sm font-medium text-white/70">Projektläget</h2>
        <p className="mt-3 text-sm text-white/35">Läser projektstatus…</p>
      </section>
    );
  }

  const blueprint = status.blueprint.approved;
  const dev = status.github.development;
  const work = status.work;
  const checkTotal = typeof dev.checks?.total === "number" ? dev.checks.total : null;

  return (
    <section className="space-y-5 rounded-2xl border border-border bg-panel/70 p-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-sm font-medium text-white/70">Projektläget</h2>
        <span className="text-[11px] text-white/30">
          Läst {new Date(status.generated_at).toLocaleString("sv-SE")} · schema {status.schema_version}
        </span>
      </div>

      {/* The blueprint is the founder's own approved goal picture — or explicitly absent. */}
      <div className="rounded-xl border border-border bg-base/50 p-3">
        {blueprint ? (
          <p className="text-sm text-white/80">
            <span className="text-[11px] uppercase tracking-wide text-white/35">Godkänd blueprint · </span>
            {blueprint.title} <span className="text-white/40">v{blueprint.version}</span>{" "}
            <span className="text-[11px] text-white/35">({blueprint.source_count} källdokument)</span>
          </p>
        ) : (
          <p className="text-sm text-amber-200">
            Ingen godkänd blueprint ännu — projektets målbild är inte fastställd.
            <Link href="/blueprint" className="ml-2 text-xs text-accent2">
              Skapa blueprint
            </Link>
          </p>
        )}
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {BUCKETS.map((bucket) => {
          const value = status.status_classes[bucket.key];
          return (
            <div key={bucket.key} className="rounded-xl border border-border bg-base/40 p-3">
              <div className="text-[10px] uppercase tracking-widest text-white/35">{bucket.label}</div>
              <div className={`mt-1 text-xl font-semibold ${bucket.tone}`}>{value.count}</div>
              <div className="mt-1 line-clamp-3 text-[10px] leading-snug text-white/30">
                {value.examples.length ? value.examples.join(" · ") : value.evidence}
              </div>
            </div>
          );
        })}
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        {/* Development status: what was VERIFIED against GitHub, and how old it is. */}
        <div className="rounded-xl border border-border bg-base/40 p-3">
          <div className="flex items-baseline justify-between">
            <div className="text-[10px] uppercase tracking-widest text-white/35">Utvecklingsstatus</div>
            <Link href="/admin/jobs" className="text-[10px] text-accent2">
              Jobb
            </Link>
          </div>
          {status.github.configured ? (
            <ul className="mt-2 space-y-1 text-xs text-white/60">
              <li>
                {dev.head_sha ? (
                  <>
                    <span className="font-mono text-white/80">{dev.head_sha.slice(0, 10)}</span>{" "}
                    <span className="text-white/40">{dev.head_subject?.slice(0, 60)}</span>
                  </>
                ) : (
                  "Ingen head verifierad ännu"
                )}
              </li>
              <li>
                {dev.open_prs ?? "?"} öppna PR · {dev.open_issues ?? "?"} ärenden ·{" "}
                {checkTotal === null ? "CI ej läst" : `${checkTotal} check-körningar`}
              </li>
              <li className={dev.stale ? "text-amber-300" : "text-white/35"}>
                {dev.last_verified_at
                  ? `Verifierad ${dev.age_minutes} min sedan${dev.stale ? " · INAKTUELL" : ""}`
                  : "Konfigurerad men aldrig verifierad"}
              </li>
              {dev.local_unpushed_count !== null && (
                <li className="text-white/35">{dev.local_unpushed_count} lokala commits ej på GitHub</li>
              )}
              {dev.local_unavailable_reason && (
                <li className="text-white/30">Lokal jämförelse: {dev.local_unavailable_reason}</li>
              )}
              {dev.error && <li className="text-amber-300">Senaste läsning misslyckades: {dev.error}</li>}
            </ul>
          ) : (
            <p className="mt-2 text-xs text-amber-200">
              GitHub är inte konfigurerad (GITHUB_TOKEN/GITHUB_REPO) — utvecklingsstatus kan inte verifieras.
            </p>
          )}
        </div>

        {/* The live work chain — the same policy the dispatch gate enforces. */}
        <div className="rounded-xl border border-border bg-base/40 p-3">
          <div className="flex items-baseline justify-between">
            <div className="text-[10px] uppercase tracking-widest text-white/35">Arbetskedja</div>
            <Link href="/admin/mainai-execution" className="text-[10px] text-accent2">
              Mål & planer
            </Link>
          </div>
          <ul className="mt-2 space-y-1 text-xs text-white/60">
            <li>
              {work.goals.active_count} aktiva mål · {work.goals.completed_count} avslutade
            </li>
            <li className={work.tasks.awaiting_approval_count ? "text-amber-200" : ""}>
              {work.tasks.awaiting_approval_count} väntar på ditt godkännande
            </li>
            <li>
              {work.tasks.counts.running ?? 0} uppgifter körs · {work.jobs.active} aktiva jobb
              {work.jobs.stalled_count > 0 && (
                <span className="text-amber-300"> ({work.jobs.stalled_count} stillastående)</span>
              )}
            </li>
            <li>
              {work.agents.open_assignments} öppna agentuppdrag
              {work.agents.blocked.length > 0 && (
                <span className="text-amber-300"> ({work.agents.blocked.length} blockerade)</span>
              )}
            </li>
            {work.candidates.unreviewed_count > 0 && <li>{work.candidates.unreviewed_count} arbetskandidater väntar</li>}
          </ul>
        </div>
      </div>

      {status.warnings.length > 0 && (
        <div className="rounded-xl border border-amber-400/30 bg-amber-400/5 p-3">
          <div className="text-[10px] uppercase tracking-widest text-amber-300/80">Osäkerhet och datakvalitet</div>
          <ul className="mt-2 space-y-1 text-xs text-amber-100/80">
            {status.warnings.slice(0, 4).map((warning) => (
              <li key={warning}>· {warning}</li>
            ))}
          </ul>
        </div>
      )}

      {status.next_steps.length > 0 && (
        <div>
          <div className="flex items-baseline justify-between">
            <div className="text-[10px] uppercase tracking-widest text-white/35">Möjliga nästa steg</div>
            <span className="text-[10px] text-white/25">Förslag — inte auktoriserade</span>
          </div>
          <ul className="mt-2 space-y-2">
            {status.next_steps.slice(0, 3).map((step) => (
              <li key={`${step.kind}-${step.source}`} className="flex items-start justify-between gap-2 text-xs">
                <div className="min-w-0">
                  <div className="truncate text-white/75">{step.title}</div>
                  <div className="truncate text-[10px] text-white/30">{step.why}</div>
                </div>
                <span className="shrink-0 rounded-full border border-border px-2 py-0.5 font-mono text-[9px] text-white/30">
                  {step.source.split(":")[0]}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
