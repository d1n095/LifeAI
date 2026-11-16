# MainAI Resource Intelligence + Context Lifecycle + Cost/Quota + Agent Efficiency — Reconciliation

Architecture decision, written before implementation, following this program's own established
discipline: audit first, decide, then build. Grounded in direct reading of `app.agent_coordination`
(full: `runtime_view.py`, `service.py`, `execution_control.py`, models) by me personally, plus a
dedicated audit pass over `app.provider_spend`, `app.workforce.cost`, `app.capability_reality`,
`app.execution_envelopes`, `app.mainai_executive.{continuity,multi_session,soak,observability}`,
and an exhaustive codebase-wide grep for any existing token/context/cost tracking.

## 0. What already exists — confirmed by direct reading, not assumed

### The real "who is doing what, where, right now" layer: `app.agent_coordination`

Full, live, already-built: `CoordinationAgent` (registry: `agent_key`/`adapter_kind`/
`model_hint`/`capabilities`/`concurrency_limit`/`cost_class: str` — a coarse unmeasured label,
not real telemetry), `AgentWorkAssignment` (WHO/WHAT/WHERE/AUTHORITY, `WorkAssignmentStatus`
state machine), `AgentScopeLease` (real `lease_generation` fencing, `expires_at`/
`last_heartbeat_at`), `AgentWorkAssignmentEvent` (append-only, trigger-enforced), and —
critically — `AgentDispatchExecution` (migration 0047, one row per dispatch **attempt**:
`attempt_id`, `adapter_state` [`starting/running/exited/lost/timeout/cancelled`],
`result_ingestion_status`, `last_heartbeat_at`, `last_output_at`, `started_at`, `ended_at`).
`execution_control.py`'s `record_execution_event()`/`reconcile_execution_state()` already
provide real, provider-neutral process-liveness tracking (PROCESS_ALIVE / PROCESS_EXITED /
ADAPTER_DISCONNECTED / SESSION_LOST / RESULT_PENDING_INGESTION), classification-only, never
mutating assignment status itself. **This is the correct, existing identity anchor for any new
per-session resource telemetry — `AgentDispatchExecution.attempt_id` is already the real
"one agent session" primitive.** A new resource-intelligence layer must attach to it, never
invent a parallel session concept.

**Confirmed codebase convention, stated explicitly in `docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md`'s
own "explicitly deferred" list**: a stored agent-level heartbeat column was deliberately NOT
built — `AgentRuntimeView.heartbeat_at` is derived from the lease's own `last_heartbeat_at` at
read time, never a second stored source of truth. **Derive, never duplicate** is this codebase's
own standing rule, not a suggestion — the new resource layer follows it: telemetry is read from
raw durable samples/events and existing ledgers at read time wherever possible, not cached into
redundant counters that could drift.

### The real dollar/token spend ledger: `app.provider_spend`

Real, live, per-call granularity: `ProviderSpendUsageEvent` (`prompt_tokens`/`completion_tokens`/
`cost_usd` settled actuals, `reserved_*` two-phase hold, tied to `goal_id`/`task_id`/`job_id`)
under `ProviderSpendAuthorization` ceilings (`max_cost_usd`/`max_requests`/`max_prompt_tokens`/
`max_completion_tokens`, `allowed_providers`/`allowed_models`, `status: active/superseded/
exhausted/expired/revoked`). `app.providers.pricing.estimate_cost()` maps provider/model/token
counts to real dollar estimates for a small hardcoded model table. `app/providers/
anthropic_provider.py` is the one confirmed real usage-extraction site (`usage.input_tokens`/
`usage.output_tokens` → `prompt_tokens`/`completion_tokens`).

**Confirmed gap**: this ledger ties to `goal_id`/`task_id`/`job_id` — **never to `agent_id` or
`AgentDispatchExecution.attempt_id`**. No join path exists today. The new layer is the first
thing that needs to bridge them (via the assignment row, which already carries both `goal_id`/
`task_id` AND `agent_id` — the bridge is a read-only join, not a schema change to `provider_spend`
itself, which stays untouched).

