# Life Development Monitor

**Status: FUTURE / DEFERRED.** This is a requirements and architecture note only. It adds
no code, schema, scheduled job, GitHub permission, notification, or execution capability.
`first_cycle_go=false` remains unchanged.

## Purpose and product boundary

Life Development Monitor is a future capability for observing verifiable changes in LifeAI's
engineering work and presenting useful, source-grounded summaries to the Founder. It belongs
within Life Forge / Engineering under the broader Life Universe vision; it is not another
agent, task queue, approval system, or supervisor.

The monitor's job is to collect evidence, explain why a change matters, and recommend safe
next actions. It must not merge or deploy, approve privileged workflows, change security
gates, or make external-agent activity appear more certain than the evidence supports.

This scope is deliberately limited to engineering evidence. Security Lab may consume relevant
security findings through its own authorized review path. Geo, patent research, voice/media,
and multi-coin compute/mining remain separate future capabilities with their own data,
provenance, permission, and cost decisions; this document does not couple them to GitHub
monitoring or grant them authority.

## Existing foundations and their limits

| Foundation | What it provides | What it does not provide |
|---|---|---|
| PR #395, `GitHubClient` | Read methods for repository-wide pull requests, issues, and commits, plus existing reads for a known ref/PR and check runs | It is not a complete monitor. The client also contains separately gated write methods; a monitor must not receive write authority. The new read surface does not yet cover all workflow runs, security findings, or changed-file evidence. |
| PR #396, `build_github_work_queue_report()` | A stateless report of open PRs/issues with deterministic draft, inactivity, check-run, and merge-conflict signals | It does not retain history, deduplicate notifications, create work candidates/goals, or claim authority. Its current report shape does not preserve all exact commit, run, and observation provenance required by this monitor. |
| `mainai_jobs` and the existing worker | Durable jobs, leases, event history, restart/retry behavior, and owner context for approved job types | They are not permission to create a competing poller, queue, or supervisor. Any future scheduled monitor job must use the existing runtime only after its capability and authorization are explicitly reviewed. |
| MainAI goal/task approval and execution-scope flow | The canonical path for authorized work, including Founder approval boundaries | GitHub evidence is not itself an authorized task. The monitor must not create goals or tasks directly from an observation. |
| `AgentWorkAssignment` coordination | Coordinates already-authorized work and describes assignment scope | It neither mints work nor proves that a provider UI session or external agent process is alive. |

PR #396 explicitly leaves open whether GitHub-derived proposals need their own Founder-
reviewable staging model. This document does not resolve that decision. In particular, do not
misrepresent live GitHub state as a Founder `KnowledgeClaim` or force it into `WorkCandidate`:
that model is owner-anchored to Founder knowledge, while a GitHub PR/issue/commit is external
system state.

## Future evidence and monitoring requirements

1. **Read the source systems directly.** Use narrowly authorized, read-only GitHub access for
   PRs, commits, CI workflow/check runs, security findings, and repository changes. Build on
   #395's GET evidence surface and #396's deterministic report rather than creating a parallel
   GitHub client. Add only the read endpoints and permissions that an approved use case needs.
2. **Preserve provenance.** Every stored or displayed observation must retain repository
   identity, evidence type, immutable SHA where applicable, PR/issue/check/workflow run ID,
   source URL, source timestamps, observation timestamp, retrieval status, and the permission
   scope used. Retain the source reference and observation history; do not silently rewrite an
   old observation as current state. Security alert payloads must not expose secret values.
3. **Detect meaningful changes, not repeated snapshots.** Compare observations and surface
   material transitions such as a new head SHA, completed/failed CI, a new or regressed security
   finding, a repository change, or inactivity beyond a stated threshold. Identical repeated
   reads must not create duplicate events or notifications. Notify only for material,
   actionable transitions through an approved existing notification path; do not introduce a
   competing notification bus. Any future retention, deduplication key, and notification
   cooldown policy requires an explicit design and privacy review.
4. **Explain each signal.** Show the source evidence, when it was observed, why the change
   matters, uncertainty or missing coverage, and a safe recommended next action. A
   recommendation is not an execution request or authorization.
5. **Use MainAI's existing authority path.** A future proposal may be presented through
   MainAI's authorized planning boundary. Creating durable work must follow the existing
   Founder-governed goal, scope, task-approval, and spend gates. Until the proposal staging
   question in #396 is decided, GitHub-derived output remains a report, not a persisted
   candidate or task.
6. **Keep one operations model.** If polling or durable event processing is later approved,
   use existing jobs, leases, event history, and Operations Center visibility. Do not add a
   second queue, agent supervisor, or independent authorization mechanism.

## Distinguish GitHub evidence from agent and worker state

GitHub evidence can establish repository facts: a commit exists, a PR changed state, a check
run completed, or an alert was reported. It cannot establish that Claude, Codex, Copilot, or
another external agent session is currently open, available, running, stalled, or able to
resume.

Show provider session state only when a supported, authorized provider/runtime interface
actually reports it, with its source and observation time. Otherwise label it **unknown / not
observable**. A recent commit, open PR, CI check, branch, or prior worker heartbeat is not
proof that an agent is currently working. Keep agent sessions, durable worker execution, CI,
and queued work as distinct states.

## Security and non-authority constraints

- The monitor receives read-only GitHub credentials or an equivalent deny-write adapter; it
  must not inherit the existing client's write-enabled configuration merely because that
  client also supports guarded mutations.
- Use least privilege for security-alert and workflow evidence. Respect repository access,
  redact secret-bearing alert details, and fail visibly when permissions or API coverage are
  missing.
- Never collect or store reusable login credentials or session cookies, and do not infer
  provider session state by scraping a provider's user interface.
- No automatic merge, deployment, privileged-workflow approval, branch protection change,
  security-gate modification, provider-session bypass, or external spending.
- No frontend-only authorization and no inference that an observation is an approval.
- Preserve `first_cycle_go=false`; a monitor signal cannot satisfy a release gate.

## Architecture references

- `docs/MAINAI_ARCHITECTURE.md` — event flow, security model, and backend-owned permissions.
- `docs/MAINAI_JOB_RUNTIME.md` — durable job, lease, restart, and truthful status contracts.
- `docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md` — coordination is above canonical work and does
  not mint tasks or invoke agents.
- `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §4.8 — future GitHub snapshots as a source type
  require source identity and provenance; this monitor does not implement that source model.
- PR #395 — read-only repository-wide GitHub evidence methods.
- PR #396 — stateless work-queue evidence report and its unresolved proposal-staging decision.

**Implementation remains deferred** until the #396 dependency is reviewed and merged and the
Founder-approved proposal, evidence-retention, permission, and notification decisions are
available. This document is not a security acceptance or a release authorization.
