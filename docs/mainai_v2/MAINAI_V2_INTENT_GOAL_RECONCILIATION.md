# MainAI V2 — Intent / Goal Architecture Reconciliation

Founder-triggered reconciliation (2026-09-06): the Operating Shell round (V2-I3) built a new
`app.operating_shell.IntentObject` without first checking whether this codebase already had a
durable intent/goal model. It does. This document is the deep audit and the canonical
architecture decision — written before any code changes, per this whole V2 lane's own
discipline of not re-deriving-by-guessing what a direct code read can confirm.

## 0. What already exists — confirmed by direct code reading, not assumed

Three real, production, DB-backed, RLS-scoped systems already exist, and they are NOT one
pipeline duplicated three times — they are three genuinely different concepts that compose:

### `MainAIGoal` (`app.models.mainai_execution`, migration 0032) — the sole execution-authority-bearing entity

One durable, owner-scoped MainAI execution run: `title`, `original_instruction` (the actual
raw text — the closest existing field to "raw user expression," but scoped to *this one run*,
not to a durable intent), `status` (pending/planning/running/waiting/blocked/failed/completed/
cancelled — `ACTIVE_MAINAI_GOAL_STATUSES`/`TERMINAL_MAINAI_GOAL_STATUSES` already closed sets),
`current_plan_version` (→ `MainAIPlan`, only one `active` per goal, service-enforced, replan
creates a new versioned row, never mutates in place), `risk_level`, `approval_policy` (a named
policy from a registry, never inline logic as data). Tasks (`MainAITask`) hang off plans with
their own richer status vocabulary and a full append-only `MainAITaskEvent` history. **This is
the only place real execution authority lives in this whole area of the codebase.**

### `WorkCandidate` (`app.models.work_candidate`, migration 0055) — document-derived candidate work

"A claim that a piece of structured project understanding (`ProjectEntity`) MIGHT be worth
turning into real, governed MainAI work — never a claim that it is authorized" (the model's own
docstring). `source_entity_id` is owner-anchored via a composite FK into `project_entities`.
The ONLY path to a real `MainAIGoal` is `app.work_candidates.service.authorize_work_candidate()`
— `record_work_candidate()` never writes to `mainai_goals` (proven by an existing test:
`test_record_work_candidate_never_writes_to_mainai_goals`). This pipeline's source is
**document/project understanding**, never live conversation.

### `LifeIntent` (`app.models.life_intent`, migration 0041) — durable, structured life-goal tracking

"The STRUCTURED, actionable tracking entity for a life goal/dream" (`docs/LIFE_FOUNDER_MEMORY.md`).
Real state machine (`state` column, closed non-actionable set:
`{blocked, waiting, future, completed, abandoned, superseded, unknown}` — confirmed in
`app.life_intents.service.evaluate_feasibility()`), a real dependency graph
(`LifeIntentDependency`, `relationship_type="requires"`) with **cycle detection and a bounded,
depth-limited feasibility walk already implemented** (`evaluate_feasibility()`), blockers
(`LifeIntentBlocker`, its own resolved/superseded/invalidated lifecycle), an append-only event
log (`LifeIntentEvent`), row-level locking (`with_for_update()`) and idempotency keys on every
mutating call. An **optional** `mainai_goal_id` link to a `MainAIGoal` — a `LifeIntent` does not
have to ever become one. Its source is `founder_memory_notes` (`note_type="goal"`) — "a
lightweight, raw, attributed statement... it may later inform the creation of a real
`LifeIntent`, but recording the statement never itself creates or mutates one"
(`docs/LIFE_FOUNDER_MEMORY.md`). **No automated note→LifeIntent promotion function exists yet**
(confirmed: `app.founder_memory_signals.promote_candidate_signal()` only ever produces a
`FounderMemoryNote`, never a `LifeIntent`) — this is a genuine, pre-existing gap in the
codebase, not something this reconciliation introduces or is responsible for closing.

### The real, composed hierarchy (two independent upstream sources, one downstream authority)

```
document / project understanding                 live conversation (personal/life goal)
        |                                                    |
   ProjectEntity                                   founder_memory_notes(note_type="goal")
        |                                                    |
   WorkCandidate                          [NOT YET AUTOMATED -- genuine existing gap]
        |                                                    |
authorize_work_candidate()                              LifeIntent  <-- real state machine,
        |                                            blockers, dependency graph,
        v                                            feasibility evaluation, event log
    MainAIGoal  <---------------------- (optional) mainai_goal_id ------------|
   (sole execution
    authority)
```

`LifeIntent` and `WorkCandidate` do **not** compete with each other — they are different
upstream sources that can both eventually reference the same downstream `MainAIGoal`. Neither
duplicates the other. The actual conflict is that `app.operating_shell.IntentObject` was built
as if none of this existed.

## 1. The decision

