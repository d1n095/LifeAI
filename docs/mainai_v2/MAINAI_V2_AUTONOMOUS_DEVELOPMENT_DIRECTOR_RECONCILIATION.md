# MainAI V2 — Autonomous Development Director: Architecture Reconciliation

Founder directive (2026-09-07): build the isolated foundation for MainAI to operate as an
autonomous development director. Explicit instruction: "Do not create a duplicate
orchestration stack if one already exists. Build on canonical structures where semantically
correct." This document is the audit and canonical decision, written before any code — same
discipline as `MAINAI_V2_INTENT_GOAL_RECONCILIATION.md`, at a larger scale.

Four parallel read-only audits (development_supervisor/development_driver;
mainai_execution; workforce/execution_envelopes/evidence; provider
selection/spend/readiness/broker) confirmed, by direct code reading, that **most of what
this task describes already exists — much of it as LIVE PRODUCTION CODE, not V2 sandbox
code.** This is the single most important finding: the job here is composition and a genuinely
new coordination tier ABOVE existing infrastructure, not a parallel system.

## 0. What already exists — confirmed by direct reading, not assumed

### Already exists, LIVE PRODUCTION (wired into `app.worker.py`, not isolated)

- **`app.development_supervisor`** — `run_authorized_goal_supervisor_tick()` already IS a
  select→assign→execute→(mechanically)verify→next loop, single-`MainAIGoal`-scoped: claim a
  real DB-backed goal lease (`supervisor_goal_leases`, atomic
  `INSERT...ON CONFLICT...DO UPDATE WHERE expires_at < now()`, generation-fenced, crash-safe
  stale-lease takeover) → re-verify authority against the live `ExecutionAuthorizationEnvelope`
  → select bindable tasks → sync a real git worktree
  (`production_worktree.py`: `ensure_goal_worktree_sync`, `goal_branch_name`,
  `goal_worktree_path`, `reset_goal_worktree_to_clean_head`) → build a real `SupervisorScope`
  from the live envelope → execute bounded tasks via `app.development_operator` (real file
  writes, real git, real subprocess pytest via `COMMAND_PROFILES`) → release the lease.
- **`SupervisorScope`** — already exactly the "provider lease" shape this task asks for:
  `owner_id`, `goal_id`, `authority_kind`, `authority_ref`, `authorized_instruction_sha256`,
  `repository_identity`, `allowed_paths`, `allowed_capabilities`, `maximum_risk`,
  `completion_criteria`, `life_intent_id`, `self_work`, `provider_spend_authorized`.
  Reconstructed fresh from the live envelope at every dispatch boundary — never trusted stale.
- **`app.mainai_execution`** — `MainAIGoal`/`MainAIPlan`/`MainAITask` with a complete,
  already-richer-than-asked-for lifecycle: `graph.py`'s `recompute_task_readiness()`/
  `next_ready_task()` is a real, working dependency-aware priority dispatch engine (nothing
  new needed here); `verify.py`'s `verify_task()` is a real, separate (though mechanical,
  same-context) gate — never self-reported by the task handler; `MainAITaskEvent`'s full
  vocabulary already covers created/ready/dispatched/verification/blocked/replanned/retry/
  approval/wait/cancel/auto-recovery/lesson-conflict; `EngineeringLesson`'s MISS→ROOT_CAUSE→
  LESSON→RETRIEVAL loop is genuinely wired into `create_plan()` via
  `apply_lessons_to_verification_plan()`; `approval.py`'s `APPROVAL_POLICIES` registry
  already distinguishes `standard_repo_work` (human reviewed the goal) from
  `autonomous_development_work` (explicitly for `SupervisorScope`-governed work with no
  per-task human review) — a real, live, embryonic autonomy-level concept;
  `recovery_takeover.py` already implements RESTART != REPLAY AUTHORITY as a hard rule:
  `goal_has_ever_been_envelope_governed()` REFUSES automatic takeover outright for any goal
  that was ever under a real authorization envelope, a dead job's verdict is never fabricated
  ("a dead job proves nothing"); `final_report.py`'s `generate_goal_report()` is already a
  near-complete per-goal Founder Brief, deliberately keeping execution-attempt status/task
  outcome/verification outcome/approval state separate (a founder-caught mistake from
  collapsing them, already fixed).
