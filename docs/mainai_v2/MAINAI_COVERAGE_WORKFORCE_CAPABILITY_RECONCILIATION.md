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

None new in the FIRST pass of this round -- the two real bugs from the prior (Cognitive Ops)
round were already fixed and are not repeated here. That first pass's own 48 tests passed on
first full run, aside from routine ruff cleanup (one unused import).

## Follow-on pass: closing the two disclosed P1s (§A/§B/§C/§D/§E below)

The founder's own review of the first pass correctly identified that two disclosed P1s were
not cosmetic -- they mapped directly to the program's own canonical goals. This section
documents the follow-on work that closed them, on the SAME branch/candidate.

### §A -- Automatic Coverage/Omission Discovery

New modules: `source_adapters.py` (real, read-only ingestion -- `ingest_markdown_doc()` reads
real handoff/reconciliation docs via `#`/`##`/`###`-section splitting; `ingest_branch_registry()`
reads real markdown-table rows from `docs/BRANCH_REGISTRY.md`; `ingest_git_commit_log()` runs
real, fixed-argv, read-only `git log`; `ingest_test_evidence()` does a real, bounded filesystem
scan for test files matching a keyword; `conversation_history_availability()` always reports
`SourceAvailability(available=False, ...)` -- confirmed by direct inspection that no durable
conversation/chat-history store exists anywhere in this codebase, UNKNOWN != ABSENT, never
fabricated), `requirement_extraction.py` (`extract_capability_claims()` +
`deduplicate_claims()`, provenance-preserving), and `discovery_pipeline.run_discovery()` (the
full composed pipeline, returning `sources_ingested`/`sources_unavailable`/`sources_failed`
explicitly rather than silently dropping a source that was expected but missing).

### §B -- Beyond keyword-only omission matching

New module `matching.py`: a genuinely LAYERED strategy (`layered_match()`) --
EXACT_IDENTITY -> NORMALIZED_LEXICAL -> ALIAS -> STRUCTURED_LINK -> GRAPH_RELATIONSHIP ->
KEYWORD_OVERLAP -> SEMANTIC_SIMILARITY, tried in that order, and every `MatchResult` discloses
WHICH layer decided it (`result.layer`) so a KEYWORD_OVERLAP hit is never presented as
semantic understanding (`result.detail` says so explicitly). `semantic_similarity_available()`
always returns `False` on this branch -- no local/bounded semantic-embedding implementation
exists, and this module does not introduce an external AI/API dependency to fake one.