**`IntentObject` is re-scoped to be the workspace-session-local, ephemeral, orb-conversational
staging representation — never a fourth independent durable "goal truth" store.** This is
Option C (a clearly distinct responsibility) composed with Option A (a read-through projection
once a canonical link exists) from the founder's own menu — never Option B (full absorption),
because `IntentObject`'s actual job (workspace linkage, referring-expression resolution,
orb-facing "what are we doing" continuity) has no equivalent in `LifeIntent`/`MainAIGoal` today
and should not be forced into their shape.

Concretely:

1. **`IntentObject` gains a canonical-linkage field**: `canonical_kind` (`NONE` / `LIFE_INTENT`
   / `MAINAI_GOAL`, closed enum) + `canonical_ref: uuid.UUID | None`. A fresh `IntentObject`
   (`CAPTURED`/`UNDERSTANDING`) has `canonical_kind=NONE` — pure workspace scratch state,
   exactly matching `CandidateLearningSignal`'s pre-promotion role relative to
   `FounderMemoryNote`.

2. **Structural rule, not just documented**: `advance_to_active()` (and any transition into
   `ACTIVE`/`WAITING`/`BLOCKED` — the "this is real ongoing work" states) requires a real
   `canonical_ref` already set. `PLANNED` may still be reached with `canonical_kind=NONE`
   (planning != authority, matching `WorkCandidate`'s own "record != authorize" split) — but an
   `IntentObject` can never assert for itself that something is actively underway; it can only
   ever *reflect* a canonical row that an already-existing, already-authorized service call
   produced.

3. **Read-only projection functions**, real DB-touching code, added to `app.operating_shell`
   (this is expected, real integration code per this round's own instructions — not a violation
   of the "five sibling V2 packages stay mutually independent" discipline, which only ever
   applied to `guardian`/`privacy_boundary`/`sentinel`/`sovereign_identity`/`life_recovery`
   toward each other, never to real pre-existing production code):
   `project_from_life_intent(db, *, owner_id, life_intent_id) -> IntentObject` and
   `project_from_mainai_goal(db, *, owner_id, goal_id) -> IntentObject`, with a real, closed,
   tested state-mapping table (`LifeIntent.state`/`MainAIGoalStatus` → `IntentState`), fail-closed
   default (`"unknown"` → `CAPTURED`, never guessed as `ACTIVE`).

4. **`refresh_from_canonical()`, not local mutation, is the only way a canonically-linked
   `IntentObject` changes state.** Once `canonical_ref` is set, `advance_to_active()`/
   `mark_blocked()`/etc. must reject being called directly — the canonical row is always
   re-read and always wins. **OLD GOAL != CURRENT GOAL**: if the underlying `LifeIntent`/
   `MainAIGoal` has itself moved to a terminal or superseded state, `refresh_from_canonical()`
   must reflect that locally too — a stale `IntentObject` can never resurrect authority the
   canonical row no longer has.

5. **Concurrency stays where it already is.** `LifeIntent`/`WorkCandidate`/`MainAIGoal`'s own
   services already use `with_for_update()` row locks and idempotency keys (confirmed by direct
   reading, §0). The projection layer is read-only and must never itself need locking — its job
   is to never race against or bypass the existing protected surface, not to re-implement
   concurrency safety that already exists. New adversarial tests target the EXISTING canonical
   services directly (two real sessions, real Postgres) to verify they already hold under the
   founder's listed race scenarios — and the reconciliation only adds new production logic if a
   real gap is actually found, not speculatively.

6. **Context resolver naming collision resolved by renaming, not by ambiguous coexistence.**
   `app.operating_shell.context` (referring-expression resolution: "this"/"that"/"continue") is
   renamed to `app.operating_shell.reference_resolution`, and its public function
   `resolve_reference` → `resolve_workspace_reference`. The pre-existing `app.context.resolver`
   (chat-turn intent-TYPE classification: `INTENT_EXPLICIT_MEMORY`/etc.) is untouched — it does a
   genuinely different job and was never wrong, only confusingly named next to the new one.

7. **Personal Recall compatibility contract (interface only, not implemented — that is Codex's
   separate track).** Personal Recall may read: intent/goal/work history, supersession
   decisions, workspace references — via the existing/future read query functions on the
   canonical services (`evaluate_feasibility`, `list_actionable`, `list_work_candidates`, a
   future `list_life_intent_history`). **RECALL RESULT != CANONICAL GOAL STATE**: Personal
   Recall is a historical-narrative reader, never an alternate source for "is this goal
   currently active." No code from any Codex branch is imported anywhere in this reconciliation.

## 2. What this reconciliation does NOT do

- Does not build the missing `founder_memory_notes` → `LifeIntent` automated promotion path —
  real, pre-existing gap, out of this round's scope (flagged, not fixed, matching this whole
  V2 lane's discipline of not silently expanding scope into adjacent found gaps).
- Does not modify `LifeIntent`/`WorkCandidate`/`MainAIGoal`'s own model or service code —
  only reads them.
- Does not wire `app.operating_shell` into `app.main`, chat, the executive loop, or any router.
- Does not touch PR #245 / SHA `818dfb7`.
