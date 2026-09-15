# Handoff — MainAI Coverage & Omission Intelligence / Dynamic Workforce Orchestration / Capability Learning Candidate

Durable checkpoint, written so a FRESH session (after context reset) has the correct CURRENT
canonical state without depending on any prior session's own conversational summary.
SESSION RESET != PROGRAM RESET.

**Written:** 2026-09-15 (follow-on pass closing the two P1s disclosed in the first pass).

## CURRENT OBJECTIVE

Give MainAI the ability to audit its own coverage (what was discussed but never built, built
but never verified, verified but never integrated), reason about workforce assignment (wait
vs assign, continue vs hand off, reservation for upcoming need), and run a real capability
learning loop that gradually reduces external-provider dependence with evidence, never a
hunch. Built as a NEW branch/worktree from the frozen Cognitive Efficiency / Information
Architecture candidate. The founder's own review of the first pass correctly flagged two
disclosed P1s as canonical-goal gaps, not cosmetic ones -- this pass closes both.

## EXACT SHA

- **Base frozen candidate:** `7015ce4d66c3df291b72ac23d15180fad1ed2f44` on
  `claude/mainai-v2-sovereign` (worktree `claude-mainai-v2`) -- NOT modified by this program.
- **This program's branch:** `claude/mainai-v2-coverage-workforce-capability`
- **This program's worktree:** `/Users/dennistorildson/Documents/LifeAI-worktrees/claude-mainai-v2-coverage-workforce`
- **First-pass commit:** `4a57d891eb39e97ba5123ac5790053c9ef6f8819`
- **This follow-on pass's commit:** see the final report delivered alongside this handoff for
  the exact SHA (this file is committed together with the code it describes).
- See `docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md` for the full
  architecture decision, reuse audit, and the follow-on pass's own §A-§E writeup.

## WHAT IS IMPLEMENTED

**One additive migration** (`0075_mainai_workforce_mastery`, unchanged from the first pass):
`mainai_workforce_capability_mastery` (owner-scoped RLS, live row) +
`mainai_workforce_mastery_events` (append-only via the reused migration-0038 trigger).

**`backend/app/mainai_coverage/`** (11 modules, no migration): maturity-ladder reuse
(verbatim, from `mainai_vision`), traceability-chain gap detection, real composition with the
existing `gap_generator` staging pipeline for dynamic-denominator expansion, PLUS (new this
pass) `source_adapters.py` (real read-only ingestion of handoff docs, reconciliation docs, the
branch registry, and the git commit log; explicit `SourceAvailability(available=False, ...)`
for conversation history, which this codebase genuinely cannot reach),
`requirement_extraction.py` (extraction + provenance-preserving dedup),
`discovery_pipeline.run_discovery()` (the full composed A pipeline), and `matching.py` (the
layered EXACT_IDENTITY -> NORMALIZED_LEXICAL -> ALIAS -> STRUCTURED_LINK -> GRAPH_RELATIONSHIP
-> KEYWORD_OVERLAP -> SEMANTIC_SIMILARITY comparison strategy, replacing the first pass's
bare-Jaccard-only omission check while remaining backward compatible with it).

**`backend/app/mainai_workforce/`** (14 modules): durable Capability Mastery Ledger,
promotion/demotion policy, teacher value, external-dependence recommendation, real
agent-runtime-view composition, reservation, wait-vs-assign, continue-vs-handoff, capability
learning loop, founder-output message builders, a workforce-scheduler composition layer
(`authorized=False` always), PLUS (new this pass) `types.SignalOrigin`/`SignalEnvelope` and
`signal_derivation.py` (real derivation of competency/rework/quota/context-loaded-relevance
signals from the Capability Mastery Ledger -> `capability_reality` -> `resource_intelligence`,
in that priority order, each tagged OBSERVED/DERIVED/ESTIMATED/CALLER_SUPPLIED/UNKNOWN) and
`real_state_decisions.py` (the new NORMAL PATH: derives real signals, resolves overrides only
where no stronger real evidence exists, then calls the UNCHANGED pure `decide_wait_or_assign()`/
`decide_continue_or_handoff()` -- real composition, not a parallel reimplementation).

**New durable doc**: `docs/mainai_v2/KNOWN_ISSUE_TEST_SUITE_COMBINED_RUN_FLAKINESS.md` --
evidence-classified writeup of a non-reproducing (1-in-4) large-combined-run test error,
confirmed unrelated to this or any prior MainAI V2 round's code by direct `pg_stat_activity`
measurement and two independent clean isolation sub-runs.

## WHAT IS NOT IMPLEMENTED / KNOWN LIMITATIONS

- Conversation/chat-history ingestion remains explicitly unavailable -- no durable store of it
  exists anywhere in this codebase (confirmed, not assumed).
