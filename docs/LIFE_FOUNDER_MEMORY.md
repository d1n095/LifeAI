# LIFE FOUNDER/USER MEMORY

## Foundation scope

This document defines the foundation introduced by migration 0049: a queryable,
evidence-backed record of what Life has learned about the founder/user -- explicit decisions,
corrections, preferences, expressed goals, and observed recurring patterns -- kept structurally
separate from project facts, world/system facts, and Life's own capability-reality facts, while
remaining linkable to any of them. It answers the "Grundarminne" gap
`docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §2/§4.3 (P6) named and left designed-but-not-built,
confirmed still missing by `docs/LIFE_REQUIREMENT_TRACEABILITY.md` §8.

## Relationship to other memory/fact concepts -- not a duplicate of any of them

- `LifeProblemDecision`/`LifeProblem` (migration 0042) -- decisions made WHILE SOLVING a
  specific problem; `problem_id` is required. A founder/user memory note is never required to
  be about a "problem" (a communication-style preference isn't solving anything) -- this is why
  `founder_memory_notes` is its own table, not a reuse of `life_problem_decisions` with a
  nullable `problem_id`. The two remain linkable via `memory_threads` (see below) without either
  being forced into the other's shape.
- `LifeIntent` (migration 0041) -- the STRUCTURED, actionable tracking entity for a life
  goal/dream (state machine, blockers, dependencies, an optional `mainai_goal_id` link). A
  `founder_memory_notes` row with `note_type="goal"` is the LIGHTWEIGHT, RAW, attributed
  statement ("the founder said they want X") -- it may later inform the creation of a real
  `LifeIntent`, but recording the statement never itself creates or mutates one.
- `app.context.resolver` -- a pure, non-persisting classifier (`INTENT_EXPLICIT_MEMORY`/
  `INTENT_CORRECTION`/`INTENT_IDEA_WORTH_SAVING`) that decides what KIND of conversational turn
  a message is. It writes nothing durable. `app.founder_memory` is the durable layer a caller
  may write to once it has decided (via the resolver or otherwise) that a turn is worth
  recording -- the resolver's own "never infer emotional/psychological state" hard constraint
  is inherited here by construction (see Principles).
- `app.capability_reality` (migration 0048, the prior increment) -- what Life/the system can
  DO. Deliberately never merged with founder memory: "the founder prefers Postgres" and
  "Postgres capability is verified_available" are two different kinds of fact, in two different
  tables, linkable via the same `memory_threads` mechanism if reasoning needs both at once.

## Principles

- Never infer `authority` or `basis` -- every call to `record_founder_memory()` is an explicit
  caller assertion, the same doctrine `app.capability_reality.service` already established for
  capability facts.
- `authority`/`basis` reuse the EXACT closed vocabularies migration 0042
  (`LifeProblem`/`LifeProblemDecision`) already established -- never a second, competing
  provenance taxonomy.
- `content` is never rewritten in place. A correction always creates a NEW row
  (`supersedes_note_id`) and flips the OLD row's own `status` to `superseded` in the same call
  -- the old row's `content`/`authority`/`basis`/`note_type` are never touched. Both rows remain
  durably queryable.
- **Hard rule, structural, not just documented: no emotional or psychological state is ever
  inferred.** There is no column, no `note_type` value, and no vocabulary anywhere in this
  foundation for it -- the same "no hidden diagnosis" doctrine `app.context.resolver` already
  established and is tested for. If a future caller wants that signal, it may only ever come
  from the founder's own explicit words captured verbatim in `content` with
  `authority="founder"`/`basis="manual"`.
- "Founder preference" never silently becomes "technical requirement," and "assistant
  suggestion" never silently becomes "founder decision" -- `authority` is the ONLY axis that
  distinguishes these, always caller-supplied, never upgraded by this module.
- `UNKNOWN` is always valid and is every classification field's own default.

## Existing systems reused

- Migration 0042's `authority`/`basis` vocabularies, reused verbatim on `founder_memory_notes`.
- `app.active_context.service`'s central object-reference registry (`SUPPORTED_TYPES`,
  `_owned_row()`) -- extended with exactly one new entry, `founder_memory_note`, the SAME
  mechanism already used by `memory_threads`, `work_intelligence`, `life_intents`, and
  `problem_learning` to reference any owned entity by kind+id. This required widening the SAME
  three CHECK constraints migration 0042 last widened
  (`active_context_sets.anchor_type`/`active_context_members.object_type`/
  `memory_thread_members.member_kind`) by one value each -- the established pattern, not a new
  mechanism.
- `app.memory_threads.service.create_thread()`/`add_member()` -- the existing, real linking
  mechanism. A `founder_memory_notes` row and a `life_problem_decisions` row (or any other
  registered entity) can share a `MemoryThread` via `member_kind`+`member_ref_id`, proving
  "linked but never collapsed": each stays its own row, in its own table, with its own
  authority/status.

## Durable records

`founder_memory_notes` -- one owner-scoped fact per row: `note_type` (`decision | correction |
preference | goal | recurring_pattern | observation | unknown`), `content` (immutable once
created), `status` (`active | superseded | disputed | unknown`), `authority`, `basis`,
`confidence` (0-1, nullable -- for an inferred pattern's own strength), `source` (free-form
pointer to where this came from), `supersedes_note_id` (self-FK, never a cycle -- `CHECK
supersedes_note_id <> id`), `provenance`, `observed_at` (when THIS system recorded it),
`valid_from` (when the fact itself became true, if known -- distinct from `observed_at`,
matching `MemoryThreadMember`'s own recording-time-vs-event-time distinction), `idempotency_key`
(`UNIQUE(owner_id, idempotency_key)`, the same safe-replay pattern used throughout this
codebase's evidence-recording functions).

Privilege-narrowed the SAME way as its closest precedent, `life_problem_decisions`: `mainai_app`
holds exactly `SELECT, INSERT, UPDATE` (DELETE/TRUNCATE/REFERENCES/TRIGGER revoked) -- deletion
is only ever possible through `erase_own_founder_memory_children()`, wired into
`erase_account_data()`.

`app.founder_memory.service`:
- `record_founder_memory()` -- the one write path. Idempotent by construction; replaying the
  same key with different field values raises rather than silently picking a winner.
- `mark_founder_memory_disputed()` -- the explicit "this note's own truth is now in question"
  transition, when there is not yet a clear replacement note to supersede it with.
- `get_founder_memory()` / `list_founder_memory()` -- read paths, filterable by
  `note_type`/`status`/`authority`.

## Explicitly deferred layers

- Automatic classification of conversation turns into founder-memory notes (wiring
  `app.context.resolver`'s own intent classifications into automatic `record_founder_memory()`
  calls) -- this foundation is the durable layer a caller writes to; deciding WHEN to write
  remains a separate, not-yet-built integration.
- Promoting a `note_type="goal"` note into a real, structured `LifeIntent` -- the linking
  mechanism (`memory_threads`) exists; automatic promotion does not.
- A UI surface for founders to browse/correct their own recorded memory (data + service layer
  only, matching every other "foundation" layer in this codebase).
- Any mechanism that lets a founder preference automatically become a binding project
  requirement -- this foundation's entire point is preventing exactly that collapse; any future
  "founder preference informed this project decision" step remains an explicit, human or
  reviewed action recorded on the PROJECT side (e.g. a `LifeProblemDecision`'s own `provenance`
  referencing the founder-memory note's id), never an automatic write from one table into the
  other.
