# Life Project Entities / Interpretation Queue (P4)

## Foundation scope

The layer `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`'s own §4.2/§6.4 describes as **P4** and
this codebase never built until migration 0054: turning a typed `KnowledgeClaim` (P3, already
live in `app/rag/claims.py`) into structured, reviewable project/founder understanding, without
silently promoting extraction output into trusted project fact.

This closes the gap independently identified by both the founder's own adversarial review of
this mission's chain and Cursor's final handoff (`docs/CURSOR_ADVERSARIAL_RUNTIME_LANE_
HANDOFF.md` §G/§L): `claims → interpretation/project_entities → justified knowledge → goal`
was completely unbuilt -- confirmed by direct source search before this migration (zero table,
model, or service existed; only a forward-reference comment in
`app/models/knowledge_claim.py`).

## Principle: SIGNAL PRODUCER != TRUTH WRITER

Exactly the same architecture migration 0053 (`candidate_learning_signals`) already
established for founder memory, applied here to project understanding:

```
KnowledgeClaim (P3, already live, itself an AI extraction heuristic)
  -> record_interpretation_proposal()   -- candidate signal, NEVER project truth
  -> interpretation_proposals            -- staging table, structurally no authority/basis columns
  -> promote_interpretation_proposal()   -- the ONLY path to real project understanding,
                                             ALWAYS requires the caller's own explicit
                                             authority/basis
  -> project_entities                    -- trusted, owner-scoped, evidence-linked knowledge
```

`interpretation_proposals` has no `authority`/`basis` columns at all -- a row here is never a
claim about the project, only a claim that "this extracted claim might be worth turning into
structured understanding." `project_entities` reuses the exact `authority`/`basis` vocabulary
migrations 0049/0050 already established (`founder`, `repeated_founder_preference`,
`deterministic_source`, `inferred_pattern`, `ai_interpretation`, `unknown` /
`manual`, `deterministic`, `imported`, `inferred`, `ai_interpretation`, `unknown`).

## Live wiring

`app/rag/claims.py`'s `extract_claims_for_document()` (already live, called from
`app/rag/library_import.py` after every successful import) now calls
`_record_interpretation_proposal_if_worth_noticing()` for every extracted claim whose
`claim_type` is `idea`, `decision`, or `task_reference` -- the exact subset
`docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`'s own P3 docstring already said P4 should route.
Wrapped in try/except with its own commit/rollback, exactly mirroring `app/routers/chat.py`'s
own candidate-signal integration: a failure here can never break claim extraction's own
result. Proven by `tests/backend/rag/test_claim_interpretation_proposal_capture.py`.

## Schema (migration 0054)

- **`interpretation_proposals`** -- staging layer. `source_claim_id` (NOT NULL FK to
  `knowledge_claims`), `proposed_entity_type`, `classifier_strategy`/`classifier_confidence`
  (carries the claim's own objective, grounding-based confidence bucket -- never the
  extracting model's self-report), `status` (`unreviewed`/`promoted`/`dismissed`),
  `promoted_to_entity_id` (composite FK to `project_entities(id, owner_id)`), idempotent by
  `(owner_id, idempotency_key)`.
- **`project_entities`** -- the trusted layer. `entity_type` (`idea`/`decision`/
  `task_reference`/`vision_statement`/`open_question`), `status` (reuses the
  `active`/`historical`/`proposed`/`superseded`/`disputed` vocabulary), `derived_from_claim_id`
  (NOT NULL, `ON DELETE RESTRICT` -- an entity can never exist without its source claim, and
  deleting that claim out from under a promoted entity is rejected, not silently orphaned),
  `authority`/`basis`/`confidence`, `decided_by`/`decided_at` (DB CHECK: only settable when
  `entity_type = 'decision'`), `supersedes_entity_id` (self-referential, same
  never-mutate-just-supersede discipline every other foundation in this mission uses).
- **`project_entity_relationships`** -- mirrors the pre-existing `claim_relationships` table
  (migration 0007) exactly: same bare (non-composite-owner-anchored) FK precedent for
  `from_entity_id`/`to_entity_id`, same `relationship_type` shape (`relates_to`/`supersedes`/
  `contradicts`/`blocks`/`answers`/`duplicates`/`derived_from`). Append-only (no `UPDATE`
  granted); cleaned up via `ON DELETE CASCADE` from `project_entities`, no independent erasure
  path needed.

All three: RLS `ENABLE`+`FORCE`+owner-isolation policy, `mainai_app` privilege narrowing
(`DELETE`/`TRUNCATE`/`REFERENCES`/`TRIGGER` revoked in `app/rls.py`'s `apply_rls()`, matching
every prior foundation), one `SECURITY DEFINER` erasure function
(`erase_own_project_entities_children()`) wired into `app/account/erasure.py`. Behavioral RLS
proven in `tests/security/test_rls_isolation_project_entities.py` (real restricted-role
session, real cross-owner insert/read attempts), matching the standing discipline
`docs/LIFE_COGNITION_FOUNDATION_REVIEW_2026-08-18.md`'s Finding 3 established.

## What "current" means

`list_current_project_entities()` plays the same safe-by-default role
`list_current_diagnoses()`/`list_current_founder_memory()` already play for their sibling
foundations: excludes `historical`/`superseded`/`disputed`.

## Explicitly deferred (not built in this migration)

Not built now -- current LIMITATIONS, not permanent architectural prohibitions. Each has a
concrete condition under which it could be safely built later without weakening any protected
invariant (see `docs/LIFE_FOUNDER_MEMORY.md`'s own "Protected vs. current-scope" section for
the pattern this doc follows).

- **No interpretation-queue UI** ("Tolkningskö" in the plan doc) -- data + service layer only,
  matching every other foundation in this mission. A future UI is possible; it would call
  `promote_interpretation_proposal()`/`dismiss_interpretation_proposal()` exactly as a
  programmatic caller would, never bypass them.
- **No embedding-based relation discovery** -- the plan doc's own "embedding-försil + riktat
  LLM-anrop bara för gränsfall" refinement for `project_entity_relationships` is future work.
  `record_entity_relationship()` exists and is fully functional for an explicit caller today;
  automatic discovery is a separate, bounded-cost addition later.
- **No automatic promotion** -- `promote_interpretation_proposal()` always requires an
  explicit, caller-supplied `authority`/`basis`. Wiring a reviewed, governed promotion trigger
  (a founder review action, or a sufficiently-evidenced automated path with its own explicit
  authority story) remains future work, not silently assumed unnecessary.
- **No `knowledge → goal` bridge yet** -- this migration builds the `claims → interpretation →
  structured knowledge` half only. Turning a `project_entities` row into a justified
  `MainAIGoal`/`MainAITask` candidate is the next, separate closing step -- deliberately not
  conflated with this one, matching this mission's own "one clear purpose per PR" discipline.

## Relationship to other foundations this mission built

Independent from, not a duplicate of: `app.founder_memory` (grundarens egna ord, never
inferred), `app.capability_reality` (runtime capability facts), `app.diagnosis` (causal
hypotheses about system behavior). `project_entities` is specifically about WHAT the project
is/decided/intends -- ideas, decisions, task references, vision, open questions -- derived
from source material, not from conversation.
