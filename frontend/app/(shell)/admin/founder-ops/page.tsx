"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { api, FounderAgent, FounderNextAction, FounderOpsSnapshot, FounderWorkItem } from "@/lib/api";
import { describeHealth, describeReadiness, describeStop } from "@/lib/founderHealth";
import { FOUNDER_STATUS_COLORS, FOUNDER_STATUS_LABELS } from "@/lib/founderStatus";

const POLL_INTERVAL_MS = 5000;

export default function FounderOpsPage() {
  const [snapshot, setSnapshot] = useState<FounderOpsSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stopReason, setStopReason] = useState("Founder STOP från gränssnittet");
  const [busy, setBusy] = useState(false);

  async function refresh() {
    try {
      setSnapshot(await api.founderOpsSnapshot());
      setError(null);
    } catch (e: unknown) {
      setSnapshot(null);
      setError(e instanceof Error ? e.message : "Kunde inte läsa Founder-status.");
    }
  }

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    refresh();
    const interval = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, []);

  async function activateStop() {
    if (!stopReason.trim() || busy) return;
    setBusy(true);
    try {
      await api.founderOpsActivateStop(stopReason.trim());
      await refresh();
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "STOP misslyckades.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold">Founder-status</h1>
        <p className="text-sm text-white/50 mt-1">
          Hållbar status från databasen. Tilldelning är inte aktivitet. Kvittering är inte
          körning. Första cykelns GO är avstängd tills transport/grants är sammansatta.
        </p>
      </div>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-300">
          {error}
        </div>
      )}

      {!snapshot && !error && <p className="text-sm text-white/40">Hämtar Founder-status…</p>}

      {snapshot && (
        <>
          <section className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <InfoCard title="Hälsa">
              <div data-testid="founder-health">{describeHealth(snapshot.health)}</div>
              <div>Worker: {snapshot.health.worker}</div>
              <div data-testid="readiness-level">{describeReadiness(snapshot.startup_readiness)}</div>
            </InfoCard>
            <InfoCard title="STOP">
              <div data-testid="stop-state">{describeStop(snapshot.stop)}</div>
              {snapshot.stop.code && <div className="text-amber-200">{snapshot.stop.code}</div>}
              {snapshot.stop.observability_failed && (
                <div role="alert">STOP-status okänd — behandlas som blockerad.</div>
              )}
            </InfoCard>
            <InfoCard title="Kostnad">
              <div>
                Användning:{" "}
                {snapshot.spend.usage_cost_usd === null
                  ? "okänd (saknar pris)"
                  : `${snapshot.spend.usage_cost_usd.toFixed(4)} USD`}
              </div>
              <div>Anrop: {snapshot.spend.usage_request_count}</div>
              <div>
                Aktiva grant: {snapshot.spend.active_grant_count} / spenderat{" "}
                {snapshot.spend.active_grant_spent_usd.toFixed(4)} USD
              </div>
            </InfoCard>
          </section>

          <section className="rounded-xl border border-border bg-panel p-4 space-y-2 text-sm">
            <div className="text-xs uppercase tracking-wide text-white/40">Säker start</div>
            <Flag ok={!snapshot.safe_launch.github_write_enabled} label="GitHub-skrivning av" />
            <Flag ok={!snapshot.safe_launch.privileged_remote_effects_enabled} label="Privilegierade fjärreffekter av" />
            <Flag ok={!snapshot.safe_launch.recall_http_mounted} label="Recall HTTP inte monterad" />
            <Flag ok={!snapshot.safe_launch.mainai_activation_mode} label="Aktiveringsläge av" />
            <Flag ok={!snapshot.first_cycle_go} label="Första cykelns GO = false" />
          </section>

          {snapshot.durable_coordination && (
            <DurableCoordination view={snapshot.durable_coordination} />
          )}

          {snapshot.security && (
            <section className="rounded-xl border border-border bg-panel p-4 space-y-2 text-sm" data-testid="founder-security-summary">
              <div className="text-xs uppercase tracking-wide text-white/40">Säkerhetsdiagnostik</div>
              <div>Fynd: {snapshot.security.finding_count}</div>
              <div>Bekräftade incidenter: {snapshot.security.confirmed_incident_count}</div>
              <Flag ok={!snapshot.security.blocks_first_cycle} label="Blockerar inte first-cycle" />
              <Link href="/admin/security" className="inline-block text-sm underline">
                Öppna säkerhetsöversikt
              </Link>
            </section>
          )}

          {snapshot.authority && (
            <section className="rounded-xl border border-border bg-panel p-4 space-y-2 text-sm" data-testid="founder-authority">
              <div className="text-xs uppercase tracking-wide text-white/40">Founder-auktoritet</div>
              <Flag ok={snapshot.authority.policy_transparent} label="Policy är transparent" />
              <Flag
                ok={snapshot.authority.sensitive_changes_require_founder_approval}
                label="Känsliga inställningar kräver Founder-godkännande"
              />
              <Flag
                ok={snapshot.authority.independent_security_enforcement}
                label="Säkerhetshandhavande är oberoende av byggaren"
              />
              <Flag ok={!snapshot.authority.live_checkout_writes_authorized} label="Live-checkout-skrivning av" />
              <ul className="text-white/60 space-y-1">
                {snapshot.authority.restrictions.map((restriction) => (
                  <li key={restriction.code}>{restriction.explain}</li>
                ))}
              </ul>
            </section>
          )}

          <section className="rounded-xl border border-border bg-panel p-4 space-y-2">
            <div className="text-xs uppercase tracking-wide text-white/40">Aktivera STOP</div>
            <div className="flex flex-col md:flex-row gap-2">
              <input
                value={stopReason}
                onChange={(e) => setStopReason(e.target.value)}
                aria-label="STOP-orsak"
                className="flex-1 rounded-lg bg-base border border-border px-3 py-2 text-sm"
              />
              <button
                type="button"
                onClick={activateStop}
                disabled={busy || snapshot.stop.blocked}
                className="rounded-lg border border-red-500/40 px-4 py-2 text-sm text-red-200 disabled:opacity-40"
              >
                Aktivera STOP
              </button>
            </div>
          </section>

          <Counts counts={snapshot.work_item_counts} />

          <Agents agents={snapshot.agents || []} />
          <NextActions actions={snapshot.next_actions || []} />
          <IndependentReady items={snapshot.independent_ready_work || []} />
          <Dependencies
            dependencies={snapshot.dependencies || []}
            cycles={snapshot.circular_dependencies || []}
          />

          <section className="space-y-2">
            <div className="text-xs uppercase tracking-wide text-white/40" data-testid="blocker-counts">
              Blockeringar (operativa {snapshot.blocker_counts?.operational ?? snapshot.blockers.length}
              {snapshot.blocker_counts
                ? ` · beredskap ${snapshot.blocker_counts.startup_readiness} · STOP ${snapshot.blocker_counts.stop}`
                : ""}
              )
            </div>
            {snapshot.blockers.length === 0 && <p className="text-sm text-white/30">Inga registrerade blockeringar.</p>}
            <ul className="space-y-1 text-sm">
              {snapshot.blockers.map((b, i) => (
                <li key={i} className="rounded-lg border border-amber-500/20 bg-amber-500/5 px-3 py-2 text-amber-100">
                  {String(b.kind ?? "blocker")}
                  {b.detail ? `: ${typeof b.detail === "string" ? b.detail : JSON.stringify(b.detail)}` : ""}
                </li>
              ))}
            </ul>
          </section>

          <section className="space-y-2">
            <div className="text-xs uppercase tracking-wide text-white/40">
              Godkännanden som väntar ({snapshot.authorization_requests.length})
            </div>
            {snapshot.authorization_requests.length === 0 && (
              <p className="text-sm text-white/30">Inga öppna auktoriseringsförfrågningar.</p>
            )}
            <ul className="space-y-1 text-sm">
              {snapshot.authorization_requests.map((req) => (
                <li key={String(req.id)} className="rounded-lg border border-border px-3 py-2">
                  <span className="text-white/70">{String(req.source)}</span>
                  {" — "}
                  <span>{String(req.title ?? req.id)}</span>
                  <span className="text-white/40"> ({String(req.status)})</span>
                </li>
              ))}
            </ul>
          </section>

          <section className="space-y-2">
            <div className="text-xs uppercase tracking-wide text-white/40">Granskningsresultat</div>
            {snapshot.review_results.length === 0 && <p className="text-sm text-white/30">Inga granskningar ännu.</p>}
            <WorkList items={snapshot.review_results} />
          </section>

          <section className="space-y-2">
            <div className="text-xs uppercase tracking-wide text-white/40">Arbete (hållbart)</div>
            <WorkList items={snapshot.work_items} />
          </section>
        </>
      )}
    </div>
  );
}

function InfoCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-border bg-panel p-4 text-sm space-y-1">
      <div className="text-xs uppercase tracking-wide text-white/40">{title}</div>
      {children}
    </div>
  );
}

function Flag({ ok, label }: { ok: boolean; label: string }) {
  return (
    <div className={ok ? "text-emerald-300 text-sm" : "text-red-300 text-sm"}>
      {ok ? "✓" : "✗"} {label}
    </div>
  );
}

const COORDINATION_STATUS_LABELS: Record<string, string> = {
  assigned: "Tilldelad",
  acknowledged: "Kvitterad",
  executing: "Utför",
  blocked: "Blockerad",
  completed: "Klar",
  failed: "Misslyckades",
  superseded: "Ersatt",
  independently_verified: "Självständigt certifierad",
};

function DurableCoordination({
  view,
}: {
  view: NonNullable<FounderOpsSnapshot["durable_coordination"]>;
}) {
  return (
    <section className="rounded-xl border border-border bg-panel p-4 space-y-3 text-sm" data-testid="durable-coordination">
      <div className="text-xs uppercase tracking-wide text-white/40">
        Agentkoordination och artefaktöverlämning
      </div>
      <Flag ok={!view.first_cycle_go && !view.blocks_first_cycle} label="CO-4b/CO-5 är inte first-cycle GO" />
      <div>
        Aktiva: {view.active.length} · Klara: {view.finished.length} · Certifierade:{" "}
        {view.independently_certified.length} · Blockerade: {view.blocked.length} · Fastnade:{" "}
        {view.stalled.length}
      </div>
      <div className="text-white/50">
        Kö tom: {view.queue_empty ? "ja (inga aktiva åtaganden)" : "nej — aktiv ägare finns"}
      </div>
      {view.needed_work && (
        <div data-testid="needed-work">
          <div className="text-xs uppercase tracking-wide text-white/40">Vem behöver arbete</div>
          <Flag ok={!view.needed_work.founder_must_forward_reports} label="Founder behöver inte vidarebefordra rutinrapporter" />
          {(view.needed_work.items || []).length === 0 && (
            <p className="text-white/30">Ingen agent väntar på nästa steg.</p>
          )}
          <ul className="space-y-1 text-xs text-white/60">
            {(view.needed_work.items || []).map((item) => (
              <li key={String(item.assignment_id)}>
                {String(item.agent_identity)}: {String(item.need)}
                {item.missing_capability ? ` (saknas ${String(item.missing_capability)})` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
      {view.obligations.length === 0 && (
        <p className="text-white/30">Inga registrerade åtaganden i ledgern.</p>
      )}
      <ul className="space-y-2">
        {view.obligations.map((row) => (
          <li key={String(row.assignment_id)} className="rounded-lg border border-border/60 px-3 py-2">
            <div className="flex justify-between gap-2">
              <span>{String(row.agent_identity)} · {String(row.workstream)}</span>
              <span className="text-white/60">
                {COORDINATION_STATUS_LABELS[String(row.status)] || String(row.status)}
              </span>
            </div>
            <div className="text-white/40 text-xs mt-1">
              Ägare: {String(row.owner_label || row.agent_identity)}
              {row.stalled ? " · fastnad i planering utan utförande" : ""}
              {row.blocked_reason ? ` · ${String(row.blocked_reason)}` : ""}
              {row.missing_capability ? ` · saknad förmåga: ${String(row.missing_capability)}` : ""}
            </div>
          </li>
        ))}
      </ul>
      {view.lifecycle && (
        <div data-testid="lifecycle-coordination">
          <div className="text-xs uppercase tracking-wide text-white/40">Automatisk livscykel</div>
          <Flag ok={!view.lifecycle.delivery_claimed} label="Ingen Cursor/Claude/Codex-leverans påstås" />
          <Flag
            ok={!view.lifecycle.github_green_is_independent_certification}
            label="GitHub-grön CI är inte oberoende certifiering"
          />
          {(view.lifecycle.builder_eligible || []).length > 0 && (
            <div>
              Byggare redo för mer arbete:{" "}
              {(view.lifecycle.builder_eligible || [])
                .map((row) => String(row.agent_identity))
                .join(", ")}
            </div>
          )}
          {(view.lifecycle.founder_approval_required || []).length === 0 ? (
            <p className="text-white/30">Inget som kräver Founder-godkännande just nu.</p>
          ) : (
            <ul className="text-xs text-white/60">
              {(view.lifecycle.founder_approval_required || []).map((row) => (
                <li key={String(row.assignment_id)}>
                  {String(row.agent_identity)}: {String(row.need)}
                  {row.missing_capability ? ` (saknas ${String(row.missing_capability)})` : ""}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {view.delivery && (
        <div data-testid="delivery-gateway">
          <div className="text-xs uppercase tracking-wide text-white/40">Leveransgateway</div>
          <Flag ok={!view.delivery.vendor_delivery_claimed} label="Ingen Cursor/Claude/Codex-vendorleverans påstås" />
          <Flag
            ok={Boolean(view.delivery.inbox_created_is_not_delivered)}
            label="Inboxrad är inte leverans"
          />
          {(view.delivery.bridges || []).length === 0 && (
            <p className="text-white/30">Ingen Founder-utfärdad lokal brygga är aktiv.</p>
          )}
          <ul className="space-y-1 text-xs text-white/60">
            {(view.delivery.bridges || []).map((row) => (
              <li key={String(row.recipient)}>
                {String(row.recipient)}: {row.live ? "brygga utfärdad" : "utgången/återkallad"}
                {row.connected ? " · ansluten" : " · inte ansluten"}
              </li>
            ))}
          </ul>
          {(view.delivery.messages || []).length === 0 ? (
            <p className="text-white/30">Inga inboxmeddelanden.</p>
          ) : (
            <ul className="space-y-1 text-xs text-white/60">
              {(view.delivery.messages || []).map((row) => (
                <li key={String(row.message_id)}>
                  {String(row.recipient)} · {String(row.status)}
                  {row.missing_capability ? ` · BLOCKED (${String(row.missing_capability)})` : ""}
                  {row.delivered_at ? " · mottaget av brygga" : ""}
                  {row.acknowledged_at ? " · kvitterat" : ""}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {view.github_intake && (
        <div data-testid="github-intake">
          <div className="text-xs uppercase tracking-wide text-white/40">GitHub-utveckling (endast läsning)</div>
          <div>
            Åtkomst: {String(view.github_intake.access || "okänd")}
            {view.github_intake.missing_capability
              ? ` · BLOCKED (${String(view.github_intake.missing_capability)})`
              : ""}
          </div>
          <Flag ok={!view.github_intake.write_attempted} label="Ingen GitHub-skrivning från intaget" />
          <Flag ok={!view.github_intake.delivery_claimed} label="Ingen agentleverans påstås" />
          {(view.github_intake.candidates || []).length === 0 && (
            <p className="text-white/30">Ingen publicerad GitHub-kandidat inläst.</p>
          )}
          <ul className="space-y-1 text-xs text-white/60">
            {(view.github_intake.candidates || []).map((row) => (
              <li key={String(row.pr)}>
                PR {String(row.pr)} @ {String(row.head_sha).slice(0, 12)}… freeze{" "}
                {row.ci && typeof row.ci === "object" && (row.ci as { freeze_allowed?: boolean }).freeze_allowed
                  ? "tillåten"
                  : "blockerad"}
                {row.reviewable ? " · examineringsåtagande" : ""}
              </li>
            ))}
          </ul>
        </div>
      )}
      {view.handoffs.length > 0 && (
        <ul className="space-y-1 text-xs text-white/60">
          {view.handoffs.map((handoff) => (
            <li key={String(handoff.handoff_id)}>
              Artefakt {String(handoff.sha256).slice(0, 12)}… {String(handoff.producer_identity)} →{" "}
              {String(handoff.consumer_identity)} ({String(handoff.status)}
              {handoff.missing_capability ? `; saknas ${String(handoff.missing_capability)}` : ""})
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Counts({ counts }: { counts: Record<string, number> }) {
  return (
    <div className="flex flex-wrap gap-2 text-xs" data-testid="work-status-counts">
      {Object.entries(FOUNDER_STATUS_LABELS).map(([key, label]) => (
        <span key={key} className="rounded-full border border-border px-3 py-1 text-white/70">
          {label}: {counts[key] ?? 0}
        </span>
      ))}
    </div>
  );
}

function Agents({ agents }: { agents: FounderAgent[] }) {
  return (
    <section className="space-y-2" data-testid="founder-agents">
      <div className="text-xs uppercase tracking-wide text-white/40">Aktiva agenter ({agents.length})</div>
      {agents.length === 0 && (
        <p className="text-sm text-white/30">Inga registrerade workforce-agenter. Idle är inte körning.</p>
      )}
      <ul className="space-y-1 text-sm">
        {agents.map((agent) => (
          <li key={agent.id} className="rounded-lg border border-border px-3 py-2">
            <div className="flex justify-between gap-2">
              <span>{agent.name}</span>
              <span className="text-white/50">{agent.running ? "Kör" : agent.founder_status}</span>
            </div>
            <div className="text-white/40 text-xs mt-1">
              {agent.role} · {agent.status}
              {agent.current_assignment_id
                ? ` · uppdrag ${agent.current_assignment_status}`
                : " · inget live-uppdrag"}
            </div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function NextActions({ actions }: { actions: FounderNextAction[] }) {
  return (
    <section className="space-y-2" data-testid="founder-next-actions">
      <div className="text-xs uppercase tracking-wide text-white/40">Nästa åtgärder ({actions.length})</div>
      {actions.length === 0 && <p className="text-sm text-white/30">Inga registrerade nästa åtgärder.</p>}
      <ul className="space-y-1 text-sm">
        {actions.map((action, i) => (
          <li key={`${action.kind}-${action.task_id || i}`} className="rounded-lg border border-border px-3 py-2">
            <span className="text-white/70">{action.kind}</span>
            {action.description ? ` — ${action.description}` : ""}
            {action.detail ? <div className="text-white/40 text-xs mt-1">{action.detail}</div> : null}
          </li>
        ))}
      </ul>
    </section>
  );
}

function IndependentReady({ items }: { items: Record<string, unknown>[] }) {
  return (
    <section className="space-y-2" data-testid="independent-ready-work">
      <div className="text-xs uppercase tracking-wide text-white/40">
        Oberoende redo arbete ({items.length})
      </div>
      {items.length === 0 && (
        <p className="text-sm text-white/30">
          Inget redo-arbete medan syskon väntar. Redo är inte körning.
        </p>
      )}
      <ul className="space-y-1 text-sm">
        {items.map((item) => (
          <li key={String(item.task_id)} className="rounded-lg border border-border px-3 py-2">
            {String(item.description ?? item.task_id)}
            <div className="text-white/40 text-xs mt-1">{String(item.note ?? "")}</div>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Dependencies({
  dependencies,
  cycles,
}: {
  dependencies: Record<string, unknown>[];
  cycles: string[][];
}) {
  return (
    <section className="space-y-2" data-testid="founder-dependencies">
      <div className="text-xs uppercase tracking-wide text-white/40">Beroenden ({dependencies.length})</div>
      {cycles.length > 0 && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-200">
          Cirkulära beroenden: {cycles.map((c) => c.join(" → ")).join(" · ")}
        </div>
      )}
      {dependencies.length === 0 && cycles.length === 0 && (
        <p className="text-sm text-white/30">Inga lagrade uppgiftsberoenden.</p>
      )}
      <ul className="space-y-1 text-xs text-white/60">
        {dependencies.map((dep, i) => (
          <li key={i} className="rounded-lg border border-border/60 px-3 py-2">
            {String(dep.task_description || dep.task_id)} väntar på{" "}
            {String(dep.depends_on_description || dep.depends_on_task_id)} ({String(dep.depends_on_status)})
          </li>
        ))}
      </ul>
    </section>
  );
}

function WorkList({ items }: { items: FounderWorkItem[] }) {
  if (items.length === 0) return <p className="text-sm text-white/30">Inget registrerat arbete.</p>;
  return (
    <ul className="space-y-2">
      {items.map((item, i) => (
        <li key={`${item.source}-${item.task_id || item.job_id || item.assignment_id || i}`} className="rounded-lg border border-border/60 p-3 text-xs">
          <div className="flex justify-between gap-2">
            <span className="text-white/50">{item.source}</span>
            <span className={FOUNDER_STATUS_COLORS[item.founder_status] || "text-white/50"}>
              {FOUNDER_STATUS_LABELS[item.founder_status] || item.founder_status}
            </span>
          </div>
          <div className="text-white/80 mt-1">{item.title || item.description || item.source_status}</div>
          <div className="text-white/40 mt-1">Källstatus: {item.source_status}</div>
          {item.blocker_reason && <div className="text-amber-300 mt-1">Blockerare: {item.blocker_reason}</div>}
        </li>
      ))}
    </ul>
  );
}
