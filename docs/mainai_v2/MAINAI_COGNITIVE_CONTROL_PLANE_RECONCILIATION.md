# MainAI Cognitive Control Plane — Reconciliation

Architecture decision, written before implementation, following this program's own established
discipline: audit first, decide, then build. Grounded in direct reading of `app.mainai_executive`
(all 24 modules), `app.project_entities` (models + service), `app.capability_reality`,
`app.work_candidates`, `app.resource_intelligence` (this program's own prior round), and the
relevant migrations (0054–0056, 0065, 0069) — not assumed.

## 0. What already exists — confirmed by direct reading, not assumed

The founder's Phase B request describes eight subsystems (Vision Compiler, Dynamic Completion
Engine, Vision Gap Generator, Cognitive Loop, Statistics Command Center, Evidence Intelligence,
Continuous Improvement, Founder Program Truth). Direct reading of `app.mainai_executive` (built
in the earlier "Founder Reasoning + Judgment" round, merged `4814d78`) shows **most of the
underlying primitives already exist**, narrower in scope and not yet unified into one graph:

| Requested | Already exists as | Gap |
|---|---|---|
| Cognitive Loop | `loop.run_executive_cycle()` — a real, composed UNDERSTAND→CONNECT→PLAN→ACT→VERIFY→STORE→LEARN→REPLAN→CONTINUE cycle, durable via `continuity.py`, already wired to lookaround/missing-piece/assumption-scan/staffing/school-routing | Not framed around a canonical *vision graph*; does not recompute graph-wide completion |
| Vision Gap Generator | `missing_piece.detect_missing_pieces()` — deterministic keyword→existing-package coverage heuristic | Returns a coverage %, not typed graph nodes; no capability-implication taxonomy |
| Dynamic Completion Engine | `completion.assess_completion()` — 10 boolean evidence dimensions for ONE named feature, caller-supplied flags | Single-feature, not graph-wide; no maturity ladder; no weighted, denominator-expandable completion |
| Continuous Improvement | `meta_improvement.py` — behavioral-lesson recording + conflict detection, reusing `EngineeringLesson`/`lesson_conflicts` verbatim | No explicit BUILD-loop/IMPROVEMENT-loop separation; no "100% still finds work" test |
| WHY / decision debt | `why_graph.why_feature_exists()` / `list_triggered_decision_debt()` — real provenance chains + triggered surfacing | Not connected to a typed requirement/capability graph |
| Founder Program Truth | `dashboard.founder_executive_dashboard()` — already answers "what is she doing/why/next/blocked/uncertain/learned" | Has no *completion %*, no vision-graph view, no evidence-quality view |
| Judgment substrate | `judgment.decide_judgment()` — pure, deterministic SPEAK/CHALLENGE/PROPOSE_BIGGER/DEFER/INCUBATE/COMBINE/KILL/ESCALATE/STAY_QUIET table (the founder's own named reasoning substrate, SHA `4814d78`) | This is the substrate, reused verbatim, not extended |
| Idea incubation ("large idea during high WIP") | `idea_incubation.py` — full disposition state machine (`unknown→incubating→planned→ready→...`), already exercises exactly the "large idea incubated instead of interrupting critical path" scenario via `judgment.py`'s own WIP branch | None — reused as-is |
| Strategic compression ("N related items → 1") | `strategic_compression.compress_into_program()` | None — reused as-is |
| Statistics/metric record shape | `app.resource_intelligence.types.MetricEnvelope` (value/unit/definition/denominator/window/population/sample_size/source/method/missing_data/uncertainty/last_updated/trend) — this program's own prior round | Missing `exclusions` and an explicit observed/derived/estimated tag; extend, don't duplicate |
| **Canonical typed entity/requirement graph** | `app.project_entities` (`ProjectEntity`/`ProjectEntityRelationship`/`InterpretationProposal`, migration 0054) — owner-scoped, RLS, supersession chain, authority/basis/confidence, SIGNAL PRODUCER != TRUTH WRITER staging via `promote_interpretation_proposal()` | `entity_type` CHECK is `{idea, decision, task_reference, vision_statement, open_question}` — **no vision/capability/requirement/risk/etc. vocabulary yet.** `relationship_type` CHECK is `{relates_to, supersedes, contradicts, blocks, answers, duplicates, derived_from}` — **no depends_on/implies/verifies/satisfies.** This is the real gap: the graph MECHANISM already exists and is exactly right (RLS, supersession, SIGNAL != TRUTH), it just doesn't have the vision vocabulary yet. |
| Evidence Intelligence (OBSERVED/DERIVED/INFERRED/PLAUSIBLE/SPECULATIVE/CONTRADICTED/VERIFIED + confound reasoning) | **Genuinely new** — nothing in this codebase models evidence quality/confounding/denominator-baseline reasoning today | New |
| "What Changes My Mind" per-conclusion ledger | **Genuinely new** in this exact shape (closest precedent: `why_graph.list_decision_debt()`, which surfaces disputed notes but does not track strengthens/weakens/reverses) | New, composes with `why_graph` |

**Decision, matching this program's own standing rule ("if what you're building overlaps with
something already in progress, build on top of it, don't create a competing solution"):**
`app.mainai_vision` is a **composition/integration layer**, not eight new subsystems built from
scratch. It:
1. Widens `project_entities`' two CHECK constraints (one small, additive migration — the
   established pattern for this exact table family, matching migration 0065's
   `work_candidates.priority` widening and migration 0069's `intelligence_ideas.disposition`
   widening) to add the vision vocabulary.
2. Compiles a canonical, typed, in-memory `VisionGraph` at read time from those widened
   `project_entities` rows plus `WorkCandidate`/`CapabilityRecord`/`EngineeringLesson` (derive,
   never duplicate — no new source of truth).
