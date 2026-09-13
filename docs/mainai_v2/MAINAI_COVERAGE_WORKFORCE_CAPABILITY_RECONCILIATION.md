# MainAI Coverage & Omission Intelligence + Dynamic Workforce Orchestration + Capability Learning / External-Independence -- Architecture Decision

Program delivered as one coherent candidate on a NEW branch/worktree
(`claude/mainai-v2-coverage-workforce-capability`), branched from the frozen Cognitive
Efficiency / Information Architecture candidate (`7015ce4`, worktree
`claude-mainai-v2`) without modifying it. Does not touch Codex's Level-2 integration lane.

## §0 -- Audit before building (the reuse decisions that shaped this program)

Before writing new code, the existing repo was checked for overlap -- this is the single most
consequential design decision in this round:

1. **The founder's own §1 maturity ladder and §4 dynamic completion denominator are ALREADY
   BUILT**, in `app.mainai_vision.types.MaturityState`/`MATURITY_ORDER`/`MATURITY_INDEX` and
   `app.mainai_vision.completion.compute_completion()`/`assess_program_completion()` (from the
   Cognitive Control Plane round). The ordinal ladder there (`DISCOVERED` -> ... ->
   `MAINTAINED`, 13 rungs) is the same concept as the founder's own MENTIONED -> ... ->
   PRODUCTION_PROVEN/MAINTAINED list (`DISCOVERED` is that ladder's name for "MENTIONED";
   `ROBUSTNESS_TESTED` is its name for "ROBUSTNESS_REVIEWED" -- same rung, pre-existing name).
   `app.mainai_coverage.types` re-exports this ladder VERBATIM rather than building a second,
   competing one. The four terminal/exceptional states the founder's own list adds that the
   ordinal ladder does NOT carry (SUPERSEDED/REJECTED/BLOCKED/UNACCOUNTED_FOR) become a new,
   orthogonal `CoverageDisposition` axis -- mirrors `app.mainai_research.types.
   EvidenceLifecycleStatus` being a separate axis from `EvidenceState`.
