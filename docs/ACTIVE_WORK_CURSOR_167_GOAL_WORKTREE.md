# ACTIVE WORK — Cursor #167 Supervisor goal-worktree ownership

**Owner:** Cursor  
**Branch / PR:** `cursor/composed-autonomy-milestone` / [#167](https://github.com/d1n095/LifeAI/pull/167)  
**Started:** 2026-08-27  

## Claimed now

| Surface | Purpose |
|---|---|
| `development_operator/service.py` | Structural Supervisor goal-worktree write auth via lease + canonical path/branch |
| `development_supervisor/production_entry.py` | Stop stamping `MainAITaskWorktree` / `.mainai_worktree_owner.json` onto shared goal WT |
| `development_supervisor/production_worktree.py` | Doc: write auth is lease-bound, not per-job marker |
| Composed milestone + ownership tests | Negative control + two-task same-goal continuation |

## Locked / do not touch

- Claude Life Vault / egress lane (`docs/CLAUDE_LIFE_VAULT_EGRESS_LANE.md`)
- `remote_write_authorized` remains false
- Do **not** merge #167 until ownership model is proven green on the new head

## Standing rule

PER-GOAL Supervisor worktree ≠ PER-JOB recovery worktree. Never mix their ownership identities.
