# Life Work Candidates -- Knowledge → Goal Bridge

## Foundation scope

The second half of the closing bridge `docs/LIFE_PROJECT_ENTITIES_INTERPRETATION.md`'s own
"Explicitly deferred" section named as the next, deliberately separate step: turning structured
project understanding (`project_entities`, migration 0054) into real, governed MainAI work
(`MainAIGoal`), without collapsing "a good inference exists" into "execution is authorized".

## Principle: DERIVED WORK CANDIDATE != AUTHORIZED WORK != EXECUTABLE WORK

The founder's own closing-phase directive named this distinction explicitly. This foundation
enforces it structurally, the same SIGNAL PRODUCER != TRUTH WRITER shape every prior
foundation in this mission used, one level further down the chain:

```
ProjectEntity (structured understanding, already trusted -- migration 0054)
  -> record_work_candidate()      -- candidate signal, NEVER authorized
  -> work_candidates                -- staging table
  -> authorize_work_candidate()     -- the ONLY path to real work, ALWAYS requires the
                                        caller's own explicit authorized_by
  -> create_goal()                  -- the EXISTING, already-governed MainAIGoal entry point
                                        (app/mainai_execution/planner.py) -- NOT reimplemented,
                                        NOT duplicated
  -> MainAIGoal                     -- real, executable work
```

`authorize_work_candidate()` does not construct a `MainAIGoal` row itself and does not
duplicate `create_goal()`'s own approval-policy/risk-level semantics -- it calls that exact
function, the same one `app/routers/mainai_execution.py`'s `Depends(require_founder)`-gated
route already uses. This module only decides WHETHER to call it and WITH WHAT ARGUMENTS,
never WHETHER THE CALLER IS ALLOWED TO -- authorization enforcement remains entirely the
caller's responsibility, exactly as `create_goal()` itself already documents. A work candidate
can never bypass whatever authorization boundary wraps real goal creation elsewhere.

## Live wiring

`app/project_entities/service.py`'s `promote_interpretation_proposal()` (already live, part of
migration 0054) now records a candidate work candidate whenever the newly-promoted entity's
`entity_type` is `idea`, `decision`, or `task_reference` -- the exact subset
`docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`'s own §4.2 note ("task_reference-typen länkar
till en BEFINTLIG Task-rad") already identified as potentially actionable, as opposed to a
plain recorded fact (`vision_statement`, `open_question`).

Uses a SAVEPOINT (`db.begin_nested()`), not a top-level commit/rollback -- unlike the
claims.py/chat.py call sites one level up the chain, `promote_interpretation_proposal()`
itself never commits (leaves that to its own caller, matching `promote_candidate_signal()`'s
own contract), so a plain `db.commit()`/`db.rollback()` here would either surprise-commit the
caller's still-open transaction or, on failure, roll back the entity/proposal promotion this
is supposed to be a side effect OF. A SAVEPOINT failure rolls back only this nested unit of
work, matching `app/rag/memory_source.py`'s own established SAVEPOINT precedent. Proven by
`tests/backend/mainai/test_project_entity_work_candidate_capture.py`, including the
non-fatal/isolated-failure guarantee.

## Schema (migration 0055)

`work_candidates`: `source_entity_id` (NOT NULL FK to `project_entities`, `ON DELETE CASCADE`
-- unlike `project_entities.derived_from_claim_id`'s RESTRICT, a work candidate is a lighter
derived signal, not itself a base for further derivation, so it is fine for it to disappear
alongside its source), `title`/`rationale`/`dependencies` (jsonb array)/`priority`
(`low`/`medium`/`high`/`urgent`), `status` (`unreviewed`/`authorized`/`dismissed`/
`superseded`), `authorized_goal_id` (bare FK to `mainai_goals(id)`, matching
`mainai_plans.goal_id`'s own existing precedent for this table family -- `mainai_goals` has no
`unique(id, owner_id)` constraint to anchor a composite FK against either), `classifier_
strategy`/`classifier_confidence` (carries the source entity's own `confidence`, never
silently promoted to authorization).

