# MainAI — Inspectable Memory Foundation (Stage A)

Cursor implementation of the memory-truth invariant on tip, without creating a second
canonical memory store.

**Invariant:** SAID ≠ STORED ≠ PLANNED ≠ IMPLEMENTED ≠ VERIFIED.

Design companion (Claude-owned wording on PR #197): when that PR merges, prefer its
`docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md` as the fuller narrative. This document records
what Stage A actually landed on tip.

## What Stage A reuses

| Existing table | Truth-state role |
|---|---|
| `candidate_learning_signals` | SAID (staging only) |
| `founder_memory_notes` | STORED |
| `work_candidates` | STORED → PLANNED when authorized |
| `mainai_tasks` / checkpoints | PLANNED → IMPLEMENTED → VERIFIED (existing) |
| `engineering_lessons` | STORED; new `verification_status` for real VERIFIED |

## What Stage A adds

1. **`memory_truth_claims`** — receipts for claims MainAI makes about her own memory/work
   ("I've saved that"). `verified_result=false` is a durable, inspectable violation.
2. **`engineering_lessons.verification_status`** — `unverified` |
   `verified_by_regression_test` | `disputed`.
3. **`app.inspectable_memory`** — projection + claim/verify APIs. Never invents authority.
4. **Founder HTTP** — `/api/founder/memory` (list/get/history/add/correct/dispute + claims).
5. **Registry widen** — `candidate_learning_signal`, `work_candidate`, `project_entity` in
   active_context / memory_threads CHECKs.

## Explicit non-goals (later stages)

- Idea reconciliation graph (Stage B)
- Automatic memory→work replan (Stage C)
- Temporal recap engine (Stage D)
- Self-model ledger beyond existing capability_reality (Stage E)
- Background claim-verifier scheduler (contract §5.2) — synchronous verify is enough for A

## Migration

`0063_inspectable_memory_foundation` (`down_revision=0062`). Claude #197's pending 0063/0064
on its unmerged branch must renumber when that lane rebases.
