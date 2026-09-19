# Handoff — MainAI Cognitive Control Plane Candidate

Durable checkpoint, written specifically so a FRESH session (after context reset) has the
correct CURRENT canonical state without depending on any prior session's own conversational
summary. `SESSION RESET != PROGRAM RESET`: everything below is meant to survive that reset.

**Written:** 2026-09-12.

## CURRENT OBJECTIVE

Phase A (independent verification of Codex's Universal Personal Recall candidate) and Phase B
(build the MainAI Cognitive Control Plane: Vision Compiler + Dynamic Completion Engine + Vision
Gap Generator + Cognitive Loop + Statistics Command Center + Evidence Intelligence + Continuous
Improvement + Founder Program Truth), per the founder's own explicit program directive. **Both
phases are complete as of this handoff.**

## PHASE A RESULT

Personal Recall candidate `024835547850035667c3d77383fd75699ceab178` (Codex, `codex/universal-
personal-recall` lineage): independently re-confirmed FOUNDATION VERIFIED / SAFE TO KEEP FROZEN
(no change from the prior review this same session). Two activation blockers confirmed, not
waived:
- **P0-1**: no reviewed production AEAD/key hierarchy — `DeterministicTestSnapshotProtector` is
  structurally forbidden from production use (`RecallIndexWorker.__init__` raises unless
  `test_only=True`, regardless of what protector object is passed).
- **P0-2**: trusted production grant/session dependencies and disclosure controls are not
  activated — the HTTP router (`routes_prep.py`) is built but never registered in `app.main`.

Classification: **P0, INTENTIONALLY_DEFERRED_WITH_SAFE_BOUNDARY** for both — real, open
blockers, but each is bounded by a structural refusal (not merely a missing feature with no
guard), so the candidate is safe to keep frozen rather than actively dangerous. Not CLOSED (the
work itself has not been done); not merely P1 (both block real production use). The candidate
was NOT modified.

## PHASE B — EXACT SHA

- **Branch:** `claude/mainai-v2-sovereign`
- **Worktree:** `/Users/dennistorildson/Documents/LifeAI-worktrees/claude-mainai-v2`
- **Base this program branched from:** `11049bb` (MainAI Resource Intelligence Round 2 tip)
- See `docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md` for the full architecture
  decision and reuse audit.

## WHAT IS IMPLEMENTED

**One additive migration** (`0072_mainai_vision_vocabulary`): widens `project_entities.
entity_type`, `project_entity_relationships.relationship_type`, and `interpretation_proposals.
proposed_entity_type` CHECK constraints with new vision vocabulary — no new table, no RLS
change, no change to any existing row. Same pattern as migration 0065/0069.

**New package `backend/app/mainai_vision/`** (11 modules):
- `types.py` — `MaturityState` (13-rung ladder), `VisionNodeKind`/`VisionEdgeKind` (the widened
  `project_entities` vocabulary), `VisionNode`/`VisionEdge`/`VisionGraph`. Re-exports
  `resource_intelligence.types.MetricEnvelope` verbatim — no competing metric shape.
- `vision_compiler.py` — `compile_vision_graph()`: read-only compile of current `project_entities`
  rows (+ relationships) into a typed `VisionGraph`. Excludes historical/superseded/disputed
  exactly like `list_current_project_entities()` already does.
- `completion.py` — `MaturityState` ladder-climb (`compute_node_maturity()`, cannot skip a rung),
  `derive_baseline_maturity()` (the one real signal always available: linked `WorkCandidate`
  status), weighted graph-wide `compute_completion()`/`assess_program_completion()` across 11
  dimensions with an EXPANDABLE denominator — proven by test to drop below 100% when a new vision
  node appears.
- `gap_generator.py` — `propose_implied_requirements()`: a documented capability-implication
  table (the founder's own "autonomous development" example verified verbatim) + composes
  `missing_piece.detect_missing_pieces()`. `persist_gap_proposals()` reaches ONLY
  `record_interpretation_proposal()` (staging) — never `promote_interpretation_proposal()`.
- `cognitive_loop.py` — `run_cognitive_cycle()`: thin wrapper around the REAL, unchanged
  `mainai_executive.loop.run_executive_cycle()`, adding vision-graph recompilation + completion
  recomputation + gap proposal as explicit extra steps.
- `evidence.py` — genuinely new: `EvidenceState` (7 values), `EvidenceClaim`/`RawEvidence`,
  `count_independent_sources()`/`classify_claim_state()` (REPEATED SOURCE != INDEPENDENT
  EVIDENCE, POPULAR CONSENSUS != PROOF, MINORITY CLAIM != SUPPRESSED TRUTH — all three proven by
  test), `evaluate_confounds()` (denominator/baseline/sample disclosure reasoning).
- `statistics.py` — one registry composing real `MetricEnvelope`s (never a second metric shape)
  plus this package's own `ObservationBasis`/`exclusions` additive wrapper.
- `improvement.py` — `BUILD_LOOP`/`IMPROVEMENT_LOOP` separation; proven by test that 100%
  completion routes to IMPROVEMENT_LOOP, never stops proposing work.
- `mind_change.py` — "What Changes My Mind" ledger, durable via the SAME `FounderMemoryNote` +
  `supersedes_note_id` mechanism `session_checkpoint.py`/`continuity.py` already use. No new
  table.
- `founder_truth.py` — `founder_program_truth()`: composes the REAL, existing
  `dashboard.founder_executive_dashboard()` with real completion/gap data into one payload.
- `adapters.py` — two REAL, composed adapters (Founder Reasoning via
  `executive_status_snapshot()`; Resource Intelligence via
  `next_best_resource_allocation()`), four typed `Protocol` seams (Development Director,
  Continuous Supervision, Personal Recall, V1 readiness) honestly disclosed as not-yet-wireable.

## WHAT IS NOT IMPLEMENTED / KNOWN LIMITATIONS

- No live wiring into any real founder-facing UI/API route — read-path functions only, same
  "isolated, non-wired foundation" status as every other MainAI V2 sub-program pending review.
- Development Director's own module IS present in this checkout but has no durable Postgres
  store this package can query by `owner_id` alone (`generate_founder_brief()` takes
  caller-supplied `Program`/`Job` dataclasses) — its adapter here is a typed `Protocol` only.
- Continuous Supervision and Personal Recall adapters are typed `Protocol` only — neither
  module is present on this branch (separate, unmerged Codex lanes).
- V1 readiness/evidence system was not audited in this reconciliation pass.
- The capability-implication table in `gap_generator.py` covers 4 capability areas
  (autonomous development, personal recall, continuous supervision, resource intelligence) —
  a documented, hand-picked starting point, not a claim of completeness.
- `derive_baseline_maturity()`'s only real, always-available signal is a linked `WorkCandidate`
  status; everything from IMPLEMENTED onward requires caller-supplied evidence (no code in this
  package invents test/review/production evidence for an arbitrary vision node — there is no
  existing FK from `ProjectEntity` to e.g. `CapabilityRecord`/test-run rows to derive it from
  automatically). This is an honest limitation, not a silent gap.

## TEST EVIDENCE

**60/60 `mainai_vision`-scoped tests passing**, independently run against real local Postgres 16
(migrations clean to head `0072_mainai_vision_vocabulary`):
- Vision compiler: real RLS/owner-isolation, current-vs-excluded filtering, empty-history.
- Completion: maturity-ladder-cannot-skip, weighted (not task-count) completion, THE central
  denominator-expansion invariant (100% → new node → completion drops below 100%), the
  implemented≠reviewed≠integrated≠activated chain.
- Gap generator: the founder's own "autonomous development" example verified verbatim,
  deduplication, staging-only persistence (idempotent), AST-verified never reaches
  `promote_interpretation_proposal()`.
- Evidence: REPEATED SOURCE != INDEPENDENT EVIDENCE (10 citations of one source = 1), POPULAR
  CONSENSUS != PROOF (20 citations still INFERRED, never DERIVED), MINORITY CLAIM != SUPPRESSED
  TRUTH (one real contradiction beats 20 supporting items), confound/denominator disclosure.
- Cognitive loop: runs the REAL `run_executive_cycle()` end-to-end (workforce dry-run, school
  routing, durable checkpoint all real), adds real vision/completion/gap steps on top.
- Improvement: 100% routes to IMPROVEMENT_LOOP and keeps proposing (never silently stops).
- Mind-change: real save/load/supersede round trip through `FounderMemoryNote`, owner isolation.
- Statistics/founder-truth/adapters: real `MetricEnvelope` reuse, real dashboard composition,
  real adapter composition.
- Package-wide structural purity sweep (AST-based): zero forbidden mutating calls anywhere in
  the package; write boundary confirmed to exactly the three documented exceptions
  (`gap_generator.persist_gap_proposals`, `mind_change.py`, `cognitive_loop.py`'s own composed
  call into `run_executive_cycle()`); every `authorized` field defaults to `False` everywhere.

Scoped regression across `project_entities`/`mainai_executive`/`resource_intelligence`/
`work_candidates`/`capability_reality`: run this same session (see final report for exact
count). `ruff check`: clean. `python -m compileall`: clean. `git diff --check`: clean.

## KNOWN P0

None found in this package's own new code.

## KNOWN P1

None found. One DESIGN note: `derive_baseline_maturity()`'s honest limitation above (no
automatic evidence derivation past WorkCandidate status) — documented, not a silent gap.

## NEXT REVIEW REQUIRED

Independent review of this candidate by a party that did not build it, per the standing
role-separation plan (BUILDER != FINAL EXAMINER) already established for every other MainAI V2
sub-program this session.

## DO-NOT-REPEAT

- Do not re-run this Phase A/Phase B pass again — both are complete and frozen at the SHA in the
  final report.
- Do not rebuild the eight subsystems from scratch in a future session — the reconciliation
  audit found most of their underlying primitives ALREADY exist in `app.mainai_executive`; read
  `MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md`'s §0 table first.
- Do not widen `project_entities`' CHECK constraints again for the same vocabulary — migration
  0072 already did it.
