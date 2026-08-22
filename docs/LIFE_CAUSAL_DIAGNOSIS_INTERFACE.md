# LIFE CAUSAL DIAGNOSIS INTERFACE

## Foundation scope

This document defines the foundation introduced by migration 0050: a durable way for Life to
distinguish, using evidence rather than hard-coded labels, WHY a step failed -- and to never
conflate an unproven hypothesis with a proven cause. It answers mission item 6 of "LIFE
SELF-MODEL, ADAPTIVE COGNITION & CORPUS READINESS." A failed step does NOT automatically mean
the code change is bad: PR tests green + a transient external HTTP 503 during merge is an
external/transient blocker candidate, not evidence of a code regression -- this foundation is
what lets that distinction be recorded and queried, not just reasoned about in the moment and
forgotten.

## Relationship to other classification concepts -- not a duplicate of any of them

- `EngineeringLesson.root_cause` (migration 0032) -- a single free-text field, written only
  AFTER a lesson is fully understood. No closed causal-category vocabulary, no concept of an
  intermediate, unproven hypothesis. `diagnosis_records` is what happens BEFORE a lesson is
  ready to be written -- a diagnosis may later inform an `EngineeringLesson`'s own `root_cause`
  text, but this migration never writes to that table, and an `EngineeringLesson` is never
  required to have a `DiagnosisRecord` behind it.
- `RecoveryClassification` (migration 0033) -- a DIFFERENT, narrower taxonomy answering "how
  much of a dead agent's own work was salvageable" (nothing_done / checkpointed_work / ... /
  verified_work / unsafe_to_auto_recover). Not a general failure-cause taxonomy at all --
  genuinely orthogonal to this foundation.
- `app.capability_reality`/`app.founder_memory` (migrations 0048/0049) -- this foundation
  follows the SAME structural pattern (a mutable row, explicit caller-supplied classification,
  self-referential supersession, migration 0042's authority/basis vocabularies reused
  verbatim) but answers a genuinely different question: not "what can Life do" or "what does
  Life know about the founder," but "why did this specific thing happen."

## Principles

- Never infer `hypothesis_category` -- every call to `record_diagnosis()` is an explicit
  caller assertion. A test failure or an error message is never automatically classified as a
  code regression by this module itself.
- `observation`, `hypothesis`, `proven_cause`, and `ruled_out` are genuinely distinct
  epistemic stages. Moving from `hypothesis` to `proven_cause` requires a real evidence
  reference (`intelligence_evidence`, migration 0038) -- enforced by a DB CHECK constraint,
  not just caller discipline. A caller cannot mark a diagnosis `proven_cause` on a guess; the
  database itself refuses the row.
- `hypothesis_category`'s nine values (`code_regression | concurrency_timing | stale_state |
  environment_configuration | external_service_failure | dependency_failure |
  authorization_blocker | missing_capability | unknown`) are bootstrap examples, not a
  permanent taxonomy -- extending the CHECK constraint with a tenth value later is a small,
  additive migration, never a redesign.
- `observation` is never rewritten in place. A later re-investigation that changes the
  conclusion always creates a NEW row (`supersedes_diagnosis_id`) -- both the original
  observation and the corrected diagnosis remain durably queryable.
- `authority`/`basis` reuse migration 0042's exact vocabularies, verbatim -- the third reuse
  of this vocabulary in this codebase (after `life_problems`/`life_problem_decisions`
  themselves and `founder_memory_notes`), never a fourth competing taxonomy.

## Existing systems reused

`intelligence_evidence` (migration 0038) is the ONLY thing that can ground a `proven_cause`
transition -- a nullable `(id, owner_id)` composite FK, `ON DELETE SET NULL`, never a copy of
evidence content. `app.active_context.service`'s central object-reference registry is extended
with exactly one new entry, `diagnosis_record` -- the same mechanism already extended once for
`founder_memory_note`, letting a diagnosis be linked (via `memory_threads`) to the task, PR, or
problem it's actually about without a dedicated FK column for every possible subject type.

## Durable records

`diagnosis_records` -- one owner-scoped row per diagnosis: `observation` (the raw, factual
thing that was actually seen, immutable), `hypothesis_category`, `hypothesis_reasoning`,
`epistemic_stage` (`observed | hypothesis | proven_cause | ruled_out`), `confidence` (0-1,
nullable), `authority`, `basis`, `proven_evidence_id` (required when `epistemic_stage=
'proven_cause'`, CHECK-enforced), `supersedes_diagnosis_id` (self-FK, no cycles),
`idempotency_key`. Privilege-narrowed the same way as `founder_memory_notes`: `mainai_app`
holds exactly `SELECT, INSERT, UPDATE`; deletion only through
`erase_own_diagnosis_children()`.

`app.diagnosis.service`:
- `record_diagnosis()` -- the one write path for a new observation/hypothesis/correction.
  Idempotent by construction.
- `prove_diagnosis_cause()` -- the ONLY path that transitions a diagnosis to `proven_cause`;
  always requires a real evidence reference.
- `rule_out_diagnosis()` -- marks a hypothesis rejected without deleting it, so a future
  diagnosis pass does not silently re-propose the same ruled-out cause.
- `get_diagnosis()` / `list_diagnoses()` / `list_unresolved_diagnoses()` -- read paths.
  `list_unresolved_diagnoses()` answers "what don't we know the cause of yet" -- the
  complement of `proven_cause`/`ruled_out`.

## Explicitly deferred

Not built in this bootstrap increment -- current limitations, not permanent prohibitions. See
"Protected vs. current-scope" below for the distinction and why it matters.

- Automatic classification of a failure into a `hypothesis_category` (reading a stack trace or
  CI log and proposing a cause) -- not built now. A future classifier remains possible PROVIDED
  it writes with `authority` honestly reflecting that an automated process produced it (`ai_
  interpretation` or `inferred_pattern`, never `founder` or `deterministic_source`) and
  `epistemic_stage="hypothesis"` -- never `proven_cause` (already impossible regardless: the DB
  CHECK constraint refuses that transition without a real evidence reference no matter who or
  what is calling `record_diagnosis()`).
- Wiring diagnosis records automatically into `EngineeringLesson` creation -- the link is a
  future, explicit, human-or-reviewed action, never SILENT automatic promotion. A reviewed
  automated proposal (e.g. a governed process drafting a candidate lesson for human
  confirmation) is a different, safe thing from an automatic write with no review step; only
  the latter is what stays prohibited.
- A UI surface for founders to browse open diagnoses (data + service layer only, matching
  every other "foundation" layer in this codebase).
- Automatic root-cause search/correlation across multiple diagnosis records (e.g. "this same
  `external_service_failure` category has recurred five times this week") -- this module
  records facts; pattern detection across them is future, separate work, and would itself
  produce a new `hypothesis`-stage diagnosis (or a candidate signal, see docs/LIFE_FOUNDER_
  MEMORY.md's "Candidate learning signals" section for the analogous pattern), never a silent
  `proven_cause`.

## Protected vs. current-scope (not the same thing)

Permanent, at any future automation level: never fabricate a `proven_cause` without a real
evidence reference (DB-enforced); never let an automated classifier claim `authority="founder"`
or `authority="deterministic_source"` for something it merely inferred; never rewrite
`observation` in place. Everything above is a current-scope limitation inside those bounds --
"not autonomously trusted yet" is not "must never exist."
