# Cognitive Core Requirements Matrix — Round 10

**Lane:** Z.ai · **Branch:** `zai/alpha-runtime-reliability` · **Base:** `fd413518` (R9) → **this round:** M3+M7 `2d951026`, M4 `94cb8f6c`, M5 `baa1f8e6`, M6 `86032376`, M8 `89ebded3` (± final registry commit)
**Relationship to the R9 registry:** this matrix EXTENDS `docs/COGNITIVE_REQUIREMENTS_REGISTRY_R9.md` (which inventoried the ten engineering capability areas) into the established 16 Cognitive Core REQUIREMENTS. One capability per row, each with: implementation, production path, persistent contract, authorization boundary, tests, measured behavioral evidence, honest gaps, and startup scope. Code existing is not verification — every "evidence" cell cites an executed assertion or measured behavior.

**Legend:** ✅ startup-critical, verified · 🟡 startup-adjacent, verified · ⏸ post-startup scope, registered

---

## 1. Independent hypothesis generation ✅
- **Implementation:** `app/rag/scientific_reasoning.py` (composition + competing-explanations gap), evidence metadata `competing_explanations`, prompt layer carries hypotheses + design spread.
- **Production path:** library stamp → evidence-chain/landscape → chat/workbench prompt (`VETENSKAPLIG SAMMANSÄTTNING`, hypothesis material).
- **Contract:** `claim_evidence_metadata.metadata_json` (closed vocabulary); claims table.
- **Authorization:** founder-only stamping; model layer renders, never writes metadata.
- **Tests:** `test_scientific_reasoning.py` (gap counting), `test_cognitive_gate.py` CG-11 objective half.
- **Behavioral evidence:** `causal_without_alternatives == 1` counted from real stamps; design composition `experimental/observational/mechanistic` asserted from stored rows.
- **Gaps:** model-mode hypothesis QUALITY is rubric-scored, not auto-enforced (by design).
- **Startup scope:** ✅ core; rubric review is a founder workflow from day one.

## 2. Critical, source-based research ✅
- **Implementation:** retrieval (`app/rag/retrieve.py`), provenance (`claim→chunk→source`), evidence grading (11 axes), citation-integrity structure (sources derive from retrieval, never model output).
- **Production path:** chat/workbench retrieve → trust assessment → framed context; library chain endpoint.
- **Contract:** `documents`, `document_chunks`, `knowledge_claims`, `claim_evidence_metadata`.
- **Authorization:** owner-scoped retrieval (RLS) + egress control.
- **Tests:** retrieval suites, `test_adversarial_reasoning.py` (fabricated citation structurally impossible), gate CG-02.
- **Behavioral evidence:** fabricated model citation did NOT enter `response.sources` (asserted); funding conflict floors grade at `low` with the scrutiny-not-rejection caveat (asserted).
- **Gaps:** none known startup-critical.
- **Startup scope:** ✅.

## 3. Causal and consequence reasoning ✅
- **Implementation:** `causal_claim` + `causal_claim_supported_by_design` checks; adversary `weak_causal_inference`; second-order consequences (`app/rag/epistemics.py`, graph block); science policy.
- **Production path:** claims relationships → chain trace → chat graph block; evidence-chain endpoint.
- **Contract:** `claim_relationships`, `source_relationships`, metadata cap.
- **Authorization:** founder declares edges; consequences computed read-only.
- **Tests:** `test_intellectual_adversary.py` (weak-causal), `test_graph_prompt_integration.py` (consequence naming), gate CG-06.
- **Behavioral evidence:** dependent source named in the prompt when a contradiction lands (asserted); causal claim on observational design flagged HIGH severity (asserted).
- **Gaps:** consequence DEPTH is capped (2) — deeper chains are post-startup.
- **Startup scope:** ✅.

## 4. Persistent, self-correcting memory ✅
- **Implementation:** decisions with append-only supersession + `decision_superseded` audit (R10 M5), candidate-signal memory (R9 M6), lesson standing machinery (R8/R9).
- **Production path:** problem-learning service; relationships endpoint → signals; finalize writer → lessons.
- **Contract:** `life_problem_decisions`, `audit_log`, `candidate_learning_signals`, `engineering_lesson_*`.
- **Authorization:** founder-only decisions; model writes CANDIDATE signals only.
- **Tests:** `test_decision_memory_integrity.py` (7 tests incl. restart + stale evidence + authority markers), `test_cognitive_cycle_continuous.py`.
- **Behavioral evidence:** 3-deep supersede chain A→B→C preserves every rationale (asserted); stale evidence never rewrote the decision row (asserted).
- **Gaps:** auto-promotion policy for signals is deliberately founder-decision territory.
- **Startup scope:** ✅.

