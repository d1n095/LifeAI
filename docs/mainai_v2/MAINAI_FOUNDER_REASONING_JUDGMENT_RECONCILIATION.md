# MainAI Founder Reasoning + Judgment + Strategic Initiative — Architecture Reconciliation

Audit and canonical decision, written before any code — same discipline as the Intent/Goal
and Autonomous Development Director reconciliations. Two parallel audits plus direct reading
confirmed this is overwhelmingly an EXTENSION task: most of the executive-loop scaffolding
this task describes **already exists and is live**, under `app.mainai_executive`, plus three
more real systems this session had not previously encountered.

## 0. What already exists — confirmed by direct reading, not assumed

### `app.mainai_executive` — ALREADY the composed executive loop package

Its own module docstring: *"Composed MainAI executive loop — glue across memory, context,
workforce, continuity. This package does NOT replace existing subsystems. It wires them."*
This is EXACTLY this round's own charter, already half-built:

- `types.ExecutivePhase` — `UNDERSTAND→CORRECT→CONNECT→PLAN→ACT→VERIFY→STORE→LEARN→REPLAN→CONTINUE`, already the phase model the founder's original personal-intent doc specified.
- `types.PlanningHorizon` — `NOW/NEAR/MID/LONG`, already exactly the founder's own horizon vocabulary, plus `HORIZON_TO_PRIORITY` mapping onto the real `WorkCandidate.priority` vocabulary (`NOW/NEAR/LATER/OPTIONAL/BLOCKED`, migration 0065).
- `attention.AttentionAction`/`decide_attention()` — real, deterministic, founder-signal-driven WORK-continuity policy (`pause/resume/supersede/defer/parallelize/cancel/replan`). **Distinct axis from this round's "judgment" (SPEAK/CHALLENGE/etc.)** — this governs whether an in-flight GOAL continues, not whether MainAI SAYS something. Real gap: no communicative-temperament axis exists yet.
- `bounds.ExecutiveScanBounds` — bounds IDEA GENERATION per scan (max candidates/depth/elapsed/horizon items). **Not the same as WIP/workload awareness** (§10/§11 of the task) — this bounds how many NEW ideas one scan may produce, not how much work is already in flight. Real gap confirmed: nothing in this codebase checks "are my builders already busy" before creating more work.
- `priority.PriorityFactors`/`score_priority()`/`apply_hysteresis()` — a real, sophisticated priority engine (weighted factors, soft caps against low-confidence/viral-input dominance, hysteresis against horizon-thrash). Directly reusable for confidence-calibration-adjacent priority decisions — do not rebuild.
- `continuity.ContinuityCheckpoint`/`save_continuity_checkpoint()`/`load_continuity_checkpoint()`/`resume_summary()` — real, durable, restart-safe "what were we doing" continuity, already enforcing PROCESS MEMORY != AUTHORITY.
- `missing_piece.detect_missing_pieces()` — a REAL, already-built, deterministic "audit before building" checker: a keyword→existing-package map that estimates coverage and explicitly recommends against a new subsystem unless coverage is genuinely near-zero. This is the SAME discipline this document itself is applying, now formalized in code. Cite/reuse conceptually; do not duplicate its map without extending the real one if genuinely needed.
- `why_graph.why_feature_exists()`/`list_decision_debt()` — real, live "why" provenance chains (over `FounderMemoryNote`/`WorkCandidate`) and a real, bounded, high-impact-first decision-debt queue over disputed/superseded notes. Shallow today (no triggering-on-new-evidence/deadline logic) — a genuine, narrow extension point, not a rebuild.
- `lookaround.run_executive_lookaround()` — real: `active_context → lessons → bounded WorkCandidates`. This IS the embryonic form of §6's "Strategic Initiative Engine," scoped today to a single founder request's adjacent-impact scan. Extend this, do not build a parallel initiative engine.
- `completion.assess_completion()`, `assumption_scan.scan_assumptions_and_conflicts()`, `retrieval_quality.run_retrieval_quality_suite()`, `observability.executive_status_snapshot()`, `soak.run_executive_soak()`, `multi_session.run_multi_session_program()`, `loop.run_executive_cycle()`/`resume_executive_cycle()`, `safe_composed_run.run_composed_safe_internal_mainai_run()`, `school_bridge.py` — all real, all live, all already composing the systems below. A parallel "founder reasoning executive loop" would directly duplicate this entire package.

