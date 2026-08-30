# MAINAI Memory → Work Linkage (Stage C)

**Branch:** `cursor/mainai-memory-work-linkage`  
**Depends on:** Stage B (`cursor/mainai-concept-reconciliation` / #210)  
**Do not touch:** Claude #197

## Purpose

Production path from founder memory into inspectable work linkage:

```
new/corrected founder memory
  → identify affected existing work
  → reconcile against current plans/tasks/goals
  → update/link/supersede appropriate work
  → create subordinate work ONLY when justified + authorized
```

## Hard boundaries

- **Memory ≠ authority.** Recording or linking a note never authorizes execution.
- **No duplicate task creation** from differently worded repeats (SAME-collapse on
  Stage-C parked work candidates).
- **No authority widening.** Subordinate inserts require `AUTHORIZED_KINDS` +
  `authorized_instruction_sha256` + live goal/plan via `insert_plan_tasks`.
- **History preserved.** Superseded work candidates remain queryable (`status=superseded`).
- Package must not import `authorize_work_candidate`, `create_plan`, replan,
  execution envelopes, or the development driver (`assert_no_forbidden_imports`).

## API surface

`app.memory_work_linkage.apply_memory_work_linkage(...)`

Key knobs:

| Param | Effect |
|---|---|
| `timing=now\|later` | LATER parks only; never inserts tasks |
| `is_correction` | Marks CORRECTION impact; can supersede prior candidates |
| `supersede_candidate_ids` | Explicit supersede (inspectable, non-delete) |
| `contradict_entity_id` | Flags `contradicts` relationship + thread membership |
| `insert_subordinate` | Opt-in; requires founder authority kinds |

## Test evidence

`backend/tests/backend/mainai/test_memory_work_linkage.py` — six required scenarios +
authority gate + replay idempotency.