A REAL bug was found and fixed while building this: `requirement_extraction.deduplicate_claims()`
originally used the full layered match (including KEYWORD_OVERLAP) to decide whether to MERGE
two claims -- which meant two claims sharing only common context words ("revenue grew 10% in
Q1" vs "...40% in Q3") could collapse into one record purely because short, digit-bearing
tokens ("10", "40", "Q1", "Q3") were being dropped by the keyword tokenizer's length filter.
Fixed two ways: (1) `_keywords()` now keeps any token containing a digit regardless of length,
and (2) `deduplicate_claims()` calls `layered_match(..., include_keyword_overlap=False)` --
MERGING two claim identities is held to a stricter bar than the "is this already in canonical
vision" omission check, because KEYWORD_OVERLAP alone is exactly the "SIMILAR WORDING == SAME
REQUIREMENT" failure mode the founder's own instruction named. Both bugs were caught by this
round's own tests before being shipped.

### §C -- Derive workforce signals from real system state

New modules `types.SignalOrigin`/`SignalEnvelope` (OBSERVED/DERIVED/ESTIMATED/CALLER_SUPPLIED/
UNKNOWN) and `signal_derivation.py`: `derive_competency_signal()` tries, in order, (1) this
package's own Capability Mastery Ledger's examiner-verified pass rate for the exact
(capability_key, task_class, external_teacher), DERIVED; (2) `app.capability_reality`'s own
asserted `confidence`, DERIVED; (3) `app.resource_intelligence.efficiency_profile`'s real
`accepted_commit_rate`, OBSERVED; (4) UNKNOWN -- never fabricated. `derive_rework_rate_signal()`,
`derive_quota_uncertainty_signal()` (MISSING DATA != ZERO, UNKNOWN COST != FREE -- a missing
quota authorization is UNKNOWN, never "unlimited"), and `derive_context_loaded_relevance_signal()`
(pure, reasons over a real `AgentState.current_program`) round out the signal set.
`resolve_signal()` implements "an override never silently replaces stronger current durable
evidence": a real OBSERVED/DERIVED value always wins; an override is used ONLY when the real
signal is UNKNOWN.

`real_state_decisions.py` is the new NORMAL PATH: `decide_wait_or_assign_from_real_state()`/
`decide_continue_or_handoff_from_real_state()` derive every signal from real state, resolve
overrides, and THEN call the existing, UNCHANGED, already-tested pure
`wait_or_assign.decide_wait_or_assign()`/`continuation_policy.decide_continue_or_handoff()` --
never a parallel reimplementation of the decision logic itself. When neither a real signal nor
an override exists, a documented, hand-picked neutral fallback (0.5 competency, 0.0 rework) is
used and ALWAYS tagged `ESTIMATED`, never silently presented as a real measurement.

### §D -- Real composition, tested

`test_mainai_workforce_real_signal_derivation.py` exercises real cross-component composition
end to end (a real Postgres-backed mastery ledger + capability_reality record feeding a real
decision), covering: best-agent-busy-but-almost-finished waits using REAL derived competency;
a long-ETA best agent yields to a weaker-but-available candidate; sparse mastery history falls
through to capability_reality rather than being read as competency=0; repeated examiner
failures derive low competency; recent strong performance derives high competency; no
sufficiently supported best choice falls back to the documented neutral estimate for BOTH
agents (never silently favoring either); missing quota is UNKNOWN, never zero/unlimited; an
agent that already owns the candidate task's program context is derived as high
context-loaded-relevance and correctly favors continuation even against a nominally-similar
candidate; high handoff+interruption cost keeps continuation. (`compare_total_verified_outcome_cost`
-- "expensive model with lower total cost" -- and context-exhaustion-with-checkpoint remain
`app.resource_intelligence`'s own domain, not duplicated here; see §0.2's own composition
boundary.)

### §E -- Test-scale flakiness, evidence-classified

See `docs/mainai_v2/KNOWN_ISSUE_TEST_SUITE_COMBINED_RUN_FLAKINESS.md` for the full
investigation. Summary: a single 322-error run on the full ~505-test combined selector has not
reproduced in 3 subsequent identical attempts (one of which directly instrumented
`pg_stat_activity` at 2-second resolution throughout -- peak 12 of 100 max_connections,
ruling out simple connection-pool exhaustion by direct measurement, not assumption). Two
independent, non-overlapping-enough sub-selections of the same query were both clean (452 and
274 passed, 0 errors each). Classified LOW-CONFIDENCE, NON-REPRODUCING,
ENVIRONMENTAL/TIMING-TRANSIENT -- confirmed unrelated to any code in this or the prior four
MainAI V2 rounds. Not fixed here; documented as a durable, evidence-backed open issue per the
founder's own explicit instruction not to contaminate this candidate with a speculative fix.

## What remains (honest P1s, after the follow-on pass)

- `app.mainai_coverage.discovery_pipeline`'s real adapters cover handoff docs, reconciliation
  docs, the branch registry, and the git commit log -- genuinely new durable source classes
  (e.g. a future MainAI memory-thread table) get their own adapter function when they exist;
  conversation/chat history remains explicitly `SourceAvailability(available=False, ...)`
  because no durable store of it exists anywhere in this codebase today.
- `matching.py`'s `SEMANTIC_SIMILARITY` layer is permanently unavailable on this branch by
  design (no local/bounded embedding model exists, and none was added to satisfy this
  requirement) -- a future caller wiring in a real local model has exactly one place to do it
  (`semantic_similarity_available()`), never a silent KEYWORD_OVERLAP substitution.
- `real_state_decisions.py` does not yet weight the RECENCY of mastery-ledger evidence
  (`last_verified_at` exists on the schema but no CRUD path in `mastery_ledger.py` currently
  populates it) -- "stale competency history" is handled today only via the sparse-history
  (`total_examined == 0`) fallback path, not a time-based decay; documented here rather than
  bolted on to already-tested, already-passing ledger-write code without a clear evidence
  need.
- ETA/handoff/interruption-cost signals have no real derivation source anywhere in this
  codebase (no timing-prediction or switch-cost model exists) -- they remain override-only,
  honestly UNKNOWN otherwise, never fabricated.
- `app.resource_intelligence.scheduler.next_best_resource_allocation()` and
  `app.mainai_workforce.workforce_scheduler.recommend_for_task()` are deliberately NOT merged
  into one call (see §0.2) -- a caller wanting a single combined founder view composes both
  at the call site.
- The test-suite-scale flakiness documented in §E remains open and unresolved (by design --
  out of scope for this candidate).
