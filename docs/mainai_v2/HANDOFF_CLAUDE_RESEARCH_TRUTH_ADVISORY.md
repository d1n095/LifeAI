# Handoff — MainAI Research, Truth & Advisory Intelligence Candidate

Durable checkpoint, written specifically so a FRESH session (after context reset) has the
correct CURRENT canonical state without depending on any prior session's own conversational
summary. `SESSION RESET != PROGRAM RESET`: everything below is meant to survive that reset.

**Written:** 2026-09-12.

## CURRENT OBJECTIVE

Build the MainAI Research, Truth & Advisory Intelligence layer — deep investigation, early-stage
epistemic caution, multi-specialist review, recursive falsification, source-of-truth lineage,
evidence reopening, words-vs-actions analysis, actor/money/relationship mapping, statistics
integrity, provider economics/procurement advisory, adversarial internal counsel, research
ledger, book-grade provenance, continuous knowledge auditing — as one coherent program, per the
founder's own explicit directive. **Complete as of this handoff.** Does not modify, rewrite, or
destabilize the completed Cognitive Control Plane candidate (`c4d336d`).

## EXACT SHA

- **Branch:** `claude/mainai-v2-sovereign`
- **Worktree:** `/Users/dennistorildson/Documents/LifeAI-worktrees/claude-mainai-v2`
- **Base this program branched from:** `c4d336d` (MainAI Cognitive Control Plane tip)
- See `docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md` for the full architecture
  decision and reuse audit.

## WHAT IS IMPLEMENTED

**One additive migration** (`0073_mainai_research_ledger`): five new tables
(`mainai_research_investigations`, `_hypotheses`, `_evidence_links`, `_confidence_history`,
`_reopen_events`) — owner-scoped, RLS FORCE. Confidence-history and reopen-events are
append-only, reusing the EXISTING `intelligence_governance_deny_mutation()` trigger function
(migration 0038) verbatim.

