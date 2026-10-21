"use client";

import { useEffect, useState, type ReactNode } from "react";
import {
  api,
  SecurityDashboard,
  SecurityFinding,
  SecurityRestriction,
} from "@/lib/api";

export default function FounderSecurityPage() {
  const [dashboard, setDashboard] = useState<SecurityDashboard | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  async function refresh() {
    try {
      setDashboard(await api.securityDashboard());
      setError(null);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "Kunde inte läsa säkerhetsdiagnostik.");
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
  }, []);

  async function propose(finding: SecurityFinding) {
    if (busy) return;
    setBusy(true);
    try {
      await api.securityPropose({
        finding_id: finding.id,
        change_class: finding.category || "code_fix",
        title: `Åtgärda ${finding.kind}`,
        summary: finding.recommended_action,
        isolated_test_plan: "Kör test_mainai_security_diagnostics.py i en isolerad checkout.",
      });
      setNote("Förslag sparat. Känsliga ändringar kräver Founder-godkännande och tillämpas inte automatiskt.");
      await refresh();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "Kunde inte spara förslaget.");
    } finally {
      setBusy(false);
    }
  }

  async function approve(proposalId: string) {
    if (busy) return;
    setBusy(true);
    try {
      const result = await api.securityApprove(proposalId);
      setNote(
        result.applied === true
          ? "Oväntat: en remediation markerades som tillämpad."
          : "Godkänt som avsikt. Ändringen tillämpades inte.",
      );
      await refresh();
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "Kunde inte godkänna förslaget.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Säkerhetsöversikt</h1>
        <p className="text-sm text-white/50 mt-1">
          MainAI läser befintliga audit-händelser, inställningar och lokala manifest. Känsliga
          ändringar kräver verifierat Founder-godkännande och tillämpas inte härifrån.
        </p>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}
      {note && (
        <div role="status" className="rounded-lg border border-border bg-panel p-3 text-sm text-white/70">
          {note}
        </div>
      )}
      {!dashboard && !error && <p className="text-sm text-white/40">Hämtar säkerhetsdiagnostik…</p>}

      {dashboard && (
        <>
          <section className="grid grid-cols-1 md:grid-cols-3 gap-4" data-testid="security-posture">
            <InfoCard title="Allvarlighet">
              {Object.entries(dashboard.severity_counts).map(([key, value]) => (
                <div key={key}>
                  {key}: {value}
                </div>
              ))}
            </InfoCard>
            <InfoCard title="Signaler">
              <div>Bekräftade: {dashboard.confidence_counts.confirmed ?? 0}</div>
              <div>Osäkra: {dashboard.confidence_counts.uncertain ?? 0}</div>
            </InfoCard>
            <InfoCard title="Begränsningar">
              <div data-testid="security-first-cycle">Första cykelns GO = {dashboard.first_cycle_go ? "true" : "false"}</div>
              <div>Blockerar inte first-cycle: {dashboard.blocks_first_cycle ? "nej" : "ja"}</div>
              <div>Kundisolation: {dashboard.customer_isolation}</div>
            </InfoCard>
          </section>

          <section className="rounded-xl border border-border bg-panel p-4 space-y-2 text-sm" data-testid="security-restrictions">
            <div className="text-xs uppercase tracking-wide text-white/40">Förklarade begränsningar</div>
            <p className="text-white/50">Källan är den faktiska inställningen. Ingen policy hittas på.</p>
            <ul className="space-y-2">
              {dashboard.restrictions.map((restriction) => (
                <RestrictionRow key={restriction.code} restriction={restriction} />
              ))}
            </ul>
          </section>

          <section className="space-y-2" data-testid="security-findings">
            <div className="text-xs uppercase tracking-wide text-white/40">Fynd</div>
            {dashboard.findings.length === 0 && <p className="text-sm text-white/40">Inga analysfynd just nu.</p>}
            {dashboard.findings.map((finding) => (
              <article key={finding.id} className="rounded-xl border border-border bg-panel p-4 space-y-2 text-sm">
                <div className="flex flex-wrap gap-2 justify-between">
                  <strong>{finding.kind}</strong>
                  <span>
                    {finding.severity} · {finding.confidence}
                  </span>
                </div>
                <div className="text-white/60">Komponent: {finding.component}</div>
                <div className="text-white/60">Källa: {finding.source}</div>
                <div>Åtgärd: {finding.recommended_action}</div>
                <div>Status: {finding.status}</div>
                <pre className="text-xs text-white/40 overflow-x-auto">{JSON.stringify(finding.evidence, null, 2)}</pre>
                <button
                  type="button"
                  onClick={() => propose(finding)}
                  disabled={busy}
                  className="rounded-lg border border-border px-3 py-1 text-sm disabled:opacity-40"
                >
                  Förbered åtgärdsförslag
                </button>
              </article>
            ))}
          </section>

          <section className="space-y-2" data-testid="security-incidents">
            <div className="text-xs uppercase tracking-wide text-white/40">Hotövervakning</div>
            {dashboard.incidents.length === 0 && <p className="text-sm text-white/40">Inga audit-signaler i fönstret.</p>}
            {dashboard.incidents.map((incident) => (
              <article key={String(incident.id)} className="rounded-xl border border-border bg-panel p-4 text-sm space-y-1">
                <div>
                  {String(incident.kind)} · {String(incident.confidence)} · {String(incident.severity)}
                </div>
                <div className="text-white/60">{String(incident.recommended_action || "")}</div>
              </article>
            ))}
          </section>

          <section className="space-y-2" data-testid="security-remediation">
            <div className="text-xs uppercase tracking-wide text-white/40">Åtgärdsförslag</div>
            {dashboard.remediation.map((item, index) => (
              <article key={String(item.proposal_id || item.finding_id || index)} className="rounded-xl border border-border bg-panel p-4 text-sm space-y-2">
                <div>{String(item.title || item.summary)}</div>
                <div className="text-white/60">Klass: {String(item.change_class)}</div>
                <div>Kräver Founder-godkännande: {item.requires_founder_approval ? "ja" : "nej"}</div>
                <div>Tillämpad: {item.applied ? "ja" : "nej"}</div>
                {item.proposal_id && (
                  <button
                    type="button"
                    onClick={() => approve(String(item.proposal_id))}
                    disabled={busy}
                    className="rounded-lg border border-border px-3 py-1 text-sm disabled:opacity-40"
                  >
                    Godkänn avsikt
                  </button>
                )}
              </article>
            ))}
          </section>

          <section className="rounded-xl border border-border bg-panel p-4 text-sm space-y-1" data-testid="security-limitations">
            <div className="text-xs uppercase tracking-wide text-white/40">Säkerhetsbegränsningar</div>
            {dashboard.limitations.map((limitation) => (
              <div key={limitation}>{limitation}</div>
            ))}
          </section>
        </>
      )}
    </div>
  );
}

function InfoCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="rounded-xl border border-border bg-panel p-4 text-sm space-y-1">
      <div className="text-xs uppercase tracking-wide text-white/40">{title}</div>
      {children}
    </section>
  );
}

function RestrictionRow({ restriction }: { restriction: SecurityRestriction }) {
  return (
    <li>
      <div>{restriction.explain}</div>
      <div className="text-white/40 text-xs">
        källa: {restriction.source || "ingen"} · hittad på: {restriction.invented ? "ja" : "nej"}
      </div>
    </li>
  );
}
