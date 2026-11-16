# LifeIntent State Machine P0 — Fix

Follow-up to `docs/mainai_v2/MAINAI_V2_INTENT_GOAL_RECONCILIATION.md`, which found that
`app.life_intents.service.transition_intent()` performs no state-machine validation at all.
This document records the fix decision.

## 0. Real existing states, confirmed by direct reading — not invented

`tests/backend/context/test_goals_dreams_dependencies.py` (migration 0041's own original
test suite) is authoritative: `test_kinds_states_provenance_and_history_are_distinct`
enumerates the complete real vocabulary — `unknown` (the column default), `future`, `active`,
`blocked`, `waiting`, `completed`, `abandoned`, `superseded`. `evaluate_feasibility()`'s own
`non_actionable` set independently confirms the same seven non-default values. No state
outside this set has ever been used anywhere in this codebase (confirmed by grep).

No router, executive-loop, or production service other than `app.life_intents.service`
itself calls `transition_intent()` — its only real caller today is that original test suite.
This significantly de-risked the fix: there was no hidden production caller depending on the
old, unvalidated behavior.

## 1. The fix

A module-level transition table, `LIFE_INTENT_TRANSITIONS`, following the exact convention
already established elsewhere in this codebase by
`app.strategy_evaluation.service.EXPERIMENT_TRANSITIONS`/`CANDIDATE_TRANSITIONS` (a plain
`{state: {allowed targets}}` dict, validated with `if to_state not in TABLE[from_state]:
raise`) — not a new pattern invented for this fix:

```python
LIFE_INTENT_TRANSITIONS = {
    "unknown":    {"future", "active", "blocked", "waiting", "completed", "abandoned", "superseded"},
    "future":     {"active", "blocked", "waiting", "completed", "abandoned", "superseded"},
    "active":     {"blocked", "waiting", "completed", "abandoned", "superseded"},
    "blocked":    {"active", "waiting", "abandoned", "superseded"},
    "waiting":    {"active", "blocked", "abandoned", "superseded"},
    "completed":  set(),
    "abandoned":  set(),
    "superseded": set(),
}
```

`completed`/`abandoned`/`superseded` are fully terminal — no exit at all. Unlike
`app.strategy_evaluation`'s own `completed`/`failed`, which retain a narrow `invalidated`
exit (a specific, product-defined correction mechanism), **no equivalent reopen operation
exists anywhere in this codebase for `LifeIntent`** — confirmed by reading every real caller.
If one is needed later, it must be its own explicitly-named, explicitly-authorized function,
never a widening of this generic gate.

`create_intent()`'s own `state` parameter is now validated against the same table (a bad
initial state was just as much a bypass of the closed vocabulary as a bad transition) —
a small, safe, directly-related extension of the same fix, not a separate feature.

**Optimistic concurrency**: `transition_intent()` gained an optional
`expected_current_state` parameter. If supplied, and the row's real current state no longer
matches it, `StaleTransitionError` is raised immediately — even when the requested
transition would otherwise be legal from the row's actual current state. This is
deliberately distinct from transition-table validation: a caller can be stale (acting on
outdated information) even when its requested edge is, in isolation, a legal one. Existing
call sites that don't pass it are unaffected — this is additive, not a breaking change to the
two real existing call sites in `test_goals_dreams_dependencies.py`.

Row-level locking (`with_for_update()`, inside the existing `_intent()` helper) already
serializes genuinely concurrent writers correctly — confirmed by the pre-existing
`test_concurrent_state_updates_serialize` (two real threads, two real sessions, one row).
That mechanism was never broken; the gap was purely the missing FROM/TO legitimacy check,
which is orthogonal to locking and is what this fix adds.

## 2. Self-attack: other direct `LifeIntent.state` writes

Confirmed by grep across the whole backend: the only direct `LifeIntent.state = ` assignment
anywhere in this codebase is the one inside `transition_intent()` itself (now gated).
`app.strategy_evaluation.service`'s own `row.state = to_state` operates on a completely
different model (`StrategyExperiment`/`StrategyCandidate`), already has its own
already-validated transition tables, and was not touched.

## 3. What this fix does NOT do

- Does not touch `app.work_candidates`, `app.mainai_execution`, any router, or `app.main`.
- Does not attempt to keep `LifeIntent.state` and a linked `MainAIGoal.status` in sync —
  that remains genuine, deliberate, pre-existing schema looseness (see the reconciliation
  doc's §0), unrelated to this specific state-machine gap.
- Does not touch PR #245 / SHA `818dfb7`.
