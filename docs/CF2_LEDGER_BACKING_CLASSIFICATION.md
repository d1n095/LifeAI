# CF-2 ledger backing classification

Isolated correction child of `#305` @ `50ca651de9648afaea31273521bba01d82aeace7`.
Do **not** amend frozen `#303`, unfrozen `#304`, or documented `#305`.

## Cause

`persist_delivery` routes through `persist_owner_ledger` (CF-1). When the owner
already has a real `AgentWorkAssignment`, `ensure_owner_ledger_backing` reuses
`rows[0]` — correct, and required by GH-4b. `persist_owner_ledger` then wrote
`ledger_backing=True` onto that row's provenance and dump. `_is_backing` treated
the flag as synthetic backing. A legitimate work assignment was therefore
classified as the bookkeeping row.

Worker reconcile still skips only `created_without_manual_assignment`, so Claude
did not have to see completion-event loss. The invariant is still violated.

## Fix

- `_is_backing` is `created_without_manual_assignment` only.
- `persist_owner_ledger` stamps `ledger_backing` only on that synthetic row.
- `repair_misclassified_backing` strips the flag from real rows (delivery,
  lifecycle, and ensure all call this first).
- Cold owners (CF-1) still create a synthetic backing row and persist.
- Warm owners still reuse the existing work row as storage and never retag it.

Zod `4.6.7` and `check-npm-audit.js` are unchanged.