### The real dollar-only budget-ceiling concept: `app.workforce.cost` (built, never wired)

`WorkforceCostBudget.scope_kind ∈ {assignment, agent, team, goal, period, provider}` — `agent`/
`team` already exist as first-class scope-kind STRINGS, confirmed by direct reading to be
**completely unpopulated in production** (`provider_spend/service.py`'s own docstring: "no
WorkforceCostBudget row is ever created in production yet"). Dollars only — no token field at
all. This is real, reusable SCOPE VOCABULARY (`reserve_against_budget()`/
`settle_budget_reservation()`/`assert_scopes_allow_spend()`), not yet actual telemetry.

### The real "current state != permanent trait" precedent: `app.capability_reality`

Live-mutable-row (`CapabilityRecord`) + parallel append-only observation-event log
(`CapabilityObservationEvent`, DB-trigger-enforced insert-only) — status is always overwritten
by the caller's latest observation, `last_success_at`/`last_failure_at` are separate timestamps
that never themselves flip status, full history lives only in the event log. **This is the
exact shape for "ONE_RUN != LONG_TERM_PROFILE," confirmed reusable** — but its `confidence:
Numeric(5,4)|None` is a bare, caller-supplied opinion field; nothing computes or decays it.
**No statistical N-observation/weighted/decaying-confidence math exists anywhere in this
codebase.** That is genuinely new work, not a gap-fill.

### The real PROPOSED != AUTHORIZED template: `app.execution_envelopes`

`propose_execution_scope()` structurally cannot write the authority table even indirectly;
`authorize_execution_scope()` is the SOLE writer, always requires every authority field
explicitly from the caller (never copies from the proposal); supersede-never-mutate history;
`(owner_id, idempotency_key)` + `ON CONFLICT DO NOTHING` + re-fetch-on-race; a durable
"has this ever been governed" predicate distinct from "is there a current one." **This is the
exact, already-proven shape `RESOURCE_OPTIMIZATION != AUTHORITY` reuses** — with one
simplification: unlike execution scope (which really does need a durable, mutable
"authorized" grant a caller later acts under), a resource-lifecycle recommendation has no
real downstream authority object to create at all — the actual COMPACT/RESET/HANDOFF action is
performed by a human or the harness itself, outside this system. So this round builds the
`propose_*`/pure-recommendation half only (mirroring `app.mainai_executive.judgment.
decide_judgment()`'s own proven pure-function shape from the Founder Reasoning round:
`authorized` always `False`, zero I/O, zero mutation) — no `authorize_*` counterpart is needed
because there is no authority table for this domain to create.

### The one already-declared, currently-unused resource-cost vocabulary slot

`app.agent_coordination.service.build_agent_outcome_payload()` already declares `cost_tokens`,
`cost_usd`, `duration_seconds` as optional fields — confirmed by grep to have **zero real
callers populating them today**; they land in unconstrained `IntelligenceEvidence.payload` JSON
with no reader anywhere. This round should **become the first real, correct populator and
reader of these already-declared fields**, not invent parallel ones.

### The real checkpoint precedent — and why it does not directly cover this round's need

`app.mainai_executive.continuity.{save,load}_continuity_checkpoint()` — durable, via
`FounderMemoryNote` + `supersedes_note_id` chain, **no new table**. `ContinuityCheckpoint`'s
fields (`session_id`/`phase`/`founder_request`/`completed`/`uncertain`/`remaining`/
`authority_still_valid`/`interruption_point`/...) are scoped to **MainAI's own internal
executive-loop phases** (UNDERSTAND→...→LEARN) — not to an external agent CLI session (Claude
Code/Cursor/Codex). Zero token/cost/context-size fields. The mechanism (durable via
founder_memory notes, supersession chain, no new table) is exactly right and must be reused
verbatim; the SHAPE needs a sibling scoped to agent sessions, not a fork of `ContinuityCheckpoint`
itself.

### Confirmed, exhaustive: no context-window/context-budget tracking exists anywhere

Codebase-wide grep for `context_window`, `context_used`, `cost_estimate`: **zero hits anywhere
in `app/`**. The only real per-call token accounting anywhere is `provider_spend`'s settled
ledger. `app.mainai_executive.soak.SoakReport` is the only place any wall-clock timing exists in
the executive layer, and it is whole-run aggregate elapsed seconds, never per-session/per-cycle.
`executive_status_snapshot()` (observability.py) surfaces goal/phase/candidates/delegation/
authority/kill-switch — zero resource metrics. This confirms the core of this round is
genuinely new, not a reuse-and-extend job like most prior V2 rounds — but every SURROUNDING
piece (identity anchor, spend ledger, budget-scope vocabulary, revisable-profile pattern,
propose/authorize doctrine, checkpoint mechanism) already exists and must be composed with, not
duplicated.

## 1. The decision

**New package `app.resource_intelligence`** — a genuinely new domain (resource/context/cost
economics), not an extension of `app.agent_coordination`/`app.mainai_executive` (whose own
concerns are "who does what" and "MainAI's own reasoning," respectively), matching this
program's own precedent of giving a genuinely new domain its own package (`app.dev_director`,
`app.attachment_chamber`) rather than overloading an existing one. Composes heavily with, never
duplicates:

1. **`types.py`** — `ContextLifecycleAction` enum (`CONTINUE_CURRENT_SESSION, COMPACT,
   CHECKPOINT, RESET_SESSION, HANDOFF, SPLIT_JOB, MOVE_SUBTASK, CHANGE_MODEL, CHANGE_PROVIDER,
   DEFER, KEEP_CURRENT_AGENT`); `ResourceActionRecommendation` dataclass (`action, reason,
   signals: dict, authorized: bool = False` — mirrors `JudgmentDecision`'s own proven shape);
   `MetricEnvelope` dataclass implementing the metrics-quality contract structurally
   (`value, unit, definition, denominator, time_window, population, sample_size, source,
   method, missing_data: bool, uncertainty: str | None, last_updated, trend: str | None`) —
   every metric-returning function in this package returns `MetricEnvelope`s, never a bare
   number, so METRIC != TRUTH is enforced by the type signature, not by convention alone.
   `UNKNOWN` stays `UNKNOWN`: every optional telemetry field is `None` when not observed,
   never defaulted to 0 or estimated silently.
2. **`telemetry.py`** — one new, small, additive table `agent_resource_telemetry_samples`
   (append-only: `id, owner_id, assignment_id FK, attempt_id, sampled_at, context_used_tokens
   | None, context_window_tokens | None, input_tokens | None, output_tokens | None,
   cached_tokens | None, tool_calls | None, provenance`), keyed to the REAL
   `AgentDispatchExecution.attempt_id` — never a parallel session identity. A caller (the CLI
   harness itself, or a future adapter) records what it actually knows; nothing here infers or
   fabricates a token count. Idle/productive/blocked-time classification is **derived**, not
   stored — computed at read time from real `AgentDispatchExecution.last_heartbeat_at`/
   `last_output_at`/`started_at`/`ended_at` plus these samples' own `sampled_at` deltas, exactly
   matching the "derive, never duplicate" convention `AgentRuntimeView.heartbeat_at` already
   established.
3. **`cost_bridge.py`** — read-only join from `AgentWorkAssignment` (`goal_id`/`task_id`) to
   real `ProviderSpendUsageEvent` rows, producing `MetricEnvelope`s for cost-per-assignment/
   cost-per-agent/cost-per-accepted-commit/cost-per-independent-PASS. Never creates a parallel
   ledger — `provider_spend` remains the sole source of real dollar/token truth. Also the first
   real populator+reader of `build_agent_outcome_payload()`'s existing `cost_tokens`/`cost_usd`/
   `duration_seconds` slots, wiring them from this same bridge rather than leaving them as dead
   vocabulary.
4. **`session_checkpoint.py`** — `AgentSessionCheckpoint` dataclass (the founder's own named
   fields: `current_objective, current_status, exact_sha, current_branch, current_worktree,
   open_p0, open_p1, what_was_tried, what_failed, what_passed, important_findings,
   current_test_evidence, next_action, do_not_repeat, authority_boundaries,
   unresolved_questions, critical_session_only_facts`), persisted through the **exact same
   real mechanism** `continuity.save_continuity_checkpoint()`/`load_continuity_checkpoint()`
   already use (`FounderMemoryNote` + `supersedes_note_id` chain) — a sibling function
   following the identical pattern, scoped to `agent_id`/`attempt_id` instead of MainAI's own
   `session_id`/`phase`. No new table.
5. **`efficiency_profile.py`** — the genuinely new statistical layer `capability_reality`
   doesn't have: a real N-observation, recency-weighted running profile per
   `(agent_id, task_type)`, computed from durable telemetry samples + `cost_bridge` +
   `AgentWorkAssignment` outcomes (accepted-commit rate, examiner-fail rate, rework rate).
   Mirrors `capability_reality`'s live-row + append-only-event-log split structurally
   (current computed profile is always revisable, full observation history is what it's
   computed FROM, never hand-edited) but adds the real math that module never had. Confidence
   grows with sample size and decays with recency — one anomalous run cannot flip a profile
   (`ONE_RUN != LONG_TERM_PROFILE` enforced by requiring a minimum sample count before a
   profile leaves `provisional` status).
6. **`decision.py`** — `propose_resource_action()`: pure, deterministic (matching
   `judgment.decide_judgment()`'s own proven shape — no LLM-first default, explicit signal
   citation, `authorized` always `False`), taking real signals (telemetry, efficiency profile,
   cost-to-finish estimate, WIP from `app.agent_coordination`) and returning a
   `ResourceActionRecommendation`. Never itself resets, compacts, or hands off anything —
   recommends only, matching `RESOURCE_OPTIMIZATION != AUTHORITY` exactly the way
   `execution_envelopes` keeps `propose_*` structurally incapable of writing authority.
7. **`scheduler.py`** — `next_best_resource_allocation()`: composes `propose_resource_action()`
   per agent/session with `app.agent_coordination`'s real WIP/availability view
   (`all_agents_runtime_snapshot()`) to answer "who should get the next job" — read-only,
   advisory, never calls `create_work_assignment()` itself.

**Confidence/status vocabulary**: reuses the existing `Numeric(5,4)` scale and
`active/historical/superseded/disputed`-shaped status where a status field is needed at all,
per this program's own standing convention — no new vocabulary invented where an existing one
fits.

## 2. What this reconciliation does NOT do

- Does not modify `app.agent_coordination`, `app.provider_spend`, `app.workforce.cost`,
  `app.capability_reality`, `app.execution_envelopes`, or `app.mainai_executive`'s own service
  files — reads and composes only, one additive table.
- Does not create a second spend ledger, a second agent registry, a second heartbeat/liveness
  system, or a second checkpoint table — every one of those already exists and is reused as-is.
- Does not grant this layer authority to actually compact/reset/hand off/change model or
  provider for any real session — advisory only, per `RESOURCE_OPTIMIZATION != AUTHORITY`.
- Does not modify the frozen `dev_director` candidate (SHA `ab1c0ce03a7a0f7b11f2f716304f9e1235a54d3e`),
  the verified runtime (SHA `1951ccef16f6e165092cf85e0bd545505b69f8b1`), `#245`
  (`818dfb732da47901eb5ae06ffdd9c829fe00c4c5`), or Personal Recall
  (`024835547850035667c3d77383fd75699ceab178`).
- Does not touch the Codex continuous-supervision branch.
- Does not merge, deploy, or enable real providers.
