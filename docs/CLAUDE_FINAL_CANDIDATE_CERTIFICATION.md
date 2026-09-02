# Claude final composed candidate exam

**DO NOT MERGE overlapping #237/#240/#243 independently.**

**Exact candidate SHA:** `75f2d8765d97c7a0eb8dafd98733645905718b59`  
**Branch:** `cursor/mainai-pre-start-reconciliation`

This is ONE composed tip combining independently verified fixes. Attack the composition, not the separate PRs.

## Canonical architecture (must hold)

- Authority SoT: `workforce_authority_epoch` (from #243) — NOT `mainai_stop_state`
- Migration: single head `0069_workforce_authority_epoch` ← `0068`
- Readiness: `receipts.py` (#237) + activation/department fixes (#240)
- Memory→work: #238 TOCTOU lock + self-replay id surfacing
- Boot: never auto-clear; `assert_not_killed(db, owner_id)` + `record_boot_blocked`

## Attack priorities

1. Grant vs stop races (7 scenarios + process restart) — `prove_no_reusable_live_authority`
2. No second stop SoT / dual lock protocol
3. safe_composed_run while owner/global stopped — no TypeError, no auto-clear
4. Readiness: migration unhealthy→BLOCKED; blockers accumulate; IMPORTABLE≠HEALTHY
5. Memory→work self-replay + SAME-collapse
6. Fabricated ack / stale epoch clear rejected
7. Owner/global isolation

## Required verdict vocabulary

`READY_FOR_SAFE_INTERNAL_CERTIFIED` only if composition holds on this exact SHA.
Otherwise `NOT_READY` with OPEN P0/P1.