- **`app.workforce`** — `TaskScopedAuthority` (`allowed_read_paths`, `allowed_write_paths`,
  `allowed_tool_classes`, `allowed_network_destinations`, `allowed_project_ids`,
  `spend_ceiling_usd`, `allow_execution_effects`, `expires_at`) backed by the real
  `WorkforceAssignment` model — a second, already-real provider-lease shape.
  `broker.resolve_delegation()`/`mark_verification()` and, especially,
  `verification.apply_verification_decision()` already implement BUILDER != EXAMINER as a
  real, risk-tiered POLICY ENGINE: `policy_for_risk()` (low/medium/high) controls
  `require_independent_verifier`/`require_two_agent_agreement`/`require_test_evidence`/
  `require_deterministic_validator`/`require_founder_approval` — high risk requires ALL of
  these. Collusion is blocked explicitly (verifier != builder, verifier1 != verifier2). Every
  decision is a durable `WorkforceVerificationDecision` row with a full `policy_snapshot`.
  **This is already almost exactly the founder's "AUTONOMY LEVELS" ask, expressed as risk
  tiers rather than a numbered 0-4 enum.**
- **`app.execution_envelopes`** — `propose_execution_scope()` (never grants) →
  `authorize_execution_scope()` (the only real grant path) — exactly TASK ASSIGNED != TASK
  AUTHORIZED, already live.
- **`app.provider_spend`/`app.provider_planning`** — a real, working reserve → settle/release
  two-phase budget system (`ProviderSpendAuthorization`: `max_cost_usd`, `max_requests`,
  `max_cost_per_request_usd`, provider/model allow-lists, an authority fingerprint,
  `provider_spend_is_live()`) — but scoped to LLM API calls (chat/embed) specifically, not a
  general job/program budget.
- **`app.mainai_startup_readiness.ReadinessLevel`** — `BLOCKED → READY_FOR_SAFE_INTERNAL_RUN
  → READY_FOR_LOW_RISK_PROVIDER_RUN → READY_FOR_SERIOUS_AUTONOMOUS_RUN`, cumulative,
  fail-closed-on-unknown tiers gated by real checks (workforce foundation, vault egress,
  authority boundaries, spend controls, provider delegation safety, Claude reviews, recovery,
  memory truth, self-model evidence, blocking migrations). **This is the closest existing
  analog to "Autonomy Levels" and should be extended/mapped onto, never duplicated.**
- **`app.autonomous_gap.GapGenerationBounds`** — a real, proven bounded-generation technique
  (`max_gaps_per_run`, `max_children_per_run`, `max_generation_depth`, `max_elapsed_seconds`,
  `max_unresolved_gaps`) — the pattern to mirror for Program-level generation bounds, not a
  job-selection engine itself (it has no priority/dependency awareness).
- **`app.capability_reality`** — real per-owner/domain capability observation/gap tracking —
  a genuine precedent for "capability profiles," not yet wired to workforce's own selector.

### Confirmed genuinely NEW — does not exist anywhere in this codebase

- A durable **Program** concept wrapping MULTIPLE goals under one autonomy/budget/protected-ref
  envelope — everything above is single-goal- or single-assignment-scoped.