## 5. Controlled proactive improvement ✅
- **Implementation:** improvement proposals (R10 M7): propose with benefit/risk, duplicate suppression, founder-only decide/implement/revert, all audit-trailed.
- **Production path:** `POST /api/mainai/improvement-proposals` (founder-gated).
- **Contract:** `mainai_improvement_proposals` (migration 0076, RLS, status CHECK).
- **Authorization:** STRUCTURAL — there is no execution path; `execution_attempted=False` in every response.
- **Tests:** gate CG-13 (`test_cognitive_gate.py`).
- **Behavioral evidence:** proposal recorded `proposed` with `decided_at=None`; duplicate suppressed; nothing executed (asserted).
- **Gaps:** recurring-failure→proposal auto-generation is a post-startup producer (the queue + boundary are the startup deliverable).
- **Startup scope:** ✅ boundary; ⏸ producer automation.

## 6. Model-provider independence ✅
- **Implementation:** deterministic ladder/grading/ranking/policy (pure functions + SQL); provider registry; gate comparison across providers.
- **Production path:** every verdict path is model-free; the model renders Swedish instruction lines only.
- **Contract:** verdicts/grades stored in rows, not in model state.
- **Authorization:** provider config is founder-wide; switching requires explicit founder action.
- **Tests:** `test_full_cognitive_acceptance_r10.py` step 11 (stored verdicts unchanged across model calls), epistemics/evidence suites (pure-function determinism).
- **Behavioral evidence:** same claim, different provider instance → identical stored verdict (asserted).
- **Gaps:** none known.
- **Startup scope:** ✅.

## 7. Cognitive regression protection ✅
- **Implementation:** the Cognitive Regression Gate (R10 M3): versioned dataset, production-path execution, objective auto-enforcement, comparison + policy interface (`blocked_requires_approval`), durable run records (migration 0075).
- **Production path:** gate runner + `compare_gate_runs`; records queryable.
- **Contract:** `cognitive_gate_runs`, `cognitive_gate_case_results` (objective/rubric separated at storage).
- **Authorization:** the gate NEVER switches a model — enforcement belongs to the authorized operational system; provider changes stay founder-gated.
- **Tests:** 6 gate tests incl. the MUTATION proof (framing disabled → CG-01 fails).
- **Behavioral evidence:** a real capability regression is CAUGHT (mutation asserted); rubric-only drift never blocks (asserted).
- **Gaps:** scheduled/CI execution of the gate is an operational task (Freebuff lane), not a repo gap.
- **Startup scope:** ✅.

## 8. Independent intellectual adversarial review ✅
- **Implementation:** `app/rag/intellectual_adversary.py` (R10 M4): bounded triggers, evidence-ref objections, epistemic categories, rides the evidence-chain endpoint.
- **Production path:** `GET /claims/{id}/evidence-chain` → `adversarial_review`.
- **Contract:** read-only; objections cite `claim:/relationship:/metadata:/audit:` refs.
- **Authorization:** founder-only endpoint; review degrades fail-open.
- **Tests:** 8 tests (`test_intellectual_adversary.py`) incl. no-manufactured-objections and owner isolation.
- **Behavioral evidence:** clean supported claim → `reviewed=False` (asserted); contested claim → counterevidence objection naming the counter-claim (asserted).
- **Gaps:** LLM-quality objection REASONING is post-startup (the deterministic layer is the startup deliverable).
- **Startup scope:** ✅.

## 9. Epistemic integrity ✅
- **Implementation:** the ladder (`assumption→hypothesis→verified_fact`) + conflict override; untrusted-content framing (R9 M4); false-consensus and misleading-metadata guards; uncertainty naming.
- **Production path:** classify in every verdict path; framing in every chat/workbench prompt.
- **Contract:** claim confidence/status columns; epistemic caps materialized in metadata rows.
- **Authorization:** model outputs never write verdicts; only founder-declared edges + stamped metadata move them.
- **Tests:** adversarial suite (metadata cannot rescue no-basis; same-source support ≠ certainty), gate CG-01/03.
- **Behavioral evidence:** no-basis claim stays `assumption` regardless of RCT-grade metadata (asserted); injection payload delivered INSIDE data markers with policy OUTSIDE (asserted, frame-aware).
- **Gaps:** none known startup-critical.
- **Startup scope:** ✅.

## 10. Cognitive sovereignty ✅
- **Implementation:** FORCE RLS on every cognitive table; founder-only routers (decisions, proposals, edges, gate endpoints); owner-scoped retrieval; cross-owner invisibility at prompt layer.
- **Production path:** every endpoint is `require_founder` + owner-scoped queries.
- **Contract:** RLS policies per table (migrations 0042..0076).
- **Tests:** isolation assertions in EVERY new suite (standing, landscape, graph, retrieval, prompt, adversary, decisions).
- **Behavioral evidence:** another owner's documents/lessons/graph state invisible in prompt and service reads (asserted repeatedly).
- **Gaps:** none known.
- **Startup scope:** ✅.

