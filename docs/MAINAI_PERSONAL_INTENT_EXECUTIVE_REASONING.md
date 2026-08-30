# MainAI — Personal Intent & Executive Reasoning Core

Founder requirement, verbatim scope (2026-08-30): MainAI ("hon" — MainAI, never Claude, never
Cursor) must reduce the founder's need to formulate perfect instructions. He may use the wrong
term, confuse two components, misspell names, refer to something indirectly ("do the same
thing there", "include that", "think about everything around it"), forget a dependency,
suggest something already implemented, or suggest something that conflicts with an earlier
decision. MainAI must not simply execute the literal sentence — she should
UNDERSTAND → CORRECT → EXPAND → CONNECT → PLAN → ACT → VERIFY → STORE → LEARN.

This document specifies an **implementation-ready architecture** for that capability. It is
deliberately not a from-scratch design: this codebase already has most of the load-bearing
primitives this requirement needs, built for adjacent purposes over several prior increments.
The work here is real, but it is composition and targeted extension, not a parallel subsystem.
See `docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md` for the memory-truth invariant and inspection
contract, and `docs/MAINAI_LONG_HORIZON_PLANNING.md` for the NOW/NEAR/MID/LONG planning layer —
both referenced here, not duplicated.

---

## 0. What already exists — do not rebuild this

Confirmed by direct code inspection (not assumed), so this section is a citation list, not a
survey:

| Capability the founder asked for | Existing primitive | Verdict |
|---|---|---|
| "Founder language memory != factual truth. Interpretation != authority." | `app.founder_memory_signals` — `CandidateLearningSignal` (no `authority`/`basis` columns at all: structurally cannot assert truth) → `promote_candidate_signal()` (the ONLY path to a real `FounderMemoryNote`, always requires the caller's own explicit `authority`/`basis`, never the signal's own `classifier_confidence`) | **Reuse directly** — this IS the doctrine, already built and tested (`test_promoting_a_signal_requires_the_callers_own_explicit_authority_never_the_classifiers_confidence`) |
| "RAW EXPRESSION → INTERPRETED INTENT → REFERENCED ENTITY → CONFIDENCE → EVIDENCE/CONTEXT → CORRECTION" | `app.project_entities` — `InterpretationProposal` (`classifier_strategy`/`confidence`/`reasoning`, `status`, `promoted_to_entity_id`) → `promote_interpretation_proposal()` → `ProjectEntity` (`supersedes_entity_id`, `decided_by`) | **Same shape, wrong source.** Built for document-derived `KnowledgeClaim`s, not live conversation. §1 below extends the pattern to a conversational source, reusing the promotion/supersession mechanics verbatim. |
| Never infer emotional/psychological state | `app.founder_memory`'s own hard rule — "no column, no `note_type` value, no vocabulary anywhere in this foundation for it" (`docs/LIFE_FOUNDER_MEMORY.md`) | **Standing constraint, inherited as-is.** Nothing in this document introduces a psychological-state field. |
| "MISS → ROOT CAUSE → GENERALIZED LESSON → APPLICABILITY → RETRIEVAL TRIGGER → FUTURE PLAN CHECK" | `app.mainai_execution.lessons` — `EngineeringLesson` (`root_cause`/`general_rule`/`applies_to` tags/`confidence`) → `lookup_lessons()` retrieves by tag match → `apply_lessons_to_verification_plan()` wired into `create_plan()`, injecting `regression_test` BEFORE planning completes | **Already fully built**, for verification-failure-sourced misses. §5 below extends the SAME table/functions to accept founder-conversation-sourced misses ("we forgot X"), not a new table. |
| "Avoid overgeneralizing a one-off correction" | `app.mainai_execution.lesson_conflicts` — real lesson-vs-lesson contradiction detection; conflicting lessons both marked `disputed`, neither applied | **Reuse directly**, unchanged. |
| "Generate well-justified follow-up work items: why/source/confidence/dependency/priority/status NOW/LATER/OPTIONAL/BLOCKED" | `app.work_candidates` — `WorkCandidate` (`title`/`rationale`/`dependencies: JSONB`/`priority`/`status`/`classifier_strategy`/`classifier_confidence`/`provenance`), `record_work_candidate()` never creates a goal, only `authorize_work_candidate()` does | **Extend, don't parallel.** §2 below adds NOW/LATER/OPTIONAL/BLOCKED as real `priority`/`status` values rather than inventing a second table. |
| "Do not create endless speculative tasks" | `app.autonomous_gap.service` — `GapGenerationBounds` (`max_gaps_per_run`, `max_children_per_run`, `max_generation_depth`, `max_unresolved_gaps`, `max_elapsed_seconds`) | **Reuse the bounding pattern directly** for the new executive-scan generator (§2). |
| "Interpretation != authority", scoped to development work | `app.safe_planner` — `FounderPlanningRequest.authority_kind` (`AUTHORIZED_KINDS` vs `NON_AUTHORITATIVE_KINDS`, unknown fails closed), `ambiguity_refs`/`contradiction_refs`/`deterministic_resolutions` as durable, separately-tracked fields | **Generalize the vocabulary**, narrowly scoped today to development-work planning; §3 lifts `authority_kind` into the conversational layer. |
| "What's relevant right now" / adjacent-component scan | `app.active_context` — anchor → edge-expansion → ranked member set, `activation_path` (literally records WHY something was pulled in) | **Reuse directly** as the traversal primitive for §2's look-around scan. |
| Cross-linking a memory item to the goals/tasks/entities it affects | `app.memory_threads` — generic typed-reference linking (`member_kind`/`member_ref_id`) across ANY registered entity, without collapsing either side into the other's shape | **Reuse directly**, unchanged. |
| "Planning != authority; future effects still need current authority at effect time" | `app.execution_envelopes` — `propose_execution_scope()` never writes to `execution_authorization_envelopes`; only `authorize_execution_scope()` does, always requiring the caller's own explicit assertion, never copying the proposal | **The canonical doctrine this entire document's authority boundary reuses verbatim** (§6). |

**Net effect:** roughly two-thirds of what the founder described already exists, under
different names, built for adjacent purposes. The genuinely new pieces are: (a) a
conversational entity/reference-resolution layer (§1), (b) an executive look-around
orchestrator that composes `active_context` + `autonomous_gap`-style bounded generation into
`WorkCandidate` rows (§2), and (c) the memory-truth invariant and inspection contract, which
have no existing unified analog anywhere in the codebase — those live in
`docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md`.

---

## 1. Personal language / intent model

### 1.1 The problem this section actually solves

`app.context.resolver` already classifies a chat turn's TYPE
(`INTENT_EXPLICIT_MEMORY`/`INTENT_CORRECTION`/`INTENT_IDEA_WORTH_SAVING`) and
`app.founder_memory_signals` already stages that classification as a `CandidateLearningSignal`
before any truth claim exists. What's missing: when the founder says "do the same thing there"
or "the other auth thing", nothing today resolves WHICH entity "there"/"the other auth thing"
refers to. That's a reference-resolution problem, not an intent-classification problem — a
genuinely new capability.

### 1.2 IMPLEMENTED (2026-08-30): extends `CandidateLearningSignal`, not a new table

**Design correction, recorded here so this document stays accurate** (see
`docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md`'s own memory-truth invariant — a doc describing a
design that was superseded during implementation is exactly the kind of SAID-vs-IMPLEMENTED
drift that invariant exists to prevent): the original sketch below proposed a new
`conversational_interpretation_proposals` table mirroring `app.project_entities.
InterpretationProposal`. Re-reading the actual schema before building anything showed
`app.founder_memory_signals.CandidateLearningSignal` (migration 0053) already carries nearly
every column that sketch needed — `source_message_id` (already a bare FK to `messages.id`,
already live-wired into `app/routers/chat.py`), `classifier_strategy`/`classifier_confidence`/
`classifier_reasoning`, `status`, `provenance`, the whole `record_candidate_signal()` →
`promote_candidate_signal()` staging discipline. A parallel table would have duplicated all of
that for no real benefit. **Implemented instead**: migration 0064 adds three nullable columns
to the existing table —

```python
resolved_entity_type: str | None      # loose string for now, not yet validated against
                                       # app.active_context.service.SUPPORTED_TYPES' closed
                                       # registry (a reasonable follow-up once real resolver
                                       # usage exists to validate against)
resolved_entity_id: uuid.UUID | None
resolution_reasoning: str | None
```

plus one new service function, `app.founder_memory_signals.resolve_candidate_signal_entity()`
— same precondition as `dismiss_candidate_signal()`/`promote_candidate_signal()` (signal must
still be `unreviewed`), same epistemic status as `classifier_confidence` (a resolver's own
guess, never a truth claim on its own — `promote_candidate_signal()`, unchanged, remains the
only path to real founder knowledge). Tests: `tests/backend/mainai/
test_candidate_learning_signals.py`. No `corrects_proposal_id`/self-correction-chain field was
added (a genuine scope cut, not an oversight) — a wrong resolution is corrected today via
`dismiss_candidate_signal()` on the wrong signal plus a fresh one, matching how this table
already handles every other "this was wrong" case; a dedicated correction chain can be added
later if that proves insufficient in practice.

`raw_expression` (already `CandidateLearningSignal`'s own message-derived content, via
`source_message_id`) vs `resolved_entity_id` is the concrete instantiation of the founder's own
"RAW EXPRESSION → INTERPRETED INTENT → REFERENCED ENTITY" split — never collapsed into one
field, exactly matching `founder_memory_notes.content` (immutable) vs the entity it's ABOUT.

**Original sketch, superseded, kept only for the reasoning trail below (§1.3's resolution flow
still applies, just producing `resolve_candidate_signal_entity()` calls instead of a new
table's rows):**

```python
class ConversationalInterpretationProposal(Base):  # NOT BUILT -- see above
    __tablename__ = "conversational_interpretation_proposals"
    ...  # superseded by CandidateLearningSignal + migration 0064, see above
```

### 1.3 Resolution flow

```
chat turn arrives
  -> app.context.resolver classifies intent TYPE (existing, unchanged)
  -> IF the turn contains an indirect/anaphoric reference ("that", "the other X", "there",
     "same as before") -- a NEW, narrow classifier, `resolve_conversational_reference()`:
       1. Candidate set: query `memory_threads` for threads with recent activity in this
          conversation/project (reuse `expand_thread`/`current_members`, existing).
       2. Candidate set: query `active_context` for the CURRENT anchor's ranked members
          (reuse `refresh_context`, existing) -- these are exactly "the things already
          established as relevant to what we're talking about".
       3. Deterministic narrowing first (recency + explicit prior mention in this thread),
          matching `lesson_conflicts.py`'s own "deterministic candidate narrowing before any
          AI judgment" precedent -- never jump straight to an AI disambiguation call.
       4. If exactly one high-confidence candidate survives: record a
          `ConversationalInterpretationProposal` with that `resolved_entity_id`, status
          `unreviewed`.
       5. If zero or multiple ambiguous candidates survive: record the proposal with
          `resolved_entity_id = null`, `resolved_entity_type = "unknown"` -- an HONEST
          admission of ambiguity (matching `propose_execution_scope()`'s own "empty proposal
          beats a fabricated one" doctrine), never a guess presented as resolved.
  -> record_candidate_signal() (existing, unchanged) still fires independently for intent-type
     classification -- these are two separate signal producers, not one merged pipeline; a
     turn can be BOTH a correction (resolver) AND a reference needing resolution (this
     section), and both get their own staged, reviewable row.
```

### 1.4 Promotion — the ONLY path to acting on a resolved reference

**IMPLEMENTED (2026-08-30), unchanged from what already existed**: `resolve_candidate_signal_
entity()` (§1.2) only records the resolver's own guess — it never itself acts on it.
`app.founder_memory_signals.promote_candidate_signal()` (already built, migration 0053, no
change needed for this) remains the ONLY path to real founder knowledge, and already requires
the caller's own explicit `authority`/`basis` — never the signal's own `classifier_confidence`
copied in, proven by the existing `test_promoting_a_signal_requires_the_callers_own_explicit_
authority_never_the_classifiers_confidence`. A caller that wants to act on a RESOLVED entity
simply reads `signal.resolved_entity_id` as context when constructing the `content` it passes
to `promote_candidate_signal()` — no separate promotion function was needed. A wrong
resolution is corrected via `dismiss_candidate_signal()` on the mis-resolved signal plus a
fresh `record_candidate_signal()` + `resolve_candidate_signal_entity()` pair, not a mutation in
place — matching every other "this was wrong" case this table already handles.

`confirmed_by` (i.e. the caller of `promote_candidate_signal()`) in practice is one of: a
founder's explicit chat reply ("yes, that one"), OR (for low-ambiguity, reversible,
PLANNING-only actions — never a consequential effect) MainAI's own executive-reasoning layer
proceeding on its best resolution while flagging it, per §4 (Self-Correction) below. The
distinction is drawn exactly where `execution_envelopes` draws it elsewhere in this codebase:
resolving a reference to inform PLANNING costs nothing to get wrong and can proceed on
inference; resolving a reference that gates a CONSEQUENTIAL EFFECT (which file to edit, which
task to authorize) must be confirmed before that effect, per §6.

### 1.5 What this section explicitly does NOT do

- Does not touch `founder_memory_notes.content` — a resolved reference points AT an entity,
  it never rewrites what a founder actually said.
- Does not infer emotional/psychological state (inherited hard rule, §0).
- Does not let a classifier's own confidence stand in for `authority`/`confirmed_by` anywhere
  in the promotion path (inherited invariant, §0).
- Does not create a second `EngineeringLesson`-shaped table for "the founder's own recurring
  phrasing" — see §5 for why the SAME lesson mechanism covers this too.

---

## 2. Executive "look around the problem" reasoning

### 2.1 The problem this section solves

The founder wants: for every meaningful instruction, MainAI automatically inspects adjacent
components, dependencies, prior related decisions, prior bugs, contradictions, security/
authority/data-model/API/recovery/testing/observability impact, and future-maintenance
implications — then produces well-justified follow-up work, not just the literal ask.

### 2.2 Composition, not a new engine

This is an orchestration of THREE existing primitives, in sequence, with ONE new output shape:

```
instruction arrives (a MainAIGoal is about to be created, or a task is about to be planned)
  |
  v
STEP 1 — RELEVANCE SCAN (existing, unchanged)
  app.active_context.create_context_set() / refresh_context() anchored on the instruction's
  own referenced entities (resolved via §1 where the instruction contains indirect
  references). Returns a ranked member set with activation_path -- WHY each adjacent thing
  is relevant, not just that it is.
  |
  v
STEP 2 — HISTORY SCAN (existing, unchanged)
  app.mainai_execution.lessons.lookup_lessons(applies_to=<tags derived from step 1's member
  types + the instruction's own task_type/affected_component>) -- "prior bugs/misses relevant
  to this exact area", retrieved BEFORE planning, exactly as create_plan() already does this
  for verification regression tests (see §5 for the SAME retrieval extended to inform
  candidate generation, not just verification_plan injection).
  |
  v
STEP 3 — BOUNDED CANDIDATE GENERATION (new orchestration, reusing autonomous_gap's bounding
  pattern verbatim, NOT autonomous_gap's own gap-during-execution semantics -- this fires
  BEFORE execution starts, not after a verification failure)

  For each relevant-but-unaddressed item surfaced by steps 1-2 (a dependency step 1 found with
  no corresponding task in the instruction; a lesson step 2 found whose applies_to tags match
  but whose regression_test isn't already in the plan's verification_plan; a contradiction
  between the instruction and an existing active FounderMemoryNote or ProjectEntity):

  emit one WorkCandidate row (existing model, extended -- see §2.3) per item, NEVER inline
  work items the caller didn't ask about into the ORIGINAL task's own scope. Bounded by the
  SAME shape as GapGenerationBounds:

    ExecutiveScanBounds(
        max_candidates_per_scan: int = 10,   # mirrors max_gaps_per_run
        max_scan_depth: int = 2,             # active_context hop distance, mirrors
                                              # max_generation_depth
        max_elapsed_seconds: int = 60,       # this runs BEFORE any provider call or write,
                                              # must stay cheap
    )

  A scan that hits its own bound stops generating candidates and records exactly that fact
  (a WorkCandidate with priority="OPTIONAL", provenance={"scan_bound_reached": true}) rather
  than silently truncating -- same "evidence preserved even when generation stops" doctrine
  as GapOutcome("DEPTH_BOUND_REACHED", ..., problem=<preserved>).
```

### 2.3 `WorkCandidate` extension — the NOW/LATER/OPTIONAL/BLOCKED vocabulary

`WorkCandidate.priority` today is a free string defaulting `"medium"`. Extend the CLOSED
vocabulary (a migration adding a CHECK constraint, not a new column) to:

```python
EXECUTIVE_PRIORITY = frozenset({"NOW", "NEAR", "LATER", "OPTIONAL", "BLOCKED"})
# NOW    -- directly required for the instruction that triggered this scan to be complete
# NEAR   -- not required for THIS instruction, but a real gap the founder will likely ask
#           about within the current horizon (see docs/MAINAI_LONG_HORIZON_PLANNING.md)
# LATER  -- real, justified, not urgent
# OPTIONAL -- surfaced because a scan bound was reached before full confidence, or genuinely
#           low-confidence -- never silently dropped, always visible, always the lowest tier
# BLOCKED -- depends on something (another WorkCandidate, an unresolved §1 reference, a
#           founder decision) that must resolve first; `dependencies: JSONB` already exists
#           and is the right field to point at the blocker
```

Every generated candidate MUST populate the fields the founder's own spec requires verbatim —
all of which already exist on `WorkCandidate`, none new: `rationale` (why it matters),
`provenance` (source/context — this scan's own trigger + which step 1/2 finding produced it),
`classifier_confidence` (confidence), `dependencies` (dependency), `priority` (the enum
above). **No new columns required for this section** — this is a vocabulary constraint plus an
orchestration function, not a schema change beyond the CHECK constraint widening.

### 2.4 Authority boundary

`record_work_candidate()` (existing) is the ONLY write this orchestration performs. It NEVER
calls `authorize_work_candidate()` itself — turning a candidate into a real, executable goal
remains exactly as gated as it already is today (an explicit, separate, founder or
policy-authorized act). Executive-scan-generated candidates are not treated any differently
from founder-authored ones once they exist as `WorkCandidate` rows — same review queue, same
`list_unreviewed_work_candidates()`, same dismiss/authorize paths. This is what keeps §2's
entire mechanism strictly a PLANNING-side capability with zero authority implication,
consistent with `docs/MAINAI_V1_READINESS.md`'s own "LLM MAY DERIVE WORK, LLM MAY NOT DERIVE
AUTHORITY" invariant.

---

## 3. Self-correction

### 3.1 Reuse `safe_planner`'s `authority_kind` vocabulary, generalized

`FounderPlanningRequest.authority_kind` already distinguishes `AUTHORIZED_KINDS`
(`founder_requirement`/`founder_decision`/`founder_correction`/`authorized_goal`) from
`NON_AUTHORITATIVE_KINDS` (`founder_preference`/`idea`/`suggestion`/`hypothesis`/
`ai_interpretation`/`unknown`, fail-closed on unknown). This is EXACTLY the founder's own
"interpretation != authority" doctrine — lift it out of `safe_planner` (where it is currently
scoped narrowly to development-work planning requests) into a shared classification any
caller can apply to a resolved §1 reference or a §2 candidate's own confidence.

### 3.2 The actual self-correction flow

```
Founder says X. MainAI's context (memory_threads, active_context, project_entities) strongly
indicates the founder actually meant Y (a wrong term, a misspelling, a component mixup).

  IF this affects only PLANNING (research, candidate generation, draft rationale text):
    proceed using Y. Record BOTH X (raw_expression, §1.2) and Y (resolved_entity_id) on the
    SAME ConversationalInterpretationProposal row -- never silently substitute without a
    durable trace of the correction having happened.

  IF this affects a CONSEQUENTIAL EFFECT (which authorized_paths to request, which task to
  mark ready, which goal to authorize):
    surface the interpretation BEFORE the effect -- exactly matching safe_planner's own
    NEEDS_CLARIFICATION classification (assess_authority() already returns this when
    ambiguity_refs aren't in deterministic_resolutions). Reuse that same three-valued outcome
    (NEEDS_AUTHORIZATION / CONTRADICTION_UNRESOLVED / NEEDS_CLARIFICATION), don't invent a
    fourth.
```

### 3.3 Storing corrections so MainAI learns the founder's own usage

A confirmed correction (founder says "no, I meant Y") becomes a `FounderMemoryNote` with
`note_type="correction"` (already a valid value in the existing closed vocabulary — no schema
change) linked via `memory_threads` to the `ConversationalInterpretationProposal` it corrects.
Future resolution attempts (§1.3 step 3, deterministic narrowing) should query
`list_current_founder_memory(note_type="correction")` as an ADDITIONAL candidate-ranking
signal — "the founder has corrected this exact confusion before" raises that resolution's
confidence. This is genuinely new logic (a ranking heuristic), but it operates entirely on
existing tables.

---

## 4. Production examples (from the founder's own spec)

Concrete flows, each traced through the mechanisms above, matching the acceptance-example
letters from the founder's directive:

**A. "få med det här med" (include that too)** — §1.3 resolves "det här" against the current
`active_context` anchor; §1.4 promotes with `confirmed_by` = the founder's own next turn if
ambiguous, or MainAI's own resolution if unambiguous; the addition is persisted as a
`WorkCandidate` (priority NOW if it affects the in-flight goal) or a `FounderMemoryNote`
(`note_type="goal"`, if it's a standalone future intent) — never both, never neither. Per
`docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md` §2, MainAI states what it actually did in terms of
durable state, never "I've added that" as a bare conversational claim.

**B. "gör samma på den andra grejen" (do the same on the other thing)** — §1.3's deterministic
narrowing (recency + `memory_threads` continuation membership) resolves "den andra grejen"
against the PRIOR entity that was contrasted against the current one in this conversation
(requires the thread to carry both — a real, already-supported `memory_threads` capability,
since a thread can span multiple referenced objects). §2's look-around scan then runs against
the resolved entity, not the literal phrase.

**C. "hon ska kunna ligga flera hundra steg före" (she should be able to think hundreds of
steps ahead)** — applies to MainAI's own planning capability (`docs/
MAINAI_LONG_HORIZON_PLANNING.md`'s LONG horizon), explicitly not to Claude or Cursor's own
operating cadence in this repo.

**D. Founder gives the wrong component name** — §3.2's PLANNING-only branch: MainAI proceeds
using the corrected entity for research/rationale, but per §0's inherited constraint never
overwrites the canonical entity's own `content`/`title` based on the founder's mistaken naming
— only a NEW, separately-tracked correction record changes.

**E. Founder suggests something already implemented** — §2's history scan (step 2, lesson/
entity lookup) surfaces the existing implementation before any `WorkCandidate` is generated;
MainAI reports the existing state (verified via `docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md`'s
truth invariant — "already implemented" must be VERIFIED, not just planned/claimed) and, per
§2.2, still checks whether SURROUNDING coverage is incomplete (a real gap even though the core
ask isn't) rather than treating the whole instruction as a no-op.

**F. Founder adds a requirement affecting existing active work** — this is exactly
`docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md` §4 (Memory → Work Integration): the new
`FounderMemoryNote`/resolved entity triggers a scan of active goals/tasks/plans referencing
the same entity (via `memory_threads`), and affected work gets updated/superseded through the
REAL functions that already do this (`create_plan()`'s own supersession, never a direct status
write) — see that document for the full flow.

---

## 5. Missed-thing learning — extending, not duplicating, `EngineeringLesson`

The founder's MISS → ROOT_CAUSE → GENERALIZED_LESSON → APPLICABILITY → RETRIEVAL_TRIGGER →
FUTURE_PLAN_CHECK loop is, per §0, already fully built for verification-failure-sourced misses.
The only real gap: `lessons.py`'s `record_lesson()` currently has no conversational entry
point — "we forgot X" / "why didn't you think of Y" said in chat never reaches it today.

**IMPLEMENTED (2026-08-30): a new, narrow caller, not a new table.**
`app.mainai_execution.lessons.record_lesson_from_founder_correction()` takes an already-
confirmed `FounderMemoryNote` (`note_type="correction"`, caller-supplied — this function never
classifies or re-derives that) and calls `record_lesson()` (existing, unchanged) with:
- `problem` = `note.content`, quoted verbatim, never paraphrased (matches
  `founder_memory_notes.content`'s own immutability)
- `root_cause`/`fix`/`general_rule`/`applies_to`/`affected_component` = the caller's own
  required, explicit judgment — never auto-generalized from the raw correction text (this is
  what prevents overgeneralizing a one-off, matching `record_lesson()`'s own existing
  required-column shape verbatim; §2's future executive scan is the intended eventual source
  of `applies_to` tags, not built yet — the caller supplies them directly today)
- `source_type="founder_correction"`, `source_ref=str(note.id)`, `evidence="founder_memory_
  notes:{note.id}"` — full provenance chain back to the exact note
- `first_seen_at=note.observed_at` — taken from the note, not re-timestamped

Rejects (`ValueError`) if the supplied note's own `note_type` isn't `"correction"` — fails
closed rather than silently accepting a preference/goal/observation note as if it were a miss.
Tests: `tests/backend/test_mainai_lesson_from_founder_correction.py`.

`lookup_lessons()`/`apply_lessons_to_verification_plan()` (existing, unchanged) then retrieve
this exactly as they would any other lesson — proven directly (`lookup_lessons(applies_to_any=
[...])` finds the new lesson in the same test). §2's own future history scan (step 2) already
calls `lookup_lessons()`, so a conversationally-sourced lesson will be automatically included
in future executive scans with zero additional retrieval code once §2 itself is built.
`lesson_conflicts.py`'s existing contradiction detection applies unchanged, closing the
founder's own "avoid overgeneralizing" requirement for free.

---

## 6. Authority boundaries — restated, not reinvented

Every mechanism in this document is bounded by doctrines this codebase already enforces
structurally, not just documents:

- **PROPOSED_SCOPE != AUTHORIZED_SCOPE** (`execution_envelopes.service`) — nothing in §1-§5
  ever creates or widens an `ExecutionAuthorizationEnvelope`. A resolved reference, a
  generated `WorkCandidate`, a promoted lesson: all planning-side data. The ONLY function that
  ever grants real execution authority remains `authorize_execution_scope()`, called exactly
  as it is today, requiring the caller's own explicit assertion every time.
- **LLM MAY DERIVE WORK, LLM MAY NOT DERIVE AUTHORITY** (`docs/MAINAI_V1_READINESS.md`) — every
  new write path in this document (§1.4 promotion, §2.4 candidate generation, §3.3 correction
  storage, §5 lesson recording) produces DATA a human or a separately-authorized process must
  still act on to have any real effect. None of them create a `MainAIGoal`, an
  `ExecutionAuthorizationEnvelope`, or a provider-spend grant.
- **Free text is never authority** (proven empirically, #190 regression) — `raw_expression`,
  `rationale`, `root_cause`, and every other free-text field introduced or reused in this
  document inherits this: their CONTENT can never widen what MainAI is authorized to do,
  regardless of what they say.

---

## 7. Failure/recovery behavior

- A `ConversationalInterpretationProposal` that never gets promoted (founder never confirms,
  or the resolution was low-confidence and no candidate cleared the bar) simply stays
  `unreviewed` — same "harmless to accumulate, explicitly designed to hold candidates"
  doctrine `candidate_learning_signals` already establishes. No cleanup job required for V1.
- An executive scan (§2) that crashes mid-generation must leave ZERO partial `WorkCandidate`
  rows from that scan attempt — wrap the whole per-scan batch in one transaction, same
  discipline `generate_child_task_for_gap()` already uses for gap/repair child insertion.
- A resolution that turns out wrong AFTER a candidate was generated (but before authorization)
  is handled by `dismiss_work_candidate()` (existing) — the wrong candidate is dismissed, not
  deleted, durable proof it was considered and rejected, matching every other
  dismiss-not-delete pattern in this codebase.

---

## 8. V1 / V1.1 / V2 classification

Per the founder's own explicit instruction: do not make all of this a V1 blocker.

**V1 minimum required** (blocks nothing new beyond what `docs/MAINAI_V1_READINESS.md` already
tracks — these are read-only compositions of already-shipped, already-tested primitives):
- None of §1-§7 is required for MainAI V1's core execution-autonomy scope (goal → plan →
  execute → verify → finalize). V1 is about proving the EXECUTION chain is safe and complete;
  this document is about making INSTRUCTION QUALITY less founder-effort-intensive, a
  genuinely separate capability axis.

**V1.1 — important, high-value, low-risk, ship soon after V1:**
- **DONE (2026-08-30)** — §1 entity-resolution capability: migration 0064 + `resolve_
  candidate_signal_entity()`, extending `CandidateLearningSignal` rather than the
  `ConversationalInterpretationProposal` table originally sketched here (§1.2 records the
  design correction). No live wiring into `app/routers/chat.py` yet — data + service layer
  only, matching this codebase's own established "foundation first" pattern (same shape as
  `founder_memory_signals` itself when it first shipped).
- **DONE (2026-08-30)** — §5 conversational lesson recording:
  `record_lesson_from_founder_correction()`, one new caller into the already-fully-built
  `EngineeringLesson` system, no schema change.
- **DONE (2026-08-30)** — §2.3's `WorkCandidate.priority` vocabulary widening: migration 0063,
  additive CHECK-constraint change.

**V2 — advanced, higher build cost, sequence after V1.1 lands and is used for a while:**
- §2's full executive look-around orchestrator (real integration work across three
  subsystems, needs its own bounded-generation tuning pass before it's trustworthy at scale).
- §3's generalized `authority_kind` lift out of `safe_planner` into a shared classifier (a
  real refactor, not just a new caller — touches an existing, working module).
- §4's production examples become real, automated acceptance tests only once §1-§3 exist to
  test against.

**Cross-cutting, required now regardless of tier:** the memory-truth invariant
(`docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md`) is called out by the founder as a core
architectural invariant to design NOW, not deferred to V1.1/V2 — see that document.