- A genuinely **independent, cross-identity adversarial examiner** — `verify_task()`/
  `apply_verification_decision()` are real and solid but operate mechanically/in the same
  execution substrate; an agent-vs-agent "different provider/session attacks a frozen SHA"
  layer, matching this session's OWN actually-lived BUILDER != FINAL EXAMINER practice
  (Claude builds PR #245's fixes, a separate session/provider must certify), does not exist.
- **Multi-provider, capability-profile-based BUILDER selection** (Codex/Claude/Cursor/local
  as code-building agents) — today's only "provider selection" is which LLM API answers a
  chat/embed call; there is no concept of delegating actual code-building work to an
  external agent identity with its own lease.
- **Protected-ref declarations** (`#245`-style "do not modify/merge/rebase this SHA") — zero
  hits anywhere in production code.
- **Founder Offline Mode** as an ongoing operating state (`ReadinessLevel` gates whether
  autonomous work may run at all; it does not distinguish "what's allowed while unattended"
  as its own axis) and a **cross-goal, cross-time-window Founder Brief** (existing
  `final_report.py` is single-goal).
- A **GitHub PR-creation broker** — no `gh pr create`/GitHub-token-handling service exists in
  `app/` at all; PR creation has so far always been a human/agent running `gh` directly.
- **External-provider usage/failover states** (AVAILABLE/RATE_LIMITED/USAGE_EXHAUSTED/
  OFFLINE/FAILED/QUARANTINED) — nothing like this exists for external agents today.

### Two real, pre-existing architectural risks — flagged, not silently resolved

1. **Two parallel spend systems already exist**: `app.provider_spend` (LLM API calls) and
   `app.workforce`'s own `cost.py`/`TaskScopedAuthority.spend_ceiling_usd` (workforce
   assignments). Not confirmed unified. A new Program-level budget envelope must NOT become a
   THIRD parallel ledger — it is designed here as a higher-level ceiling that REFERENCES
   both, enforcing nothing new against provider-call spend or assignment spend directly this
   round (real-time cross-system enforcement is explicitly out of scope, flagged as future
   integration work, not silently built as a third system).
2. **PR #245's DB-backed `workforce_authority_epoch` fix and `evidence_claim.py`
   (`evidence_supports_claim()`) do NOT exist on this branch.** They live only on the
   separate, frozen, still-uncertified `claude/final-blocker-closeout` branch (SHA
   `818dfb7`), never merged into `claude/det-kommer-mer-879lcm` (this branch's ancestor).
   **This round's kill-switch/completion-evidence design must NOT assume either exists,
   must NOT import from the `#245` branch, and must build fresh, isolated equivalents** —
   explicitly noted as intended to be reconciled with the real #245 mechanisms once that
   branch is independently certified and merged, not duplicated permanently.

## 1. The decision

A new, isolated package, `app/dev_director/` — matching the established V2 convention
(types.py, focused modules, service.py, hash-chained receipts, closed lifecycle enums with
explicit transition tables, `to_snapshot`/`from_snapshot`) — implementing ONLY the genuinely
new coordination tier, composing with (never duplicating) the real infrastructure in §0:

1. **`Program`** — new. References `goal_ids` (a tuple of real `MainAIGoal.id` values,
   never a copy of goal data) plus a NEW autonomy envelope, protected-ref list, and budget
   ceiling. PROGRAM STATE != EXECUTION AUTHORITY: a Program's own fields never themselves
   authorize anything — every actual execution still goes through the real
   `execution_envelopes`/`SupervisorScope`/`TaskScopedAuthority` machinery unchanged.
2. **`AutonomyLevel`** — new, explicit 0-4 enum, but its `required_readiness_level` field
   maps onto the REAL `ReadinessLevel` tiers, and its policy semantics reuse
   `approval.py`'s real `standard_repo_work`/`autonomous_development_work` distinction rather
   than inventing a third, competing approval concept. Levels 3/4 (auto-merge/deploy)
   default OFF, require an explicit, separately-tracked founder-authorization field —
   AUTONOMY LEVEL != AUTHORITY TOKEN, matching the founder's own invariant.
3. **`Job`** — new, but a thin reference layer over `MainAIGoal`/`MainAITask`
   (`goal_ref: uuid.UUID | None`, `task_ref: uuid.UUID | None` — a Job may reference an
   EXISTING goal/task or describe one not yet created; it never becomes a second source of
   truth for what's being worked on, same doctrine as `IntentObject`'s own resolution in the
   Intent/Goal reconciliation). New states (`PLANNED/READY/ASSIGNED/RUNNING/
   WAITING_FOR_RESULT/VERIFYING/NEEDS_FIX/BLOCKED/FAILED/CERTIFIED/CANCELLED/SUPERSEDED`)
   are Director-layer-only — `CERTIFIED` in particular is a wholly new concept (no existing
   status means "an independent examiner attacked this exact SHA and passed it").
4. **Builder/Examiner separation** — the core new mechanism. A `BuilderAssignment`
   references a real `WorkforceAssignment` (local) or a new `ExternalProviderLease` (Codex/
   Cursor/etc.) and produces a `CompletionEvidence` record at a frozen SHA. A SEPARATE
   `ExaminerAssignment` — different agent/provider identity, never the builder's own — attacks
   that EXACT SHA and produces `PASS`/`FAIL`/`INCONCLUSIVE`/`BLOCKED` with evidence. Reuses
   `app.workforce.verification`'s PROVEN collusion-detection pattern (verifier identity !=
   builder identity), generalized to cross-provider identities.
