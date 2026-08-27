# ACTIVE WORK — Cursor goal finalize canonical wire

**Owner:** Cursor  
**Branch:** `cursor/goal-finalize-canonical-wire`  
**Base tip:** `4261787` (#167 merged)  
**Started:** 2026-08-27  

## Why

Two-task composed #167 proved Task A+B can complete while MainAIGoal stays `running`.

Root cause: `_finalize_task_outcome` recomputed readiness but never called canonical
`record_final_report`. Worker `_finalize_mainai_execution_goals` exists, but
Supervisor/Driver completion path skipped that boundary; composed tests that only called
`_advance_authorized_supervisor_goals` never hit it either.

## Fix

Reuse B5 chain inside the completion gate:

`_finalize_task_outcome` → `recompute_task_readiness` → `record_final_report`

No new finalizer. No direct `goal.status = completed`. Worker finalize tick remains
crash/retry reconciler (+ post-Supervisor scan in `run_once`).

## Locked

- Claude Vault/egress lane
- Do not reopen #167 PER-GOAL worktree design