- `matching.py`'s SEMANTIC_SIMILARITY layer is permanently unavailable on this branch by
  design -- no local/bounded embedding model exists, and none was added to fake it.
- `real_state_decisions.py` does not weight mastery-ledger evidence RECENCY yet
  (`last_verified_at` exists on the schema but nothing currently populates it) -- "stale
  competency history" is handled today only via the sparse-history fallback, not real
  time-decay.
- ETA/handoff/interruption-cost signals have no real derivation source anywhere in this
  codebase -- override-only, honestly UNKNOWN otherwise.
- `app.resource_intelligence.scheduler.next_best_resource_allocation()` and this round's
  `workforce_scheduler.recommend_for_task()` remain deliberately un-merged (different
  questions; compose at the call site).
- The test-suite-scale flakiness (see the new KNOWN_ISSUE doc) remains open, by design, out of
  scope for this candidate.

## TEST EVIDENCE

**First pass: 48/48.** **This follow-on pass adds 41 new tests** (17 discovery/matching, 14
real-signal-derivation/composition, plus incidental coverage), **all passing** — **89/89 total
`mainai_coverage`+`mainai_workforce` tests green**, real local Postgres 16, migrations clean
to head `0075_mainai_workforce_mastery`. New coverage includes: real file reads of actual
handoff docs (asserting real section content, not a mock), real `git log` ingestion with real
40-character SHAs, the layered-match strategy's each layer individually (exact identity,
normalized lexical, alias, structured link, keyword-overlap-with-explicit-disclosure,
semantic-unavailable-honestly-reported), two REAL bugs caught and fixed by these tests before
shipping (a short-numeric-token keyword-matching precision bug, and a
"SIMILAR-WORDING-would-have-silently-merged-two-distinct-claims" dedup-bar bug), real
Postgres-backed competency derivation across all four priority levels (mastery ledger ->
capability_reality -> efficiency profile -> honest UNKNOWN), the override-vs-real-evidence
resolution rule in both directions, and real cross-component composed decisions (busy-but-
imminent best agent waits using REAL derived competency; long-ETA best agent yields to a safe
candidate; no-sufficiently-supported-best-choice falls back to a documented, clearly-tagged
neutral estimate for BOTH sides rather than silently favoring one; an agent that already owns
the candidate task's real program context is derived as high-relevance and correctly favors
continuation).

Broader regression across `mainai_vision`/`mainai_research`/`mainai_cognitive_ops`/
`mainai_coverage`/`mainai_workforce`/`project_entities`/`resource_intelligence`/
`work_candidates`/`capability_reality`/`agent_coordination`/`agent_dispatch`/`multi_agent`:
run this session (see final report for exact count). `ruff check`: clean.
`python -m compileall`: clean.

## KNOWN P0

None found.

## KNOWN P1

None found beyond the disclosed scope limits above -- both P1s the founder flagged after the
first pass are now closed with real, tested code.

## NEXT REVIEW REQUIRED

Independent review by a party that did not build it (BUILDER != FINAL EXAMINER). Per explicit
instruction: this candidate must NOT be reviewed by the same session that built it, and the
resulting exact SHA is intended for Codex or another independent examiner.

## DO-NOT-REPEAT

- Do not rebuild `app.mainai_vision.types.MaturityState`/`completion.py`'s dynamic-denominator
  math -- already implemented.
- Do not rebuild `app.capability_reality` -- different question from this round's own Mastery
  Ledger.
- Do not merge `app.resource_intelligence.scheduler.next_best_resource_allocation()` with
  `app.mainai_workforce.workforce_scheduler.recommend_for_task()` into one function.
- Do not let `deduplicate_claims()` (or any future merge-style function) use a bare
  KEYWORD_OVERLAP match as sufficient grounds to merge two record identities -- that is
  exactly the "SIMILAR WORDING == SAME REQUIREMENT" bug this pass found and fixed; use
  `layered_match(..., include_keyword_overlap=False)` for any MERGE decision, reserving the
  full layered match (including keyword overlap) for softer "is this plausibly the same
  thing" checks like `omission_discovery`'s own canonical-vision-coverage check.
- Do not add a semantic-similarity layer by quietly wiring an external AI/API call behind
  `matching.semantic_similarity_available()` -- that function's contract is "a real local/
  bounded model or nothing"; an external dependency there would violate the founder's own
  explicit instruction not to introduce one to satisfy this requirement.
- Do not speculatively fix the test-suite-scale flakiness in
  `KNOWN_ISSUE_TEST_SUITE_COMBINED_RUN_FLAKINESS.md` without first capturing a failing run's
  actual traceback -- the connection-exhaustion hypothesis was tested directly and ruled out;
  guessing again without new evidence would repeat the same mistake.