5. **`ExternalProviderLease`** — new, same SHAPE as `SupervisorScope`/`TaskScopedAuthority`
   (task/workspace/branch/tools/time/budget/network/disclosure/authority scope) but for
   agents OUTSIDE this process. Never carries raw GitHub credentials or Vault access.
   References `app.egress_policy`/provider-disclosure conceptually in documentation only —
   does not import them.
6. **`ProtectedArtifact`** — new. `#245` / `818dfb732da47901eb5ae06ffdd9c829fe00c4c5` is the
   real, seeded first example, matching this whole session's actual practice.
7. **`CompletionEvidence`** — new, isolated (does NOT import the unmerged `#245` branch's
   `evidence_claim.py`): branch/base_sha/new_sha/changed_files/test_commands/test_results/
   open_blockers/P0/P1/production_wiring_state/merge_state, validated against real repo
   state where the Director actually has it available, never trusted from provider prose.
8. **Provider usage/failover states** — new (`AVAILABLE/RATE_LIMITED/USAGE_EXHAUSTED/
   OFFLINE/FAILED/QUARANTINED`), scoped to `ExternalProviderLease` holders specifically.
9. **Git/PR broker** — the LOCAL git-worktree pattern reuses `production_worktree.py`'s own
   technique (cited, not imported, since that module is real production code this round
   should not import into an isolated, unwired package — mirror the approach). A NEW
   `PullRequestProposal` type represents what a PR WOULD contain — no live `gh pr create`
   call anywhere in this round, matching the founder's explicit "no live remote push
   required... disabled by default."
10. **Founder Offline Mode + Founder Brief** — new. Founder Brief COMPOSES (references) the
    real, existing `final_report.py`-style per-goal reports across every goal in a Program's
    window rather than reinventing goal-level reporting.
11. **Kill-switch / pause** — built fresh in the isolated package, explicitly NOT assuming
    this branch's in-process-only kill-switch is durable, and NOT assuming the separate
    `#245` branch's DB-backed epoch exists here. Documented explicitly: once `#245` is
    certified and merged, real integration should adopt ITS mechanism, not this round's
    isolated stand-in.

## 2. What this reconciliation does NOT do

- Does not modify `app.development_supervisor`, `app.mainai_execution`, `app.workforce`,
  `app.execution_envelopes`, `app.provider_spend`, `app.provider_planning`,
  `app.mainai_startup_readiness`, `app.autonomous_gap`, or `app.capability_reality` — reads
  and composes with their real types only.
- Does not import anything from the frozen `#245` branch (`claude/final-blocker-closeout`).
- Does not implement live GitHub push/PR creation, live external-provider API invocation, or
  any production wiring — deterministic in-process adapters stand in for real providers this
  round, exactly as instructed.
- Does not attempt real cross-system budget enforcement between the Program-level ceiling and
  `provider_spend`'s/`workforce.cost`'s own systems — flagged as future integration work.
- Does not touch PR #245 / SHA `818dfb7`.