**New package `backend/app/mainai_research/`** (13 modules):
- `types.py` — re-exports `mainai_vision.evidence.EvidenceState`/`RawEvidence`/
  `count_independent_sources` verbatim; new `EvidenceLifecycleStatus` (11 values, a DIFFERENT,
  orthogonal axis from `EvidenceState`), `SpecialistRole`, `CausalTest`, `ProviderRecommendation`,
  `KnowledgeItemState` (the founder's own named ladder), `Actor`/`Relationship`/`MoneyFlow`/
  `TimelineEvent`.
- `research_ledger.py` — durable CRUD over the new tables. `update_hypothesis_confidence()`
  requires a non-empty reason and always appends history. `reject_evidence()` requires the
  caller to choose the exact rejection status (never defaults to PROVEN_FALSE).
  `reopen_evidence()` records a reopen event and preserves the original rationale.
- `epistemic_caution.py` — sparse-evidence/thin-history/high-source-dependence raises the
  confidence bar; genuinely overwhelming independent evidence (>= 95%) is never diluted.
- `falsification.py` — recursive round tracker: survival raises confidence (capped at 0.9 from
  survival alone), a real contradiction caps confidence low regardless of prior support,
  failure to find counterevidence is never treated as proof.
- `causal_reasoning.py` — a causal verdict requires every one of 6 required alternative tests to
  be explicitly checked and ruled out; an untested alternative blocks the causal verdict.
- `words_vs_actions.py` — statement/action mismatch pattern detection; output has no
  intent/guilt field.
- `council.py` — `SpecialistRole` + disagreement-preserving synthesis + structured adversarial
  self-critique.
- `statistics_integrity.py` — relative-risk-without-baseline flagging, source-collapse
  detection (reuses `count_independent_sources()`), metric-vs-system-quality conflation check.
- `provider_economics.py` + `procurement_review.py` — `ProviderRecommendation` engine requiring
  minimum observation history (never REMOVE/UPGRADE on thin history); cross-specialist
  procurement review where an ADVERSARIAL_COUNSEL dissent stays visible, never silently
  overridden; user-facing wording review.
- `knowledge_ingestion.py` — `KnowledgeItemState` transition table with NO direct edge from
  MENTION/IDEA to DECISION (must pass through CLAIM first).
- `investigation_graph.py` — pure Actor/Relationship/MoneyFlow/Timeline graph builder; no
  control/intent/conspiracy field anywhere; flags dangling references and unevidenced ties.
- `book_provenance.py` — claim lineage tracer (composes `research_ledger.py`, never
  re-derives) + founder-facing Research Council Output synthesis.
- `adapters.py` — real composition with `mainai_vision.completion`/
  `resource_intelligence.cost_bridge`; typed `Protocol` seams for the sibling programs not
  present on this branch.

## WHAT IS NOT IMPLEMENTED / KNOWN LIMITATIONS

- No live wiring into any founder-facing UI/API route — same isolated-foundation status as
  every sibling program.
- `investigation_graph.py` is deliberately NOT backed by its own dedicated Actor/Relationship
  table — it is a pure builder over caller-supplied, already-evidenced records; a caller wanting
  this durable stores it as `provenance` on the relevant `mainai_research_evidence_links` row.
  This is an honest, documented design choice (no new schema for a concern the existing
  evidence-link provenance JSONB already accommodates), not a silent gap.
- `provider_economics.py`/`procurement_review.py` take real `resource_intelligence` SIGNALS as
  caller-supplied input (`ProviderEconomicsSignal`) rather than deriving them internally — this
  package composes with `resource_intelligence.cost_bridge`/`quota`/`efficiency_profile` via
  `adapters.py`, but the wiring from those real functions into a `ProviderEconomicsSignal` is
  the caller's own responsibility (matching every other advisory module in this program).
- Development Director, Continuous Supervision, Personal Recall, V1 readiness adapters remain
  typed `Protocol` only (same disclosure as `mainai_vision.adapters` — two are simply not
  present on this branch, one has no durable owner-queryable store yet).

## TEST EVIDENCE

**67/67 `mainai_research`-scoped tests passing**, real local Postgres 16, migrations clean to
head `0073_mainai_research_ledger`:
- Ledger: idempotency, CONFIDENCE CHANGE MUST HAVE A REASON, real append-only trigger
  (`sqlalchemy.exc.InternalError` on a direct UPDATE attempt), REJECTED_AS_SUPPORT !=
  PROVEN_FALSE, reopen preserves original rationale, SATURATED_FOR_NOW != PERMANENTLY CLOSED,
  owner isolation, falsification-round increment.
- Epistemic caution: sparse evidence raises the bar; 97% independent high-quality evidence is
  NEVER diluted into fake 50/50 uncertainty despite thin history.
- Falsification: survival raises confidence (bounded ceiling proven over 19 rounds), a real
  contradiction caps confidence low regardless of 0.95 prior support, failure-to-find-
  counterevidence is never proof.
- Causal reasoning: bare correlation is `insufficient_testing`; ONE unruled-out test blocks a
  causal verdict even when the other 5 pass.
- Words vs actions: repeated mismatch recommends investigation only, never exposes an
  intent/guilt field.
- Council: disagreement stays visible (never averaged away); adversarial dissent on a
  procurement recommendation blocks it from silently standing.
- Statistics integrity: 10 articles from 1 source collapse to 1 independent source; relative-
  risk claims without a disclosed baseline are flagged.
- Provider economics: thin history never recommends removal; a rare provider with unique
  capability is kept as fallback; expensive-but-effective provider wins on total verified-
  outcome cost.
- Knowledge ingestion: IDEA cannot directly become DECISION (must pass through CLAIM);
  terminal states cannot transition further.
- Investigation graph: dangling references rejected; unevidenced relationships surfaced, not
  hidden; no control/intent field anywhere.
- Book provenance: claim lineage correctly collapses repeated-source citations; council output
  always carries `still_unknown`/`what_would_change_our_mind` fields even when empty.
- Package-wide structural purity sweep (AST-based): zero forbidden mutating calls; write
  boundary confirmed to exactly `research_ledger.py`; every `authorized` field defaults `False`.

Scoped regression across `mainai_vision`/`project_entities`/`resource_intelligence`/
`work_candidates`/`capability_reality`: run this session (see final report for exact count).
`ruff check`: clean. `python -m compileall`: clean. `git diff --check`: clean.

## KNOWN P0

None found in this package's own new code.

## KNOWN P1

None found. Design notes documented above (investigation_graph has no dedicated table;
provider_economics signals are caller-supplied) are intentional scope decisions, not gaps.

## NEXT REVIEW REQUIRED

Independent review by a party that did not build it, per the standing role-separation plan
(BUILDER != FINAL EXAMINER).

## DO-NOT-REPEAT

- Do not rebuild the evidence-quality axis (`EvidenceState`) — already exists in
  `app.mainai_vision.evidence`, reused verbatim here.
- Do not confuse this package's `SpecialistRole` with `app.agent_coordination.
  WorkAssignmentRole` or `app.strategy_evaluation`'s "challenger" — three different domains,
  deliberately non-overlapping vocabularies (see reconciliation doc §0).
- Do not add a second cost/quota ledger for provider economics — compose with
  `app.resource_intelligence` via `adapters.py`.