## 11. Causal verification ✅
- **Implementation:** explicit evidence required for causal language: design-support check, science composition "kräv uttrycklig evidens för kausalitet", adversary weak-causal/unsupported-assumption objections, landscape disagreement handling.
- **Production path:** stamping → grading cap → verdict → prompt line.
- **Tests:** `test_scientific_reasoning.py` (confounding gap), `test_intellectual_adversary.py`, epistemics suites.
- **Behavioral evidence:** causal claim without alternatives counted and named in the prompt (asserted).
- **Gaps:** formal causal-effect estimation is out of scope (not an evidence-claims system).
- **Startup scope:** ✅.

## 12. Memory integrity and provenance ✅
- **Implementation:** M5 (append-only corrections, authority markers, audited transitions) + full provenance chain (claim→chunk→source→stamp) + shift audit.
- **Production path:** decision service + library endpoints; chain trace.
- **Tests:** `test_decision_memory_integrity.py`, hardening suite (rollback atomicity).
- **Behavioral evidence:** superseded decision text/basis/alternatives byte-identical after supersession (asserted); audit row carries both ids + both bases (asserted).
- **Gaps:** cross-session decision DIFF view (UI) is cosmetic post-startup.
- **Startup scope:** ✅.

## 13. Goal and decision continuity ✅
- **Implementation:** goals/plans/tasks with rationale fields; plan-event provenance (`lessons_applied`, selection audit); decision chains; signals persist across restart.
- **Production path:** planner + problem learning + finalize writer.
- **Tests:** `test_full_cognitive_acceptance_r10.py` step 8 (fresh connection), planner suites, loop-integrity suite.
- **Behavioral evidence:** fresh session reads the whole decision chain + audit + signals (asserted).
- **Gaps:** cross-conversation context threading is post-startup.
- **Startup scope:** ✅.

## 14. Cognitive resource governance ✅
- **Implementation:** bounded statement counts (measured in tests), LIMIT-capped queries everywhere, graph traversal depth caps, investigation cost bounds (`max_studies_to_examine`), batched test harness with reclaim gaps.
- **Production path:** every read path is bounded by construction.
- **Tests:** `test_cognitive_cycle_continuous.py` (perf bounds), `test_full_cognitive_acceptance_r10.py` (science ≤ 6 statements, traversal ≤ 10).
- **Behavioral evidence:** corpus noise adds zero statements (asserted); wall-clock sanity bound (asserted).
- **Gaps:** sustained-churn inode behavior of THIS container documented (R10 forensics) — an environment fact, not a code defect.
- **Startup scope:** ✅.

## 15. Uncertainty-driven investigation ✅
- **Implementation:** the bounded investigation policy (R10 M6): answer/investigate/stop/report_uncertainty with evidence-cited reasons and cost caps.
- **Production path:** rides the chat prompt for retrieved topics.
- **Tests:** `test_scientific_reasoning.py` (all four outcomes + bounds).
- **Behavioral evidence:** <2 studies → report_uncertainty; declared conflict → investigate with a 4-10 cap (asserted).
- **Gaps:** expected-information-gain is qualitative (risk-tiered), not numeric — post-startup refinement.
- **Startup scope:** ✅.

## 16. Controlled capability evolution ✅
- **Implementation:** the gate (behavioral regression protection) + improvement proposals (proposed→decided→implemented→reverted with audit) + lesson machinery measuring whether accepted changes work (effectiveness rollup + guard observations).
- **Production path:** gate compare + proposals queue + lesson observations.
- **Tests:** gate suite (policy semantics incl. revert-visibility), proposals CG-13, loop-integrity (no self-reinforcement).
- **Behavioral evidence:** rubric-only drift never blocks; still-failing criticals keep the block; revert is a recorded outcome (asserted).
- **Gaps:** automated A/B evaluation between model versions is post-startup (the comparison interface exists).
- **Startup scope:** ✅.

---

## Startup-critical gaps implemented THIS round (were open after R9)
1. The gate did not exist → behavioral regression protection now executable (M3).
2. Improvement proposals had no queue/boundary → M7.
3. The intellectual adversary did not exist → M4 (production-path ride).
4. Decision corrections were not audited → M5.
5. Scientific composition + investigation policy did not exist → M6.
6. The 12-step acceptance chain was partially covered → M8.

## Registered post-startup enhancements (none blocking)
Scheduled gate runs in CI (operational lane); LLM-quality rubric review workflow; deeper consequence traversal; automated A/B between model versions; numeric information-gain estimation; UI decision-diff view; recurring-failure→proposal auto-producer.
