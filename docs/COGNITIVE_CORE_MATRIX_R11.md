# Cognitive Core Capability Matrix — Round 11 (integration-readiness classification)

**Branch:** `zai/alpha-runtime-reliability` @ R11 head (`5bfccbad` at matrix-write time; see the R11 report for the exact final HEAD)
**Extends:** `docs/COGNITIVE_CORE_MATRIX_R10.md` (implementation detail per capability lives there; this file classifies VERIFICATION STATUS with R11 evidence).
**Evidence base:** deterministic suites on real PG/Redis; the REAL approved model (z-ai CLI → glm-4-plus) through the production registry path; the 10-step cross-session scenario with restarts; the ablation suite; the integration-contract suite.

Classification legend: **Verified** (executed behavioral evidence) · **Partially verified** (core proven, a named slice open) · **Missing** · **Blocked by external dependency** · **Deferred until after private startup**.

| # | Capability | Status | R11 evidence | Open slice |
|---|---|---|---|---|
| 1 | Independent hypothesis generation | **Verified** | RM-07: the real model presented BOTH competing explanations, majority-of-3; deterministic composition counts alternatives | — |
| 2 | Critical, source-based research | **Verified** | RM-05 real-model source fidelity (true figure reproduced, no fabricated figure); provenance chain end-to-end; fabricated citations structurally excluded | — |
| 3 | Causal and consequence reasoning | **Verified** | design-support checks; weak-causal objections incl. case_series; consequence naming in prompt; composition buckets distinct | — |
| 4 | Persistent, self-correcting memory | **Verified** | Phase 3's 10-step scenario: conclusion re-evaluated on contradiction, original preserved verbatim, correction recorded, fresh-session recall, why answerable, NO revival | — |
| 5 | Controlled proactive improvement | **Verified** | Phase 6: full lifecycle with estimates, dedup, founder-only decisions, audited revert, no privileged path (source-inspection + end-to-end integrity) | recurring-failure→proposal auto-producer **Deferred** |
| 6 | Model-provider independence | **Verified** | Phase 2: the approved model ran through the ADMIN provider-config path with run-scoped activation fully restored; stored verdicts provider-independent (R10 step 11); deterministic ladder model-free | — |
| 7 | Cognitive regression protection | **Partially verified** | gate executes deterministic + MODEL-mode (24 trials, 8/8 behavior cases majority-passed against glm-4-plus; baseline persisted via migration 0077; comparison blocks critical regressions) | scheduled/CI gate execution **Blocked by external dependency** (operational lane owns CI); comparison-test run skipped honestly on model-service quota once (environmental) |
| 8 | Intellectual adversarial review | **Verified** | Phase 4 BOTH directions: real weaknesses objected (weak funded case-series), best-supported consensus → ZERO objections, conflict stays symmetric 1-vs-4 (no popularity/contrarian bias) | LLM-quality objection reasoning **Deferred** |
| 9 | Epistemic integrity | **Verified** | ladder + framing CONTRACT C1-C7; C4 hardening: marker smuggling defanged (real escape closed); contract verified on the REAL assembled prompt | — |
| 10 | Cognitive sovereignty | **Verified** | RLS isolation self-asserts (service RAISES on foreign owner); founder-ACCOUNT gating; another owner's corpus invisible cross-session | — |
| 11 | Causal verification | **Verified** | causal claims without declared alternatives counted and named; policy requires explicit evidence | — |
| 12 | Memory integrity and provenance | **Verified** | append-only supersession audited (both ids + bases); provenance chain claim→chunk→source survives restarts; atomicity under concurrent races (loser rolls back with NO debris) | — |
| 13 | Goal and decision continuity | **Verified** | decisions + rationale + corrections across restarts (Phase 3); plan-event provenance (R9/R10 suites unchanged-green) | — |
| 14 | Cognitive resource governance | **Partially verified** | bounds measured and calibrated (full chat ≈70 fixed + cognitive add-on bounded; science view ≤6; graph block ≤9); simple asks carry NO cognitive lines (relevance gate) | absolute per-reply cost optimization **Deferred** (registered post-startup) |
| 15 | Uncertainty-driven investigation | **Verified** | all four policy outcomes with cost caps; RM-04: the real model expressed uncertainty for the assumption-only claim, majority-of-3 | numeric information-gain **Deferred** |
| 16 | Controlled capability evolution | **Partially verified** | gate + proposals + audited revert + lesson effectiveness machinery all proven | automated A/B between model versions **Deferred** |

## Startup-critical capabilities — all 16 have executed behavioral evidence; none Missing.

**Blocked by external dependency (2):** scheduled/CI gate runs (operational lane's CI); a locally-installed Ollama runtime (absent in this sandbox by design — the approved z-ai model served as the real-model evaluator; the adapter is provider-agnostic so Ollama slots in without code change).

**Deferred until after private startup (7, none blocking):** auto-producer for proposals; LLM-quality rubric review workflow; numeric information-gain; deeper consequence traversal; automated A/B; per-reply statement optimization; UI decision-diff.

## Real-model evaluation results (Phase 2)
- Adapter: `app/providers/zai_provider.py` (approved model via the standard LLMProvider interface; OpenAI-shaped parsing; honest 429→rate_limited classification).
- 8 behavior surfaces × 3 trials through the REAL production chat path (registry-resolved provider, real retrieval/framing): injection resistance, premise challenge, sycophancy resistance, uncertainty calibration, source fidelity, context retention, hypothesis generation, initiative boundary — **all 8 majority-passed** (24 trials; assertion details in `tests/backend/test_model_gate_r11.py`; failures would name the case + trial).
- Deterministic vs scored discipline preserved: structural text assertions auto-enforced; reply QUALITY recorded as rubric (human review), never gating.
- No model promotion/replacement: run-scoped provider activation fully restored (asserted); the comparison interface blocks critical regressions and never switches models.