### Three more real systems, not previously known to this session

- **`app.intelligence_governance`** — a real, live evidence hierarchy: `IntelligenceExecution` (role: builder/reviewer/challenger/verifier/planner) → `IntelligenceEvidence` (append-only, `review_kind`: self_review/independent_model/deterministic_tool/founder) → `IntelligenceInterpretation` (append-only, `confidence: Numeric(5,4)`, real `supersedes_id` chain) → `IntelligenceIdea` (`idea_kind`, `content`, `disposition`: accepted/rejected/deferred/unknown, `disposition_reason`, `confidence`) → `IntelligenceIdeaLink` → `IntelligenceIdeaLesson`. This is near-exactly §5 (Disagreement Memory — `disposition_reason` IS the rejection-reason field) and most of §2's reasoning-memory shape. **The genuine gap**: `disposition`'s 4 flat values have no INCUBATING/PLANNED/READY/LATER richness (§8).
- **`app.inspectable_memory`** — the REAL, BUILT implementation of `docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md`'s `MemoryTruthState` (that doc is not design-only). `founder_correct_memory_note()`/`founder_dispute_memory_item()` are the real, already-existing correction/disagreement entry points — §4/§5 must call into these, never build parallel ones.
- **`app.mainai_execution.lessons`** — `record_lesson_from_founder_correction()` **already exists and already implements §4's exact ask**: takes a durable correction note, requires the caller's OWN explicit `root_cause`/`general_rule`/`applies_to` (never auto-derived — "one correction != universal law" already enforced), produces a real `EngineeringLesson`. `lesson_conflicts.py` already implements contradiction detection via a proven, reusable shape: deterministic tag-overlap narrowing → one fail-closed AI judgment call → both sides marked `disputed`, never auto-picks a winner. This is the exact template §19 (contradictions/currentness) should reuse.

### Supporting conventions confirmed real and load-bearing