3. Adds the genuinely new computation this codebase does not have: graph-wide weighted maturity/
   completion with an expandable denominator, a capability-implication gap generator, an evidence
   -quality reasoning layer, and a founder-truth compiler that composes the existing dashboard
   with the new completion/gap/evidence views.
4. Wraps (never reimplements) `run_executive_cycle()`, `judgment.decide_judgment()`,
   `missing_piece.detect_missing_pieces()`, `meta_improvement.py`, `why_graph.py`,
   `strategic_compression.py`, `MetricEnvelope`.

## 1. Migration: widen `project_entities`' vocabulary (additive only)

`ck_project_entities_entity_type` gains: `domain, capability, system, subsystem, requirement,
implied_requirement, invariant, risk, acceptance_criterion, verification_criterion` (10 new
values; `vision_statement` already covers "vision" — no new value needed there).
`ck_project_entity_relationships_type` gains: `depends_on, implies, verifies, satisfies,
mitigates` (5 new values). No new table. No change to RLS, supersession, or any existing row.
`promote_interpretation_proposal()`/`mark_project_entity_superseded()` are reused completely
unchanged — the new vocabulary flows through the SAME staging pipeline (SIGNAL PRODUCER != TRUTH
WRITER still holds: nothing in `app.mainai_vision` writes a `ProjectEntity` row directly).

## 2. Package `app.mainai_vision`

1. **`types.py`** — `MaturityState` (13-value ladder), `VisionNodeKind` (the 11 widened
   entity_type values used by this package + `vision_statement`), `VisionEdgeKind` (the 5 widened
   + `depends_on`'s siblings), `VisionNode`/`VisionEdge`/`VisionGraph` dataclasses. Re-exports
   `app.resource_intelligence.types.MetricEnvelope`/`unknown_metric` verbatim (no competing metric
   shape) plus one additive `MetricEnvelope`-shaped extension point for `exclusions`/
   `observation_basis` via a thin wrapper dataclass (never modifying the original, still used
   as-is by `resource_intelligence`).
2. **`vision_compiler.py`** — `compile_vision_graph()`: read-only, compiles current (never
   superseded/disputed/rejected) `project_entities` rows (filtered to the widened vocabulary) +
   relationships into a `VisionGraph`. VISION != AUTHORITY structurally: zero writes.
3. **`completion.py`** — `MaturityState` scoring per node (derived from linked
   `WorkCandidate.status`/`CapabilityRecord.status`/`EngineeringLesson`/independent-review
   evidence — reusing `mainai_executive.completion.assess_completion()`'s own evidence-dimension
   doctrine, never reinventing "what counts as tested"), weighted completion across the 11 named
   dimensions, with a denominator that expands when the graph gains new nodes.
4. **`gap_generator.py`** — `propose_implied_requirements()`: a documented capability-implication
   table (same hand-picked-threshold convention as `judgment.py`/`decision.py`) plus
   `missing_piece.detect_missing_pieces()` as one input signal; writes proposals via the EXISTING
   `record_interpretation_proposal()` only — never promotes itself.
5. **`cognitive_loop.py`** — thin composition wrapper around `run_executive_cycle()`, adding
   vision-graph recompilation + completion recomputation + gap proposal as explicit, cited extra
   steps; returns a `CognitiveLoopResult` referencing the underlying `ExecutiveCycleResult`
   unchanged.
6. **`evidence.py`** — genuinely new: `EvidenceState` enum, `EvidenceClaim`/`EvidenceItem`
   dataclasses, confound/denominator/baseline reasoning helpers, `REPEATED SOURCE != INDEPENDENT
   EVIDENCE` dedup-by-underlying-source logic.
7. **`statistics.py`** — one coherent registry composing `MetricEnvelope`s from
   `resource_intelligence`, `capability_reality`, `work_candidates`, this package's own
   completion/gap counts — never a second metric shape.
8. **`improvement.py`** — BUILD_LOOP vs IMPROVEMENT_LOOP separation; at-100% bottleneck/
   simplification/cost proposal, composing `meta_improvement.py` for the lesson-candidate side.
9. **`mind_change.py`** — "What Changes My Mind" ledger for important conclusions, composing
   `why_graph.list_decision_debt()`/`FounderMemoryNote` (no new table).
10. **`founder_truth.py`** — composes `dashboard.founder_executive_dashboard()` (unchanged) with
    completion/gap/evidence summaries into one founder-facing truth payload.
11. **`adapters.py`** — typed `Protocol` seams for Founder Reasoning (real, composed directly —
    merged on this branch), Resource Intelligence (real, composed directly — this program's own
    prior round, same branch), and Development Director / Continuous Supervision / Personal
    Recall / V1 readiness (NOT present on this branch — those are separate, unmerged Codex
    lanes/frozen candidates; adapters here are typed interfaces only, honestly disclosed as
    not-yet-wireable until those candidates merge, matching this package's own "RUNTIME STATE !=
    REASONING STATE" boundary).

## 2. What this reconciliation does NOT do

- Does not modify `app.mainai_executive`, `app.resource_intelligence`, `app.capability_reality`,
  `app.work_candidates`'s own service files — reads and composes only.
- Does not create a second spend ledger, entity graph, checkpoint mechanism, or metric shape.
- Does not grant this layer authority to authorize work, kill/dismiss candidates, or promote a
  `ProjectEntity` itself — `record_interpretation_proposal()`/staging only.
- Does not modify the frozen `dev_director` candidate, the verified runtime, `#245`, Personal
  Recall, or the Continuous Supervision candidate — none of those branches are touched, and none
  of their code is imported (they are not present on this branch).
- Does not merge, deploy, or enable real providers.
