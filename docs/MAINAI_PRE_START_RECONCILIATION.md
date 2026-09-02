# Pre-start merge reconciliation — composed safety candidate

**DO NOT MERGE #237 / #240 / #243 independently until this candidate is certified.**

## Canonical decisions

| Concern | Canonical | Rejected duplicate |
|---|---|---|
| Authority SoT + grant/stop race | `#243` `workforce_authority_epoch` + `FOR SHARE`/`FOR UPDATE` | `#237` `mainai_stop_state` as second SoT |
| Migration 0069 | `#243` `0069_workforce_authority_epoch.py` | `#237` `0069_mainai_stop_controls.py` (removed) |
| Readiness engine | `#237` `receipts.py` (IMPORTABLE≠HEALTHY, migration→BLOCKED) | `#240` monolithic `__init__.py` |
| Readiness reporting fixes | `#240` `activation_commit` + `department_evidence` + blocker-extend tests | pre-#240 hardcoded None / one-success |
| Memory→work | `#238` TOCTOU advisory lock + `#237` replay labeling | either alone |
| Composed boot clear | `#237` policy: never auto-clear; assert + `record_boot_blocked` | `#239`/`#243` stranded clear-on-boot |

## Migration chain

```
… → 0068_workforce_ops → 0069_workforce_authority_epoch → (head)
```

Single Alembic head required. No `0070` yet (stop-events audit deferred; boot identity uses `query_stop_status` over epoch).

## Lock / grant / clear / observability

- **Table:** `workforce_authority_epoch`
- **Grant:** `assert_grant_allowed` → FOR SHARE GLOBAL then owner; broker calls once immediately before insert
- **Owner stop:** `activate_kill_switch` / `activate_owner_stop` → FOR UPDATE owner
- **Global stop:** `activate_global_kill_switch` / `activate_global_emergency_stop` → FOR UPDATE GLOBAL
- **Clear:** `clear_*` require `founder_ack:`/`operator_ack:` + denylist (no fabricated boot acks); FOR UPDATE
- **Observability:** `query_stop_status(db)` over epoch (not process cache)

## Recommended merge order (after certification)

1. `#238` (independent memory-work)
2. `#240` (readiness reporting; no migration)
3. `#239` then `#243` (owner scope → authority epoch)
4. Reconciled `#237` product lane **or this composed PR** — not raw `#237` as-is

## Status vocabulary

This branch is the **FINAL CANDIDATE** for Claude’s single-SHA exam — not yet `READY_FOR_SAFE_INTERNAL_CERTIFIED`.
