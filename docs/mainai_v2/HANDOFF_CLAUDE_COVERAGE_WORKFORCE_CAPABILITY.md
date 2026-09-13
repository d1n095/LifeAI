# Handoff — MainAI Coverage & Omission Intelligence / Dynamic Workforce Orchestration / Capability Learning Candidate

Durable checkpoint, written so a FRESH session (after context reset) has the correct CURRENT
canonical state without depending on any prior session's own conversational summary.
SESSION RESET != PROGRAM RESET.

**Written:** 2026-09-13.

## CURRENT OBJECTIVE

Give MainAI the ability to audit its own coverage (what was discussed but never built, built
but never verified, verified but never integrated), reason about workforce assignment (wait
vs assign, continue vs hand off, reservation for upcoming need), and run a real capability
learning loop that gradually reduces external-provider dependence with evidence, never a
hunch. Built as a NEW branch/worktree from the frozen Cognitive Efficiency / Information
Architecture candidate.

## EXACT SHA

- **Base frozen candidate:** `7015ce4d66c3df291b72ac23d15180fad1ed2f44` on
  `claude/mainai-v2-sovereign` (worktree `claude-mainai-v2`) -- NOT modified by this round.
- **This program's branch:** `claude/mainai-v2-coverage-workforce-capability`
- **This program's worktree:** `/Users/dennistorildson/Documents/LifeAI-worktrees/claude-mainai-v2-coverage-workforce`
- See `docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md` for the full
  architecture decision and reuse audit.

## WHAT IS IMPLEMENTED

**One additive migration** (`0075_mainai_workforce_mastery`): `mainai_workforce_capability_mastery`
(owner-scoped RLS, live row) + `mainai_workforce_mastery_events` (append-only via the reused
migration-0038 trigger). No existing table touched.

**New package `backend/app/mainai_coverage/`** (6 modules, pure/composing, no migration):
maturity-ladder reuse (verbatim, from `mainai_vision`), traceability-chain gap detection,
historical-omission discovery (caller-supplied claims vs real canonical vision), production-
claim guard, and real composition with the existing `gap_generator` staging pipeline for
dynamic-denominator expansion.

**New package `backend/app/mainai_workforce/`** (12 modules): durable Capability Mastery
Ledger, promotion/demotion policy (asymmetric evidence bars), teacher value, external-
dependence recommendation, real agent-runtime-view composition, reservation, wait-vs-assign
(the founder's own two worked examples reproduced verbatim as tests), continue-vs-handoff,
capability learning loop (observe -> extract procedure -> record), founder-output message
builders composing the existing anti-repetition filter, and a workforce-scheduler composition
layer whose output is always `authorized=False`.

## WHAT IS NOT IMPLEMENTED / KNOWN LIMITATIONS

- No corpus/conversation-history/commit-log scanner exists to automatically produce
  `CapabilityClaim` rows -- a caller supplies them (disclosed, matching every other advisory
  module's "caller supplies the real signal" convention this whole session has used).
- `omission_discovery.score`-style keyword overlap is a documented Jaccard heuristic, not
  semantic/NLP matching.
- `wait_or_assign`/`continuation_policy` take competency/cost/context signals as caller-
  supplied floats rather than deriving them automatically from `mastery_ledger`/
  `capability_reality`/`resource_intelligence.efficiency_profile` in one composed call --
  kept modular and independently testable instead of building one large, harder-to-verify
  composition this round.
- `app.resource_intelligence.scheduler.next_best_resource_allocation()` (in-flight triage) and
  this round's `workforce_scheduler.recommend_for_task()` (not-yet-assigned wait/assign) are
  deliberately not merged into one call.

## TEST EVIDENCE

**48/48 new tests passing**, real local Postgres 16, migrations clean to head
`0075_mainai_workforce_mastery`. Covers, mapped to the founder's own §17 test scenarios:
best-agent-busy-briefly waits (founder's own worked example, verbatim), best-agent-busy-long
assigns a safe candidate (founder's own second worked example, verbatim), duplication/branch-
conflict risk defers regardless of availability, high-context-loaded agent beats a nominally
stronger cold agent, cheap-with-high-rework loses to expensive-reliable, capability promotion
requires diverse verified successes (single success never promotes), demotion after a real
regression, external teacher retained when it catches unique bugs, external dependence reduced
only after sustained local parity, reservation with real evidence is kept and never
indefinite, a forgotten-but-discussed requirement is rediscovered and recommended for
denominator expansion, a REJECTED requirement is never auto-resurrected,
implemented/reviewed/activated-but-not-yet-verified/integrated/production-proven omissions are
each detected, and a PRODUCTION_PROVEN claim without runtime evidence is rejected. Also: real
composition against an actual `CoordinationAgent` row (not a stub) proving
`WorkforceRecommendation.authorized` is always `False`.

Broader regression across `mainai_vision`/`mainai_research`/`mainai_cognitive_ops`/
`mainai_coverage`/`mainai_workforce`/`project_entities`/`resource_intelligence`/
`work_candidates`/`capability_reality`/`agent_coordination`: run this session (see final
report for exact count). `ruff check`: clean. `python -m compileall`: clean.

## KNOWN P0

None found.

## KNOWN P1

None found beyond the disclosed scope limits above.

## NEXT REVIEW REQUIRED

Independent review by a party that did not build it (BUILDER != FINAL EXAMINER).

## DO-NOT-REPEAT

- Do not rebuild `app.mainai_vision.types.MaturityState`/`completion.py`'s dynamic-denominator
  math -- already implemented; this round only reuses it and adds the missing "stage a
  rediscovered omission" composition.
- Do not rebuild `app.capability_reality` -- it answers a different question ("can this be
  invoked at all") from this round's own Mastery Ledger ("what autonomy stage is this
  task-class at").
- Do not merge `app.resource_intelligence.scheduler.next_best_resource_allocation()` with
  `app.mainai_workforce.workforce_scheduler.recommend_for_task()` into one function -- they
  answer genuinely different questions; compose both at the call site instead.
