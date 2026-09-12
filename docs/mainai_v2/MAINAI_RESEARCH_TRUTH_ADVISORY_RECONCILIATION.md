# MainAI Research, Truth & Advisory Intelligence — Reconciliation

Architecture decision, written before implementation, following this program's own established
discipline: audit first, decide, then build. Grounded in direct reading of `app.mainai_vision`
(this program's own immediately prior round), `app.work_intelligence`/`app.strategy_evaluation`/
`app.strategy_synthesis` (the "specialist"/"challenger" hits a codebase-wide grep surfaced),
`app.agent_coordination.WorkAssignmentRole`, and `app.resource_intelligence` — not assumed.

## 0. What already exists — confirmed by direct reading, not assumed

- **`app.mainai_vision.evidence`** (this program's own prior round, same session): `EvidenceState`
  (OBSERVED/DERIVED/INFERRED/PLAUSIBLE/SPECULATIVE/CONTRADICTED/VERIFIED — a claim's evidential
  *directness*), `RawEvidence`/`EvidenceClaim`, `count_independent_sources()` (REPEATED SOURCE !=
  INDEPENDENT EVIDENCE), `classify_claim_state()`, `evaluate_confounds()`/`ConfoundCheck`
  (denominator/baseline/sample/window disclosure). **This program extends these, never
  duplicates them** — imported directly, not re-implemented. The NEW vocabulary this round
  introduces (`EvidenceLifecycleStatus`: REJECTED_AS_SUPPORT/PROVEN_FALSE/INSUFFICIENT_EVIDENCE/
  CONTRADICTED/SUPERSEDED/DEPRIORITIZED/UNRESOLVED/STALE/VALID_SUPPORT/STRONG_SUPPORT/
  VERIFIED_WHERE_POSSIBLE) is a DIFFERENT axis — an evidence item's *lifecycle/standing within one
  investigation* — orthogonal to, and composed with, `EvidenceState`'s own *directness* axis.
- **`app.work_intelligence`/`app.strategy_evaluation`/`app.strategy_synthesis`**: a real
  "challenger"/"specialist contribution" vocabulary — confirmed by direct reading to be a
  DIFFERENT domain entirely: A/B experimentation between two WORK STRATEGIES (`WorkStrategy`,
  `WorkStrategyExecution`, `challenger_binding_id` = the competing strategy execution being
  compared against a baseline), not a multi-perspective epistemic review council over one body
  of evidence. No collision in practice, but this program's own `SpecialistRole` enum
  deliberately uses different, non-overlapping string values to avoid future confusion.
- **`app.agent_coordination.WorkAssignmentRole`**: `builder/reviewer/tester/challenger/
  researcher/synthesizer` — real, already-existing AGENT DISPATCH roles (who does actual staffed
  work). This program's own `SpecialistRole` (analyst/economist/legal_counsel/
  adversarial_counsel/evidence_analyst/mainai) is a DIFFERENT concern — a REASONING PERSPECTIVE
  taken over one body of evidence, not a staffing assignment. Deliberately not merged into one
  enum: a real agent could eventually be DISPATCHED (via `WorkAssignmentRole.researcher`) to
  PRODUCE the material a `SpecialistRole.evidence_analyst` perspective then reasons over — two
  different layers, matching this codebase's own "runtime state != reasoning state" convention.
- **`app.resource_intelligence`** (this program's own prior-prior round): `cost_bridge.
  cost_per_accepted_commit()`, `quota.provider_quota_remaining()`, `efficiency_profile.
  agent_efficiency_profile()`, `founder_attention.py`. Provider Economics (§17-18) is BUILT ON
  these real signals, never a second cost/quota system.
- **Confirmed, exhaustively**: no existing module models an investigation graph (actors/
  relationships/money/timeline), a multi-specialist review council, recursive falsification, a
  durable research ledger, source-dependence lineage, or book-grade provenance tracing. This is
  genuinely new work, not a reuse-and-extend job for those specific pieces — but every
  SURROUNDING piece (evidence quality axis, cost/quota signals, agent role vocabulary) already
  exists and must be composed with.

## 1. The decision

**New package `app.mainai_research`** — a genuinely new domain (investigation/evidence-lifecycle/
advisory economics), matching this program's own precedent of giving a new domain its own
package. One additive migration (new tables — a durable, book-grade-auditable research ledger
genuinely needs real relational structure; a JSON blob in `FounderMemoryNote` would not support
the querying this domain requires, unlike the lighter-weight checkpoint/mind-change use cases
`session_checkpoint.py`/`mind_change.py` reuse that mechanism for).

1. **`types.py`** — `EvidenceLifecycleStatus`, `SpecialistRole`, `CausalTest`, `ProviderRecommendation`,
   `KnowledgeItemState` (the founder's own named MENTION/IDEA/ASSUMPTION/CLAIM/DECISION/PLAN/
   IMPLEMENTATION/VERIFIED_RESULT/REJECTED/SUPERSEDED/UNKNOWN ladder — a new, small enum;
   deliberately similar in spirit to, but not imported from, Codex's Personal Recall
   `DecisionState`, which lives on a separate, unmerged branch not present here), plus
   `Actor`/`Relationship`/`MoneyFlow`/`TimelineEvent` dataclasses. Re-exports `mainai_vision.
   evidence.EvidenceState`/`RawEvidence`/`count_independent_sources()` verbatim.
2. **Migration `0073_mainai_research_ledger`** — `mainai_research_investigations`,
   `mainai_research_hypotheses`, `mainai_research_evidence_links`, `mainai_research_confidence_
   history` (append-only trigger, mirroring `intelligence_governance`'s own deny-mutation
   precedent), `mainai_research_reopen_events` (append-only). All owner-scoped, RLS FORCE.
3. **`research_ledger.py`** — durable CRUD/query over the above; `update_confidence()` REQUIRES a
   reason (CONFIDENCE CHANGE MUST HAVE A REASON) and always appends history, never overwrites.
4. **`epistemic_caution.py`** — pure: sparse-evidence/narrow-source/high-source-dependence raises
   the confidence bar (LOW EXPERIENCE / LOW EVIDENCE → HIGHER THRESHOLD), never fabricates doubt
   against overwhelming independent evidence.
5. **`investigation_graph.py`** — durable Actor/Relationship/MoneyFlow/Timeline graph, composed
   with `research_ledger.py`. RELATIONSHIP != CONTROL, BENEFIT != PROOF OF INTENT structurally
   (no function anywhere infers intent/guilt from association alone).
6. **`falsification.py`** — pure recursive falsification round tracker: survive → confidence up
   (bounded), fail to find counterevidence → NOT proof, contradiction → confidence down
   regardless of prior support volume.
7. **`causal_reasoning.py`** — pure causal-test checklist (correlation/common_cause/reverse_
   causation/selection/confounding/mechanism/temporal_order/counterfactual).
8. **`words_vs_actions.py`** — pure statement-vs-action longitudinal comparator; LINGUISTIC
   PATTERN != PROOF OF INTENT (signal for deeper investigation only).
9. **`council.py`** — `SpecialistRole` + `CouncilReview`/`synthesize_council_review()` — SPECIALIST
   AGREEMENT != TRUTH, disagreement stays visible until explicitly resolved; `adversarial_
   critique()` structured self-attack checklist.
10. **`statistics_integrity.py`** — extends `mainai_vision.evidence`'s confound work with
    explicit relative-vs-absolute/source-collapse claim checks.
11. **`provider_economics.py`** + **`procurement_review.py`** — `ProviderRecommendation` engine
    over real `resource_intelligence` signals (never a second cost ledger), requiring history
    depth (ONE BAD DAY != DECISION), cross-specialist review composing `council.py`.
12. **`knowledge_ingestion.py`** — `KnowledgeItem` (WHAT/WHO/WHEN/CONTEXT/SOURCE/ORIGINAL_VS_
    DERIVED/state/currentness/confidence/inference_vs_explicit); "I think X may be useful" != a
    permanent founder decision, enforced structurally (an `IDEA`/`ASSUMPTION`-state item has no
    code path to `DECISION` without an explicit, caller-supplied transition).
13. **`book_provenance.py`** — claim→conclusion→evidence→source lineage trace + the founder-
    facing Research Council Output synthesis (§22).
14. **`adapters.py`** — real composition with `mainai_vision`/`resource_intelligence`; typed
    `Protocol` seams for the sibling programs not present on this branch (same disclosure
    convention as `mainai_vision.adapters`).

## 2. What this reconciliation does NOT do

- Does not modify `app.mainai_vision`, `app.resource_intelligence`, `app.work_intelligence`,
  `app.strategy_evaluation`, `app.agent_coordination` — reads/imports/composes only.
- Does not create a second evidence-quality axis, cost ledger, or agent-role vocabulary.
- Does not grant this layer purchase/spend/execution/legal/ownership authority — every
  recommendation-producing function returns `authorized=False`; provider changes require
  explicit, separate founder authorization exactly like every other advisory layer in this
  program.
- Does not modify the frozen Personal Recall, Continuous Supervision, dev_director, #245, or
  Founder Reasoning candidates.
- Does not merge, deploy, or enable unrestricted real providers.
