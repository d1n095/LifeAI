"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  DecisionCompleteness,
  GitHubOperationsCheck,
  GitHubOperationsPullRequest,
  GitHubOperationsSnapshot,
  IntegrationAnalysis,
} from "@/lib/api";

export default function GitHubOperationsPage() {
  const [snapshot, setSnapshot] = useState<GitHubOperationsSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [analysis, setAnalysis] = useState<IntegrationAnalysis | null>(null);
  const [analysisError, setAnalysisError] = useState<string | null>(null);
  const [selectedPrs, setSelectedPrs] = useState("");
  const [migrationGraph, setMigrationGraph] = useState(false);
  const analysisRequest = useRef(0);

  const loadAnalysis = useCallback(async (numbers: number[] = [], graph = false) => {
    const requestId = ++analysisRequest.current;
    setAnalysis(null);
    setAnalysisError(null);
    try {
      const result = await api.githubIntegrationAnalysis(numbers, graph);
      if (requestId === analysisRequest.current) setAnalysis(result);
    } catch {
      if (requestId === analysisRequest.current) {
        setAnalysisError("Integration analysis is unavailable; readiness is UNKNOWN.");
      }
    }
  }, []);

  const load = useCallback(async (forceRefresh = false) => {
    setLoading(true);
    setError(null);
    try {
      setSnapshot(await api.githubOperationsSnapshot(forceRefresh));
      void loadAnalysis();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load project status.");
    } finally {
      setLoading(false);
    }
  }, [loadAnalysis]);

  useEffect(() => {
    let active = true;
    api.me()
      .then((user) => {
        if (!active) return;
        if (user.role !== "founder") {
          setError("Founder access is required to view repository operations.");
          setLoading(false);
          return;
        }
        void load();
      })
      .catch(() => {
        if (active) {
          setError("Could not verify access to repository operations.");
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [load]);

  const pullRequests = snapshot?.pull_requests ?? [];
  const detailCount = snapshot?.observation.pagination.detailed_pull_request_count;
  const totalPrCount = snapshot?.observation.pagination.total_open_pull_request_count;
  const failedChecks = pullRequests.flatMap((pr) =>
    pr.checks.filter((check) => check.state === "failed").map((check) => ({ pr, check })),
  );
  const openBlockers = snapshot?.blockers ?? [];

  return (
    <div className="space-y-7" data-testid="github-operations-page">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">GitHub Operations Center</h1>
          <p className="mt-1 text-sm text-white/55">
            Read-only project observations. GitHub content is untrusted data; no agent is dispatched from this view.
          </p>
        </div>
        <button
          type="button"
          onClick={() => void load(true)}
          disabled={loading}
          className="rounded-lg border border-border px-4 py-2 text-sm disabled:opacity-40"
        >
          {loading ? "Loading…" : "Refresh from GitHub"}
        </button>
      </header>

      {error && (
        <div role="alert" className="rounded-lg border border-red-500/30 bg-red-500/10 p-4 text-sm text-red-200">
          {error}
        </div>
      )}
      {!snapshot && loading && <p className="text-sm text-white/50">Loading project observations…</p>}
      {snapshot && (
        <>
          <section className="rounded-xl border border-border bg-panel p-4" aria-label="Overall project status">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <h2 className="text-lg font-medium">
                  {snapshot.repository.full_name ?? "Repository not configured"}
                </h2>
                <p className="mt-1 text-sm text-white/50">
                  Default branch: {snapshot.repository.default_branch ?? "UNKNOWN"} · Availability:{" "}
                  {snapshot.repository.availability}
                </p>
              </div>
              {snapshot.repository.url && <ExternalLink href={snapshot.repository.url}>Open repository</ExternalLink>}
            </div>
            <div
              className={`mt-4 rounded-lg border p-3 text-sm ${freshnessClass(snapshot.observation.freshness)}`}
              role="status"
              data-testid="snapshot-freshness"
            >
              Observation: <strong>{snapshot.observation.freshness.toUpperCase()}</strong>
              {snapshot.observation.observed_at && (
                <> · observed {formatDate(snapshot.observation.observed_at)}</>
              )}
              {snapshot.observation.cache_age_seconds !== null && snapshot.observation.cache_age_seconds > 0 && (
                <> · cached {snapshot.observation.cache_age_seconds}s</>
              )}
              {snapshot.observation.error_category && <> · {snapshot.observation.error_category}</>}
              {snapshot.observation.freshness === "unavailable" && (
                <span> · GitHub status is unavailable; an empty list is not evidence that no PRs exist.</span>
              )}
            </div>
            <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
              <Metric label="Open PRs" value={pullRequests.length} />
              <Metric
                label="PRs with failed CI evidence"
                value={pullRequests.filter((pr) => pr.ci_state === "failed").length}
              />
              <Metric label="Recorded blockers" value={openBlockers.length} />
              <Metric label="Registered assignments" value={snapshot.assignments.length} />
            </div>
            {snapshot.observation.pagination.open_pull_requests_truncated && (
              <p className="mt-3 text-xs text-amber-200">
                Open-PR pagination reached its safety limit. The list is incomplete; do not treat it as repository-wide absence.
              </p>
            )}
            {typeof detailCount === "number" && typeof totalPrCount === "number" && detailCount < totalPrCount && (
              <p className="mt-3 text-xs text-amber-200">
                Detailed checks, reviews, and comments were fetched for {detailCount} of {totalPrCount} open PRs.
                Remaining PR details are UNKNOWN, not passed.
              </p>
            )}
          </section>

          <section className="space-y-3" aria-label="Integration analysis">
            <SectionHeading title="Integration analysis" note="Read-only evidence, not security approval. Findings are never dispatched." />
            <form
              className="flex flex-wrap gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                const parts = selectedPrs.split(",").map((part) => part.trim());
                if (migrationGraph && (parts.length < 1 || parts.length > 2 || !parts.every((part) => /^[1-9]\d*$/.test(part)))) {
                  analysisRequest.current++;
                  setAnalysis(null);
                  setAnalysisError("Cross-PR migration graph requires one or two positive PR numbers.");
                } else if (!selectedPrs.trim()) {
                  void loadAnalysis();
                } else if (parts.length <= 8 && parts.every((part) => /^[1-9]\d*$/.test(part) && Number.isSafeInteger(Number(part)))) {
                  void loadAnalysis(parts.map(Number), migrationGraph);
                } else {
                  analysisRequest.current++;
                  setAnalysis(null);
                  setAnalysisError("Enter up to eight comma-separated positive PR numbers.");
                }
              }}
            >
              <label htmlFor="integration-prs" className="text-sm text-white/60">PR numbers (optional)</label>
              <input id="integration-prs" value={selectedPrs} onChange={(event) => setSelectedPrs(event.target.value)} className="rounded border border-border bg-panel px-2 text-sm" placeholder="237, 363" />
              <label className="text-sm text-white/60"><input type="checkbox" checked={migrationGraph} onChange={(event) => setMigrationGraph(event.target.checked)} /> Compare migration graphs (1–2 PRs; up to 128 files each)</label>
              <button type="submit" className="rounded border border-border px-3 py-1 text-sm">Analyze</button>
            </form>
            {analysisError && <p role="alert" className="text-sm text-amber-100">{analysisError}</p>}
            {analysis && (
              <>
                <p className="text-xs text-white/50">
                  Observation {analysis.freshness} · inspected {analysis.coverage.inspected_pull_requests.join(", ") || "NONE"} · limit {analysis.coverage.limit}.
                  {" "}{analysis.coverage.migration_ancestry}. {analysis.coverage.founder_decisions}. {analysis.coverage.engineering_lessons}.
                </p>
                <section aria-label="Founder-approved architecture decisions" className="space-y-2 rounded-lg border border-border bg-panel p-3">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <h3 className="text-sm font-medium">Founder-approved architecture decisions</h3>
                    <StatusBadge value={analysis.coverage.canonical_decisions?.status ?? "UNKNOWN"} />
                  </div>
                  {analysis.coverage.canonical_decisions?.decisions.map((decision) => (
                    <article key={`${decision.decision_id}-${decision.version}`} className="rounded border border-border p-3 text-xs">
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <strong>{decision.decision_id} v{decision.version}: {decision.summary}</strong>
                        <StatusBadge value={decision.status} />
                      </div>
                      <p className="mt-1">Repository: {decision.repository} · approver: {decision.approver} · approved {formatDate(decision.approved_at)} · effective {formatDate(decision.effective_at)}</p>
                      {decision.repository_evidence && (
                        <p>Repository scope mismatch: expected {decision.repository_evidence.expected ?? "UNKNOWN"}, recorded {decision.repository_evidence.recorded}</p>
                      )}
                      <p>Canonical: PR #{decision.canonical.pull_request} @ <code>{decision.canonical.head_sha}</code> · {decision.canonical.architecture.join(", ")}</p>
                      <p>Affected modules: {decision.affected_modules.join(", ")}</p>
                      <p>Rejected/superseded: {decision.rejected_alternatives.map((alternative) => `PR #${alternative.pull_request} @ ${alternative.head_sha} (${alternative.label})`).join("; ") || "None recorded"}</p>
                      <p>Relevant PRs: {decision.relevant_pull_requests.map((number) => `#${number}`).join(", ")}</p>
                      <p>Source: {decision.source.type} · {decision.source.ref} · note {decision.source.id} · content SHA-256 <code>{decision.source.content_sha256}</code></p>
                      {decision.pr_evidence?.map((evidence) => (
                        <p key={`${decision.decision_id}-${evidence.number}`} className="break-all">
                          PR #{evidence.number}: expected {evidence.expected_sha}, observed {evidence.observed_sha ?? "UNKNOWN"} ({evidence.state ?? "UNKNOWN"})
                        </p>
                      ))}
                    </article>
                  ))}
                  {(!analysis.coverage.canonical_decisions?.decisions.length) && (
                    <p className="text-xs text-white/50">No machine-readable, authenticated Founder decision was verified. PR prose and registry statements alone are not approval.</p>
                  )}
                  {analysis.coverage.canonical_decisions?.errors?.map((error) => (
                    <p key={error} className="text-xs text-amber-100">Decision evidence: {error}</p>
                  ))}
                </section>
                {analysis.coverage.inspection_errors.map((item, index) => (
                  <p key={index} className="text-xs text-amber-100">PR {item.number ?? "selection"}: {item.errors.join(", ")} {item.numbers?.join(", ")}</p>
                ))}
                {analysis.coverage.migration_graph && (
                  <div className="text-xs text-white/50">
                    <p>Migration graphs: {analysis.coverage.migration_graph.status} {analysis.coverage.migration_graph.errors?.join(", ")}</p>
                    {analysis.coverage.migration_graph.graphs?.map((graph) => (
                      <p key={graph.number}>PR #{graph.number} @ {graph.head_sha}: {graph.status}; heads {graph.heads?.join(", ") || "UNKNOWN"}; missing {graph.missing?.join(", ") || "none observed"}; {graph.errors.join(", ")}</p>
                    ))}
                  </div>
                )}
                {analysis.findings.length === 0 && <Empty>No conflicts observed in the inspected data; completeness and readiness remain UNKNOWN.</Empty>}
                {analysis.findings.map((finding, index) => (
                  <article key={`${finding.kind}-${index}`} className="rounded-lg border border-border bg-panel p-3 text-sm">
                    <div className="flex justify-between gap-3"><strong>{finding.detail}</strong><StatusBadge value={`${finding.severity ?? "P3"} · ${finding.status ?? "UNKNOWN"}`} /></div>
                    <p className="mt-1 text-xs text-white/50">PRs {finding.pull_requests.join(", ")} · {finding.kind} · {finding.blocks_integration ? "integration blocked" : "not an established blocker"}</p>
                    <p className="mt-1">{finding.corrective_action ?? finding.recommendation}</p>
                    <p className="mt-1 text-xs text-white/50">Owner: {finding.owners.map((owner) => `#${owner.number}: ${owner.agent ?? "UNKNOWN"} (${owner.status})`).join("; ")}</p>
                    <p className="mt-1 text-xs text-white/50">Reuse: {finding.reuse}</p>
                    <p className="mt-1 text-xs text-white/50">Tests: {finding.tests.join(" ")}</p>
                    <p className="mt-1 text-xs text-white/50">Downstream revalidation: {finding.downstream_revalidate.join(", ") || "UNKNOWN / none observed"}</p>
                    {finding.evidence.map((evidence, evidenceIndex) => (
                      <p key={evidenceIndex} className="mt-1 break-all text-xs">
                        {evidence.path ?? evidence.reviewed_sha ?? evidence.revision ?? "Evidence"}{" "}
                        {evidence.url && <SourceLink href={evidence.url} />}
                      </p>
                    ))}
                  </article>
                ))}
                <details className="rounded-lg border border-border bg-panel p-3 text-sm">
                  <summary className="cursor-pointer">Dependency graph and readiness ({analysis.candidates.length})</summary>
                  {analysis.candidates.map((candidate) => (
                    <div key={candidate.number} className="border-t border-border py-2 text-xs">
                      <SourceLink href={candidate.url ?? ""} label={`PR #${candidate.number}`} /> · <StatusBadge value={candidate.readiness} />
                      <p className="break-all">HEAD {candidate.head_sha ?? "UNKNOWN"} · base {candidate.base_branch ?? "UNKNOWN"}</p>
                      <p>Dependencies: {candidate.dependencies.map((dep) => `#${dep.number} (${dep.state})`).join(", ") || "UNKNOWN"}</p>
                      <p>Transitive dependency chain: {candidate.dependency_chain?.map((number) => `#${number}`).join(", ") || "UNKNOWN / none observed"}</p>
                      <p>Required upstream: {candidate.required_upstream_candidates?.map((dep) => `#${dep.number} (${dep.state}, ${dep.head_sha ?? "SHA UNKNOWN"})`).join(", ") || "UNKNOWN"} · safe to integrate: {candidate.safe_to_integrate ?? "UNKNOWN"}</p>
                      <p>Exact-HEAD CI: {candidate.ci_state} · {candidate.ci_evidence.length ? candidate.ci_evidence.map((check) => `${check.name ?? "check"}: ${check.state ?? "UNKNOWN"} @ ${check.head_sha ?? "SHA UNKNOWN"}`).join("; ") : "No exact-HEAD CI evidence"}
                        {candidate.ci_evidence.map((check, index) => check.url && (
                          <span key={`${check.name}-${index}`}> · <SourceLink href={check.url} label={`${check.name ?? "check"} run`} /></span>
                        ))}
                      </p>
                      <p>
                        Blueprint source scope: {candidate.blueprint_scope?.status ?? "UNKNOWN"}
                        {candidate.blueprint_scope?.evidence.map((source) => (
                          <span key={source.path}> · <SourceLink href={source.url} label={source.path} /></span>
                        ))}
                      </p>
                      <p>Changed migration revisions: {candidate.migration_dependencies?.map((migration) => `${migration.revision} ← ${migration.down_revision.join(", ") || "root"} (${migration.path})`).join("; ") || "UNKNOWN / none observed"}</p>
                      <p>Owner: {candidate.owner.agent ?? "UNKNOWN"} · Review: {candidate.review_state} · {candidate.security_review}</p>
                      <p>{candidate.readiness_reason}</p>
                    </div>
                  ))}
                </details>
              </>
            )}
          </section>

          <section className="space-y-3" aria-label="Pull requests">
            <SectionHeading title="Open pull requests" note="Head SHAs and timestamps are observations from the configured repository." />
            {pullRequests.length === 0 && <Empty>There are no fetched open PRs, or GitHub data is unavailable.</Empty>}
            {pullRequests.map((pr) => <PullRequestCard key={pr.number} pr={pr} />)}
          </section>

          <div className="grid gap-6 xl:grid-cols-2">
            <section className="space-y-3" aria-label="Blockers">
              <SectionHeading title="Blockers" />
              {snapshot.blockers.length === 0 ? <Empty>No blockers were observed in the available evidence.</Empty> : (
                <div className="space-y-2">
                  {snapshot.blockers.map((blocker, index) => (
                    <div key={`${blocker.kind}-${index}`} className="rounded-lg border border-border bg-panel p-3 text-sm">
                      <div className="flex justify-between gap-3">
                        <span>{blocker.title}</span><StatusBadge value={blocker.status} />
                      </div>
                      <p className="mt-1 text-xs text-white/45">{blocker.kind} · {blocker.evidence}</p>
                      <SourceLink href={blocker.source} />
                    </div>
                  ))}
                </div>
              )}
            </section>

            <section className="space-y-3" aria-label="Recommended next work">
              <SectionHeading title="Next feasible work" note="Evidence-linked suggestions only; nothing is dispatched." />
              {snapshot.recommendations.length === 0 ? <Empty>No next step is verified from the current snapshot.</Empty> : (
                <div className="space-y-3">
                  {snapshot.recommendations.map((item, index) => (
                    <article key={`${item.kind}-${index}`} className="rounded-lg border border-border bg-panel p-3">
                      <div className="flex items-start justify-between gap-3">
                        <h3 className="text-sm font-medium">{item.title}</h3>
                        <StatusBadge value={item.status} />
                      </div>
                      {item.reason && <p className="mt-1 text-xs text-white/50">{item.reason}</p>}
                      <SourceLink href={item.source} />
                      {item.decision_completeness && <DecisionCompletenessView value={item.decision_completeness} />}
                    </article>
                  ))}
                </div>
              )}
            </section>
          </div>

          <div className="grid gap-6 xl:grid-cols-2">
            <section className="space-y-3" aria-label="Capabilities">
              <SectionHeading title="Recorded capabilities" note="Statuses come from canonical capability records; missing records are not inferred." />
              {snapshot.capabilities.length === 0 ? <Empty>No capability records are available.</Empty> : (
                <div className="space-y-2">
                  {snapshot.capabilities.map((capability) => (
                    <article id={`capability-${capability.id}`} key={capability.id} className="rounded-lg border border-border bg-panel p-3">
                      <div className="flex items-center justify-between gap-3">
                        <a className="text-sm font-medium underline decoration-white/25" href={capability.source.href}>
                          {capability.key}
                        </a>
                        <StatusBadge value={capability.status} />
                      </div>
                      <p className="mt-1 text-xs text-white/50">{capability.domain} · tests: {capability.test_state}</p>
                      {capability.status_reason && <p className="mt-1 text-sm text-white/70">{capability.status_reason}</p>}
                      {capability.last_verified_at && <p className="mt-1 text-xs text-white/40">Verified {formatDate(capability.last_verified_at)}</p>}
                      {capability.known_limitations.length > 0 && (
                        <p className="mt-1 text-xs text-amber-100">Limitations: {capability.known_limitations.join(", ")}</p>
                      )}
                    </article>
                  ))}
                </div>
              )}
            </section>

            <section className="space-y-3" aria-label="Agents and assignments">
              <SectionHeading title="Registered agents and assignments" note="Registration is not evidence of live agent availability or activity." />
              {snapshot.agents.length === 0 && snapshot.assignments.length === 0 ? <Empty>No agent profiles or active assignments are recorded.</Empty> : (
                <>
                  {snapshot.agents.map((agent) => (
                    <div key={agent.id} className="rounded-lg border border-border bg-panel p-3 text-sm">
                      <div className="flex justify-between gap-3">
                        <a href={agent.source.href} className="underline decoration-white/25">{agent.name} ({agent.key})</a>
                        <StatusBadge value={agent.runtime_status} />
                      </div>
                      <p className="mt-1 text-xs text-white/45">{agent.adapter_kind} · registry {agent.registry_status} · {agent.availability_reason}</p>
                      {agent.heartbeat_at && <p className="mt-1 text-xs text-white/40">Heartbeat {formatDate(agent.heartbeat_at)}</p>}
                    </div>
                  ))}
                  {snapshot.assignments.map((assignment) => (
                    <div key={assignment.id} className="rounded-lg border border-border bg-panel p-3 text-sm">
                      <div className="flex justify-between gap-3">
                        <a href={assignment.source.href} className="underline decoration-white/25">
                          {assignment.agent_key} · {assignment.role}
                        </a>
                        <StatusBadge value={assignment.status} />
                      </div>
                      <p className="mt-1 text-xs text-white/45">
                        Runtime view: {assignment.runtime_status}
                        {assignment.branch && <> · branch {assignment.branch}</>}
                        {assignment.head_sha && <> · SHA <code>{assignment.head_sha}</code></>}
                      </p>
                      {assignment.block_reason && <p className="mt-1 text-xs text-amber-100">{assignment.block_reason}</p>}
                    </div>
                  ))}
                  {snapshot.agent_results.length > 0 && (
                    <div className="space-y-2 pt-2">
                      <h3 className="text-xs uppercase text-white/45">Recorded agent result and usage evidence</h3>
                      {snapshot.agent_results.map((result) => (
                        <article key={result.id} className="rounded-lg border border-border bg-panel p-3 text-xs">
                          <div className="flex flex-wrap justify-between gap-2">
                            <span>Assignment <code>{result.assignment_id}</code></span>
                            <StatusBadge value={result.verification_status} />
                          </div>
                          <p className="mt-1 text-white/50">
                            Observed {formatDate(result.observed_at)} · evidence {result.evidence_kind} · review kind {result.review_kind}
                          </p>
                          <p className="mt-1 text-white/60">
                            Tests {result.tests_passed === null ? "UNKNOWN" : result.tests_passed ? "reported passed" : "reported failed"}
                            {" "}({result.tests_run ?? "UNKNOWN"} run, {result.tests_failed ?? "UNKNOWN"} failed) · CI {result.ci_conclusion ?? "UNKNOWN"}
                          </p>
                          <p className="mt-1 text-white/60">
                            Usage {result.cost_tokens ?? "UNKNOWN"} tokens · ${result.cost_usd ?? "UNKNOWN"} · {result.duration_seconds ?? "UNKNOWN"}s
                          </p>
                          <p className="mt-1 text-white/40">
                            Source evidence <code>{result.id}</code> · deterministic record: {result.deterministic ? "yes" : "no"}; result is not independently verified.
                          </p>
                          <a href={result.source.href} className="mt-1 inline-block text-accent2 underline">Open agent registry</a>
                        </article>
                      ))}
                    </div>
                  )}
                </>
              )}
            </section>
          </div>

          <section className="space-y-3" aria-label="Work candidates and decision completeness">
            <SectionHeading title="Project work candidates" note="Candidate priority labels are not recalculated unless all sourced factors are present." />
            {snapshot.work_candidates.length === 0 ? <Empty>No durable work candidates are recorded.</Empty> : (
              <div className="space-y-3">
                {snapshot.work_candidates.map((candidate) => (
                  <article id={`candidate-${candidate.id}`} key={candidate.id} className="rounded-lg border border-border bg-panel p-3">
                    <div className="flex flex-wrap items-center justify-between gap-3">
                      <a href={candidate.source.href} className="text-sm font-medium underline decoration-white/25">{candidate.title}</a>
                      <div className="flex gap-2"><StatusBadge value={candidate.status} /><StatusBadge value={candidate.priority} /></div>
                    </div>
                    <p className="mt-1 text-xs text-white/45">
                      Classifier confidence: {candidate.classifier_confidence ?? "UNKNOWN"} · source-grounded priority completeness:
                    </p>
                    <DecisionCompletenessView value={candidate.decision_completeness} />
                  </article>
                ))}
              </div>
            )}
          </section>

          <div className="grid gap-6 xl:grid-cols-2">
            <section className="space-y-3" aria-label="Recent activity">
              <SectionHeading title="Latest observed activity" />
              {snapshot.activity.length === 0 ? <Empty>No recent GitHub activity was fetched.</Empty> : (
                <div className="space-y-2">
                  {snapshot.activity.map((item, index) => (
                    <div key={`${item.kind}-${index}`} className="flex flex-wrap justify-between gap-2 rounded-lg border border-border bg-panel p-3 text-sm">
                      <SourceLink href={item.source ?? ""} label={item.title} />
                      <span className="text-xs text-white/40">{formatDate(item.timestamp)}</span>
                      {item.head_sha && <code className="w-full text-xs text-white/45">HEAD {item.head_sha}</code>}
                    </div>
                  ))}
                </div>
              )}
            </section>
            <section className="space-y-3" aria-label="Decision debt">
              <SectionHeading title="Existing decision debt" note="Reused from the executive why-graph service; not an independent review." />
              {snapshot.decision_debt.items.length === 0 ? <Empty>No active or disputed decision notes were returned.</Empty> : (
                <div className="space-y-2">
                  {snapshot.decision_debt.items.map((item) => (
                    <div key={item.note_id} className="rounded-lg border border-border bg-panel p-3 text-sm">
                      <div className="flex justify-between"><span>{item.preview}</span><StatusBadge value={`${item.impact} impact · ${item.status}`} /></div>
                      <code className="mt-1 block text-xs text-white/40">{item.note_id}</code>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>
        </>
      )}
    </div>
  );
}

function PullRequestCard({ pr }: { pr: GitHubOperationsPullRequest }) {
  const failedChecks = pr.checks.filter(
    (check) => check.state === "failed" && check.is_latest_for_check !== false,
  );
  return (
    <article className="rounded-xl border border-border bg-panel p-4" data-testid={`pull-request-${pr.number}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-medium">
            <ExternalLink href={pr.url ?? ""}>#{pr.number} {pr.title}</ExternalLink>
            {pr.draft && <span className="ml-2 text-xs text-amber-200">DRAFT</span>}
          </h2>
          <p className="mt-1 text-xs text-white/50">
            @{pr.author ?? "UNKNOWN"} · {pr.head_branch ?? "UNKNOWN"} → {pr.base_branch ?? "UNKNOWN"} · updated {formatDate(pr.updated_at)}
          </p>
          <p className="mt-1 break-all text-xs text-white/45">
            HEAD <code>{pr.head_sha ?? "UNKNOWN"}</code>
            {pr.latest_commit?.committed_at && (
              <> · commit {formatDate(pr.latest_commit.committed_at)}
                {pr.latest_commit.url && <> · <ExternalLink href={pr.latest_commit.url}>source</ExternalLink></>}
              </>
            )}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <StatusBadge value={pr.review_state} />
          <StatusBadge value={`CI ${pr.ci_state}`} />
          <StatusBadge value={pr.mergeability.state} />
          {failedChecks.length > 0 && <StatusBadge value={`${failedChecks.length} failed checks`} />}
        </div>
      </div>
      <p className="mt-2 text-xs text-white/40">
        Review state is a GitHub observation, not an independent review or release approval.
      </p>
      {pr.head_sha_changed_since_list && (
        <p className="mt-2 text-xs text-amber-100">
          The PR head changed between list and detail reads; SHA-bound evidence is tied to the newer detail-read SHA shown above.
        </p>
      )}
      {pr.head_sha_changed_during_detail && (
        <p className="mt-2 text-xs text-amber-100">
          The PR head changed during this snapshot. Earlier SHA-bound checks, workflow runs, commit, and review status were invalidated.
        </p>
      )}
      {pr.dependencies.length > 0 && (
        <div className="mt-3 text-sm">
          <strong className="text-xs text-white/50">Declared dependencies</strong>
          <ul className="mt-1 flex flex-wrap gap-3">
            {pr.dependencies.map((dependency) => (
              <li key={dependency.number}>
                <ExternalLink href={dependency.url}>PR #{dependency.number} · {dependency.state}</ExternalLink>
              </li>
            ))}
          </ul>
        </div>
      )}
      {pr.dependencies.length === 0 && (
        <p className="mt-2 text-xs text-white/40">
          No explicit dependency was found in the PR body; prerequisite completeness is UNKNOWN.
        </p>
      )}
      <div className="mt-3 grid gap-4 lg:grid-cols-2">
        <div>
          <h3 className="mb-1 text-xs uppercase text-white/45">CI evidence for {pr.head_sha ?? "UNKNOWN HEAD"}</h3>
          <CheckList checks={pr.checks} empty="No check data fetched (UNKNOWN, not passed)." />
          {pr.workflow_runs.map((run) => (
            <div key={run.id} className="mt-2 border-l border-border pl-3 text-xs">
              <ExternalLink href={run.url ?? ""}>{run.name ?? `Workflow run ${run.id}`}</ExternalLink>
              <span className="ml-2 text-white/50">
                {run.is_latest_for_workflow ? "latest" : "historical"} · {run.status} / {run.conclusion ?? "pending"}
              </span>
              <CheckList checks={run.jobs} empty="Jobs not returned." />
            </div>
          ))}
        </div>
        <div>
          <h3 className="mb-1 text-xs uppercase text-white/45">GitHub review evidence · not independent approval</h3>
          {pr.reviews.length === 0 ? <p className="text-xs text-white/45">No review was fetched; review state is not verified.</p> : (
            <ul className="space-y-1 text-xs">
              {pr.reviews.map((review, index) => (
                <li key={review.id ?? index}>
                  {review.author ?? "UNKNOWN"} · {review.state} · {formatDate(review.submitted_at)}
                  {" "}· commit {review.commit_sha ?? "UNKNOWN"}
                  {review.url && <> · <ExternalLink href={review.url}>source</ExternalLink></>}
                </li>
              ))}
            </ul>
          )}
          {pr.detail_errors.length > 0 && <p className="mt-2 text-xs text-amber-100">Some details unavailable: {pr.detail_errors.join(", ")}</p>}
        </div>
      </div>
      {pr.comments.length > 0 && (
        <details className="mt-3 border-t border-border pt-3">
          <summary className="cursor-pointer text-xs text-white/55">PR comments (untrusted source text)</summary>
          <ul className="mt-2 space-y-2">
            {pr.comments.map((comment, index) => (
              <li key={comment.id ?? index} className="rounded bg-black/20 p-2 text-xs">
                <p className="text-white/45">{comment.author ?? "UNKNOWN"} · {formatDate(comment.created_at)}</p>
                <p className="mt-1 whitespace-pre-wrap break-words">{comment.body}</p>
                {comment.url && <ExternalLink href={comment.url}>Open comment</ExternalLink>}
              </li>
            ))}
          </ul>
        </details>
      )}
      {pr.details_truncated && (
        <p className="mt-3 text-xs text-amber-100">
          Some check, workflow, review, or comment history was omitted at the configured fetch limit.
        </p>
      )}
    </article>
  );
}

function DecisionCompletenessView({ value }: { value: DecisionCompleteness }) {
  return (
    <details className="mt-2 border-t border-border/70 pt-2 text-xs">
      <summary className="cursor-pointer text-white/65">
        Decision completeness: {value.status} · priority {value.priority.status}
        {value.priority.horizon && ` (${value.priority.horizon}, ${value.priority.score})`}
      </summary>
      <div className="mt-2 grid gap-3 md:grid-cols-2">
        <div>
          <div>
            <strong>Factors evaluated:</strong>
            {value.factors_evaluated.length ? (
              <ul className="mt-1">
                {value.factors_evaluated.map((factor) => (
                  <li key={factor.name}>
                    {factor.name} = {factor.value} · <code>{factor.source_ref}</code> · {formatDate(factor.observed_at)}
                  </li>
                ))}
              </ul>
            ) : " NONE"}
          </div>
          <p className="mt-1"><strong>Missing:</strong> {value.factors_missing.join(", ") || "NONE"}</p>
          <p className="mt-1"><strong>Stale:</strong> {value.factors_stale.join(", ") || "NONE"}</p>
          <p className="mt-1"><strong>Freshness unknown:</strong> {value.factors_freshness_unknown.join(", ") || "NONE"}</p>
          <p className="mt-1"><strong>Not applicable:</strong> {value.factors_not_applicable.join(", ") || "NONE"}</p>
          <p className="mt-1"><strong>Assumed/unsourced:</strong> {value.factors_assumed_or_unsourced.join(", ") || "NONE"}</p>
          <p className="mt-1"><strong>Impact unknown:</strong> {value.impact_unknown_for.join(", ") || "NONE"}</p>
        </div>
        <div>
          <p><strong>Alternatives:</strong> {value.alternatives.status}</p>
          {value.alternatives.items.length > 0 && (
            <ul className="mt-1">
              {value.alternatives.items.map((alternative, index) => (
                <li key={`${alternative.source_ref ?? "alternative"}-${index}`}>
                  {alternative.description ?? "Alternative"} · <code>{alternative.source_ref ?? "SOURCE_UNKNOWN"}</code>
                  {alternative.observed_at && <> · {formatDate(alternative.observed_at)}</>}
                </li>
              ))}
            </ul>
          )}
          <p className="mt-1"><strong>Re-evaluation:</strong> {value.reevaluation.status} — {value.reevaluation.reason}</p>
          <p className="mt-1"><strong>Research:</strong> {value.research.status} — {value.research.reason}</p>
          <p className="mt-1"><strong>Authority granted:</strong> {value.authority_granted ? "YES" : "NO"}</p>
          <ul className="mt-1">
            {Object.entries(value.mandatory_constraints).map(([name, constraint]) => (
              <li key={name}>
                {name}: {constraint.status}{constraint.reason ? ` — ${constraint.reason}` : ""}
                {constraint.source_ref && <> · <code>{constraint.source_ref}</code></>}
                {constraint.observed_at && <> · {formatDate(constraint.observed_at)}</>}
              </li>
            ))}
          </ul>
          {value.priority.explanation && (
            <p className="mt-2"><strong>Priority explanation:</strong> {JSON.stringify(value.priority.explanation)}</p>
          )}
        </div>
      </div>
    </details>
  );
}

function CheckList({ checks, empty }: { checks: GitHubOperationsCheck[]; empty: string }) {
  if (!checks.length) return <p className="text-xs text-white/40">{empty}</p>;
  return (
    <ul className="space-y-1 text-xs">
      {checks.map((check, index) => (
        <li key={`${check.name}-${index}`} className="flex flex-wrap justify-between gap-2">
          {check.url ? <ExternalLink href={check.url}>{check.name ?? "Unnamed check"}</ExternalLink> : <span>{check.name ?? "Unnamed check"}</span>}
          <span className={check.state === "failed" ? "text-red-200" : "text-white/50"}>
            {check.status ?? "UNKNOWN"} / {check.conclusion ?? "pending"} · {check.state}
            {check.is_latest_for_check === false ? " · historical" : ""}
          </span>
        </li>
      ))}
    </ul>
  );
}

function SourceLink({ href, label = "Open source" }: { href: string; label?: string }) {
  if (href.startsWith("https://github.com/")) return <ExternalLink href={href}>{label}</ExternalLink>;
  if (href.startsWith("/api/")) return <a className="mt-1 inline-block text-xs text-accent2 underline" href={href}>{label}</a>;
  return <span className="mt-1 block text-xs text-white/35">Source unavailable</span>;
}

function ExternalLink({ href, children }: { href: string; children: React.ReactNode }) {
  if (!href.startsWith("https://github.com/")) return <span className="text-white/70">{children}</span>;
  return <a className="text-accent2 underline decoration-accent2/40 underline-offset-2" href={href} target="_blank" rel="noreferrer">{children}</a>;
}

function Metric({ label, value }: { label: string; value: number }) {
  return <div className="rounded-lg border border-border/70 p-3">
    <div className="text-xs uppercase text-white/40">{label}</div>
    <div className="mt-1 text-xl font-semibold">{value}</div>
  </div>;
}

function SectionHeading({ title, note }: { title: string; note?: string }) {
  return <div>
    <h2 className="text-lg font-medium">{title}</h2>
    {note && <p className="mt-1 text-xs text-white/45">{note}</p>}
  </div>;
}

function StatusBadge({ value }: { value: string }) {
  const tone = /failed|blocked|conflict|stale|unknown/i.test(value)
    ? "border-red-400/30 bg-red-500/10 text-red-100"
    : /success|approved|complete|live|merged|verified|ready/i.test(value)
      ? "border-emerald-400/30 bg-emerald-500/10 text-emerald-100"
      : "border-border bg-white/5 text-white/70";
  return <span className={`inline-flex rounded-full border px-2 py-0.5 text-[11px] ${tone}`}>{value}</span>;
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="rounded-lg border border-border bg-panel p-4 text-sm text-white/45">{children}</p>;
}

function formatDate(value: string | null) {
  if (!value) return "UNKNOWN";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "UNKNOWN" : date.toLocaleString();
}

function freshnessClass(freshness: string) {
  if (freshness === "live") return "border-emerald-400/30 bg-emerald-500/5 text-emerald-100";
  if (freshness === "cached_fresh") return "border-sky-400/30 bg-sky-500/5 text-sky-100";
  if (freshness === "stale") return "border-amber-400/30 bg-amber-500/5 text-amber-100";
  return "border-red-400/30 bg-red-500/5 text-red-100";
}