RLS `ENABLE`+`FORCE`+owner-isolation policy, `mainai_app` privilege narrowing (`DELETE`
revoked in `app/rls.py`'s `apply_rls()`, matching every prior foundation), one `SECURITY
DEFINER` erasure function (`erase_own_work_candidates_children()`) wired into
`app/account/erasure.py` -- called explicitly for the same clarity every other foundation's
erasure call has, even though `source_entity_id`'s own `ON DELETE CASCADE` from
`project_entities` would also remove these rows once that foundation's own erasure runs.
Behavioral RLS proven in `tests/security/test_rls_isolation_work_candidates.py`.

## Post-merge hardening (migration 0056)

A founder adversarial review of migration 0054 found an owner-anchoring defect (see
`docs/LIFE_PROJECT_ENTITIES_INTERPRETATION.md`'s own "Post-merge hardening" section for the
full writeup). Applying the SAME check to this foundation's own `source_entity_id` -- a
caller-supplied parameter to `record_work_candidate()`, structurally identical to the ones
already fixed -- found it had the same bare-FK gap. Fixed in the same migration 0056:
`source_entity_id` is now owner-anchored via a composite FK to `project_entities(id,
owner_id)`, plus fail-closed validation in `record_work_candidate()` itself.

`authorized_goal_id` was deliberately left as a bare FK, not an oversight: unlike
`source_entity_id`, this column is never caller-supplied -- `authorize_work_candidate()` sets
it only from a `MainAIGoal` row that SAME function call just created via `create_goal(db,
owner_id=owner_id, ...)`, with the identical `owner_id` already in scope, so it cannot
structurally diverge from the work candidate's own owner. `mainai_goals` also has no
`UNIQUE(id, owner_id)` constraint to anchor against, and adding one would touch a much older,
more central, more actively-used table for a reference that carries no actual cross-owner
risk.

## Explicitly deferred (not built in this migration)

- **No automatic authorization** -- `authorize_work_candidate()` always requires an explicit,
  caller-supplied `authorized_by`. A future review UI/API route is possible; it would call
  `authorize_work_candidate()`/`dismiss_work_candidate()` exactly as a programmatic caller
  would, gated by whatever `require_founder`-equivalent dependency wraps it, never bypass them.
- **No dependency-graph resolution** -- `dependencies` is a plain jsonb array today, not a
  validated reference to other `work_candidates` rows with cycle detection. A future
  refinement is possible; not silently assumed unnecessary, just smaller-scope for this
  foundation.
- **No priority-ranking algorithm** -- `priority` is caller-set, not computed. This foundation
  records facts, it does not rank them, matching `docs/LIFE_CAPABILITY_REALITY.md`'s own
  "records facts, does not rank them" precedent for the same class of deferral.

## What this does NOT close

The `AgentTask` ↔ `MainAITask` dual-plane gap Cursor's own handoff (`docs/CURSOR_ADVERSARIAL_
RUNTIME_LANE_HANDOFF.md` §H.4) identified remains open -- `AgentTask` has no `owner_id`
column at all and no link to `MainAITask`, confirmed by direct inspection of
`app/models/agent_task.py`. Bridging the two is a genuine product/architecture decision
(which plane wins, or how they merge) requiring explicit founder input, not something this
foundation attempts to silently resolve by routing `work_candidates` through one or the other.
`authorize_work_candidate()` deliberately targets `MainAIGoal`/`create_goal()` -- the newer,
formal, governed-execution plane this mission's own #83-#90 chain already builds on -- not
`AgentTask`.

The Supervisor production-entry gap (`eligible MainAI work` → no production caller →
`run_supervisor()`) also remains open -- `app/development_supervisor/service.py` is inside
this mission's own hard boundary (never touched), confirmed unchanged by direct inspection.