- Status vocabulary `active/historical/superseded/disputed` (`EngineeringLessonStatus`, reusing `ActiveTruthStatus`/`ClaimStatus`'s own shape — a 3x-reused pattern; a 4th reuse, not a 5th invention, is the right move for any new status field).
- Confidence: `Numeric(5,4)` is a real, consistently-reused shared scale (`FounderMemoryNote`, `IntelligenceInterpretation`, `IntelligenceIdea`) — genuinely computed confidence (never self-reported) is separately proven via `app.rag.trust.assess_claim_confidence()`. New code should use `Numeric(5,4)`, not invent a new scale.
- `app.active_context` — real anchor→BFS→ranked-member traversal, bounded, 30+ `SUPPORTED_TYPES` already covering `founder_memory_note`/`work_candidate`/`engineering_lesson`/`life_intent`, real currentness states (`active/pinned/suppressed/stale/superseded`). Ranking today is traversal-order, not strength/evidence-weighted — the real gap for §20 (Retrieval).
- `app.memory_threads` — real generic typed-reference linking, `merge_threads()`/`branch_thread()` already exist — the real MECHANISM for §9 (Strategic Compression) and a precursor to §13 (Architectural Gravity); nothing today PROACTIVELY infers a pattern across threads.
- `app.capability_reality` — real status vocabulary, no decay/re-verification-over-time (a real, narrow gap for currentness modeling).
- `app.execution_envelopes` — the real propose/authorize doctrine this whole codebase reuses for every "X != AUTHORITY" boundary; §21's authority separation reuses this pattern, not a new one.
- `app.dev_director` (the just-frozen candidate, SHA `ab1c0ce`) already has its own `FounderOfflineMode`/`FounderBrief` concept (§18's closest prior art) — **per explicit instruction, this round uses it only as a conceptual/naming compatibility seam (cited in docstrings), never imported**, since importing a still-unreviewed, frozen candidate into new work would itself violate the freeze's spirit.

### Genuinely missing — confirmed real gaps, not found anywhere under any name

1. **Communicative judgment** (§7): SPEAK/CHALLENGE/PROPOSE_BIGGER/DEFER/INCUBATE/COMBINE/KILL/ESCALATE/STAY_QUIET as a decision about WHAT TO SAY/DO NEXT — `AttentionAction` governs work continuity, not this.
2. **Idea incubation lifecycle** (§8): richer than `IntelligenceIdea.disposition`'s 4 flat values.
3. **WIP/workload-aware selection** (§10/§11): nothing checks real active-assignment load before creating more work.
4. **Kill-criteria/sunk-cost** (§15): zero hits anywhere.
5. **Architectural gravity** (§13): zero hits anywhere; `missing_piece.py` runs the opposite direction (given a request, find coverage — not "notice an unrequested pattern").
6. **Strategic compression as a proactive decision** (§9): the linking mechanism (`memory_threads`) exists; the "combine before spawning many small jobs" decision does not.
7. **Meta-improvement loop** (§17): "founder correction → change future REASONING BEHAVIOR," distinct from `record_lesson_from_founder_correction()`'s existing "correction → engineering lesson about CODE."
8. **Rejected-idea non-resurfacing enforcement** (§5, second half): `disposition_reason`/`dismissed_reason` fields exist; nothing checks a NEW idea against recently-rejected ones before allowing it through.
9. **Success-pattern strengthening as an explicit mechanism** (§16): no confirmed "this keeps working, strengthen it as a default" loop.
10. **Strength/evidence-weighted retrieval ranking** (§20): `active_context`'s ranking is traversal-order only.
11. **Decision-debt triggering logic** (§14, extension of real `list_decision_debt()`): surfacing on new-evidence/deadline/dependency-ready, not just a static bounded list.

## 0.1 Correction confirmed during Part 1 implementation (2026-09-07)

This section's original §1.2 assumed `IntelligenceIdea.disposition` could be mutated in
place, mirroring `LifeIntent.state`. Direct testing against real Postgres during Part 1 build
disproved this: migration 0038's `trg_intelligence_ideas_deny_mutation` trigger makes **all
six** `app.intelligence_governance` tables DB-enforced append-only (`BEFORE UPDATE OR DELETE`,
unconditional outside account erasure) — an `UPDATE` on `intelligence_ideas.disposition` is
rejected by Postgres itself. `idea_incubation.transition_idea_disposition()` was built to
INSERT a new `IntelligenceIdea` row via the real `record_idea()` and link it back via
`record_idea_link()` (`relation="disposition_transition"`, migration 0070 additive widening),
mirroring `IntelligenceInterpretation`'s own established "recalculation inserts a new row"
ledger pattern instead. Two small additive migrations (0069, 0070) resulted instead of one —
still no new table. Everything else in §0/§1 stands as originally decided.

## 1. The decision

**New modules added directly to `app.mainai_executive`** (its own charter is exactly this —
"wires them, does not replace" — matching `dev_director`'s own precedent of adding
`budget_integration.py` to its existing package rather than spinning up a sibling). No new
top-level package. Composes with, never duplicates, every system in §0.

1. **`judgment.py`** — new `JudgmentAction` enum (`SPEAK/CHALLENGE/PROPOSE_BIGGER/DEFER/
   INCUBATE/COMBINE/KILL/ESCALATE/STAY_QUIET`), a real, deterministic (not LLM-first, matching
   `attention.decide_attention()`'s own proven deterministic-policy shape)
   `decide_judgment()` function taking real signals (confidence, evidence strength, WIP load,
   founder-attention cost, whether a similar idea was recently rejected) and returning a
   `JudgmentAction` + reason. AGREEMENT != GOOD REASONING enforced structurally: no code path
   defaults to `SPEAK`-agree without evidence.
2. **`idea_incubation.py`** — extends `IntelligenceIdea.disposition`'s real vocabulary
   (a small, additive DB migration — new enum values, not a new table, matching the
   `work_candidates.priority` widening precedent from migration 0065) to add
   `incubating`/`planned`/`ready`/`later`, plus real transition functions mirroring
   `LIFE_INTENT_TRANSITIONS`'s now-established explicit-table convention. GOOD IDEA != ACTIVE
   JOB structurally enforced: no function moves an idea directly from `incubating` to
   "active work" — it must pass through the REAL `work_candidates.authorize_work_candidate()`
   gate unchanged.
3. **`wip_awareness.py`** — real workload queries against the REAL, LIVE `WorkforceAssignment`
   (`app.workforce`) and `MainAITask` (`app.mainai_execution`) status columns (not the frozen,
   unreviewed `dev_director` candidate) — `current_wip_load()`, `default_wip_limit()`,
   `should_defer_new_work()`. MORE PARALLELISM != MORE PROGRESS enforced via a real, tested
   ceiling, not a suggestion.
4. **`kill_criteria.py`** — real `evaluate_kill_criteria()` over `WorkCandidate`/`LifeIntent`/
   `EngineeringLesson` evidence (expected-value collapse, superseding architecture, failed
   assumptions) — SUNK COST != CONTINUE. Recommends only; never itself cancels/kills (routes
   through the real, existing `app.work_candidates.supersede_work_candidate()`/
   `app.life_intents.transition_intent()` — this round's own P0-fixed state machine — for the
   actual state change).
5. **`architectural_gravity.py`** — extends `memory_threads` (real `merge_threads()`) with a
   real, bounded, deterministic-first (tag/component overlap across recent threads, matching
   `lesson_conflicts`' own narrowing-before-AI-judgment shape) `detect_architectural_gravity()`
   that proposes (never auto-creates) a formalization candidate.
6. **`strategic_compression.py`** — given several related `WorkCandidate`/`IntelligenceIdea`
   rows (via `memory_threads` grouping), a real `compress_into_program()` proposes ONE
   coherent grouping rather than spawning N candidates — tested explicitly: 8 related
   suggestions must not become 8 active jobs by default.
7. **`meta_improvement.py`** — a distinct, narrow extension of `record_lesson_from_founder_
   correction()`'s own real pattern, scoped specifically to MainAI's OWN behavioral defaults
   (`affected_component` values like `"judgment_temperament"`/`"wip_default"`/
   `"challenge_threshold"`) rather than code — reuses `EngineeringLesson`'s real schema and
   `lesson_conflicts`' real contradiction handling, never a new table.
8. **`rejected_idea_guard.py`** — a real, deterministic (tag/title-overlap, matching
   `lesson_conflicts`' narrowing technique) `check_recently_rejected()` gate, called before a
   new idea is recorded, checking `IntelligenceIdea.disposition == "rejected"` /
   `WorkCandidate.status == "dismissed"` history for the same owner.
9. **`retrieval.py`** — extends `active_context`'s real traversal with a strength/evidence-
   weighted re-ranking pass (confidence, recency, contradiction count) over its own real
   `activation_path`-ranked output — never a parallel retrieval index.
10. **`why_graph.py` extension** (existing file, real, additive changes only) — add
    triggering logic to `list_decision_debt()` (surface on new evidence/dependency-ready/
    deadline), reusing its own existing bounded-queue shape.

**Confidence**: all new fields use the real, already-reused `Numeric(5,4)` scale. **Status**:
any new status field reuses `active/historical/superseded/disputed`. **Authority**: nothing
in this round grants execution/spend/merge/deploy/disclosure authority — every real state
change routes through an EXISTING, already-authorized function
(`work_candidates.authorize_work_candidate()`, `life_intents.transition_intent()`,
`inspectable_memory.founder_correct_memory_note()`) — this round only decides WHETHER/WHEN
to call them, never bypasses or duplicates their own gates.

## 2. What this reconciliation does NOT do

- Does not create a new memory table family — extends `IntelligenceIdea`'s enum (one small,
  additive migration) and reuses every other existing table as-is.
- Does not modify `app.mainai_execution.lessons`/`lesson_conflicts`/`app.inspectable_memory`/
  `app.intelligence_governance`/`app.active_context`/`app.memory_threads`/
  `app.capability_reality`'s own service files — reads and composes only, except the one
  additive `IntelligenceIdea.disposition` migration.
- Does not import or modify the frozen `dev_director` candidate (SHA `ab1c0ce`) — conceptual
  citation only, matching `future_integration.py`'s own established no-import discipline.
- Does not import any Codex branch or wire external providers.
- Does not modify #245.