2. **`app.resource_intelligence.scheduler.next_best_resource_allocation()` already answers**
   "who, among already in-flight assignments, needs attention next" (session-lifecycle
   triage: checkpoint/reset/handoff/compact/change-model). It does NOT decide "should we wait
   for a busy best agent or assign a safe candidate to a NOT-yet-assigned task" -- that gap is
   real and is what `app.mainai_workforce.wait_or_assign`/`continuation_policy` fill. The two
   are complementary, never merged (see `app.mainai_workforce.adapters`'s own disclosure).
3. **`app.agent_coordination.runtime_view.all_agents_runtime_snapshot()`/`AgentRuntimeView`**
   is the real, live "who is busy/idle, on what branch/SHA" truth. `app.mainai_workforce.
   situational_snapshot.py` converts this REAL data into `app.mainai_cognitive_ops.types.
   AgentState` -- closing the "no live adapter" P1 the Cognitive Ops round's own handoff
   disclosed, never a second registry.
4. **`app.capability_reality`** already answers "can Life invoke this capability at all right
   now" (a binary-ish availability/verification state per `capability_key`). The founder's
   §10 Capability Mastery Ledger is a DIFFERENT axis -- how far along the external-dependency
   -> local-default AUTONOMY STAGE a task-class is, relative to a specific external teacher --
   composed via a shared `capability_key`, never duplicated into that package's own tables.
5. **`app.mainai_vision.gap_generator.propose_implied_requirements()`/`persist_gap_proposals()`**
   is the existing, unchanged staging pipeline for expanding the canonical vision.
   `app.mainai_coverage.dynamic_denominator.py` composes it directly rather than
   reimplementing "stage a new requirement."

## Package layout

**`backend/app/mainai_coverage/`** (6 modules, no migration -- pure/composing only):
- `types.py` -- re-exports `MaturityState` etc. verbatim; new `CoverageDisposition`,
  `TraceabilityStage`, `CapabilityClaim` (caller-supplied; this package has no corpus/
  conversation-history scanner of its own -- honestly disclosed, matching every other
  advisory module's "caller supplies the real signal" convention in this whole session).
- `traceability.py` -- VISION -> ... -> RUNTIME_EVIDENCE ordinal chain; detects the first real
  gap and produces a deterministic narrative finding (never an LLM judgment call).
- `omission_discovery.py` -- keyword-overlap (Jaccard, documented heuristic) comparison of
  caller-supplied claims against the real canonical vision; NOT_IN_ROADMAP !=
  INTENTIONALLY_REJECTED enforced structurally (a `REJECTED`/`SUPERSEDED` disposition is never
  reconsidered for denominator expansion).
- `production_claim_check.py` -- ACTIVATED != PRODUCTION_PROVEN guard.
- `dynamic_denominator.py` -- composes the real `gap_generator` staging pipeline (staging
  only, never promotion).
- `adapters.py` -- real composition with `vision_compiler.compile_vision_graph()`; discloses
  the one seam that is not real (no corpus scanner).

**`backend/app/mainai_workforce/`** (12 modules + migration 0075):
- `types.py` -- `AutonomyStage` (0-6, IntEnum), `WaitOrAssignDecision` (8 values, matching the
  founder's own §5 list exactly), `ProviderDependenceRecommendation`, `TeacherObservation`
  (observable-only fields -- no hidden chain-of-thought, per explicit instruction).
- `mastery_ledger.py` -- durable (migration 0075), STAGE CHANGE MUST HAVE A REASON (mirrors
  `mainai_research.research_ledger`'s CONFIDENCE CHANGE MUST HAVE A REASON exactly);
  `promote()`/`demote()` require `new_stage` to move strictly in the requested direction.
- `promotion_policy.py` -- ONE LOCAL SUCCESS != MASTERY: promotion requires minimum
  observation count AND task diversity AND examiner pass rate AND recency.
- `demotion_policy.py` -- deliberately asymmetric: no minimum-evidence floor (a real
  regression is responsive, matching `mainai_research.falsification`'s "a real contradiction
  is never outvoted by prior confidence" asymmetry).
- `teacher_value.py` -- LOW USAGE != LOW STRATEGIC VALUE, a distinct axis from `$`-based
  `mainai_research.provider_economics` (deliberately not merged -- the two can point in
  opposite directions at once).
- `external_dependence.py` -- ONE EXPENSIVE DAY != REMOVE PROVIDER / ONE GOOD RESULT !=
  UPGRADE; composes `promotion_policy`+`teacher_value` rather than a third evidence bar.
- `situational_snapshot.py` -- REAL composition with `agent_coordination.runtime_view` (see
  §0.3 above).
- `reservation.py` -- AGENT_RESERVED != AGENT_WASTED, with a hard cap against indefinite
  reservation.
- `wait_or_assign.py` -- the founder's own two worked examples reproduced as tests verbatim
  (Codex 8min/ideal -> WAIT; Codex 3h/Claude-safe -> ASSIGN).
- `continuation_policy.py` -- CONTEXT ALREADY LOADED HAS VALUE / HANDOFF HAS COST /
  cheap-with-rework loses to expensive-reliable.
- `capability_learning_loop.py` -- OBSERVE METHOD -> EXTRACT REUSABLE PROCEDURE -> (durable)
  observation, composing `mastery_ledger.py`; never stores hidden chain-of-thought, only the
  named observable-process fields.
- `founder_output.py` -- builds the founder's own §18 example message strings, then asks the
  EXISTING `mainai_cognitive_ops.founder_anti_repetition` filter whether to send them (never
  reimplements suppression logic).
- `workforce_scheduler.py` -- composition layer; `WorkforceRecommendation.authorized` always
  `False` (SCHEDULER OUTPUT != EXECUTION AUTHORIZATION), structurally verified.
- `adapters.py` -- discloses the one deliberately-NOT-composed seam (see §0.2 above).

## Migration 0075

Adds `mainai_workforce_capability_mastery` (owner-scoped RLS, live mutable row) and
`mainai_workforce_mastery_events` (append-only via the reused migration-0038 trigger). No
existing table altered.

## Real bugs found and fixed during this round

None new this round -- the two real bugs from the prior (Cognitive Ops) round were already
fixed and are not repeated here. This round's own tests (48 total) passed on first full run
after the initial draft, aside from routine ruff cleanup (one unused import).

## What remains (honest P1s)

- `app.mainai_coverage.omission_discovery`'s keyword-overlap scoring is a documented heuristic
  (Jaccard over significant words), not semantic/NLP matching -- same disclosed limitation as
  `mainai_research.research_reopen_trigger.score_relevance()`.
- No corpus/conversation-history/commit-log scanner exists anywhere in this codebase that
  could automatically produce `CapabilityClaim` rows -- a caller supplies them today.
- `app.mainai_workforce.wait_or_assign`/`continuation_policy` take competency/cost/context
  signals as caller-supplied floats -- deriving these automatically from
  `mastery_ledger`/`capability_reality`/`resource_intelligence.efficiency_profile` in one
  composed call is a natural next step, not built this round (kept modular and testable
  instead, matching this whole session's incremental-composition discipline).
- `app.resource_intelligence.scheduler.next_best_resource_allocation()` and
  `app.mainai_workforce.workforce_scheduler.recommend_for_task()` are deliberately NOT merged
  into one call (see §0.2) -- a caller wanting a single combined founder view composes both
  at the call site.
