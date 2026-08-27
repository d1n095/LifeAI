# ACTIVE WORK — Cursor stale authority at effect time

**Owner:** Cursor  
**Branch:** `cursor/stale-authority-effect-time`  
**PR:** [#177](https://github.com/d1n095/LifeAI/pull/177)  
**Head:** `bd08505`  
**Base tip:** `6a3572e` (#168 merged)  
**Started:** 2026-08-27  

## Claimed

| Surface | Purpose |
|---|---|
| `OperatorContext.execution_envelope_id` | Bind exact envelope row used when context was prepared |
| `_require_live_execution_authority` | Effect-time re-read of CURRENT active envelope; exact-id match |
| Governed empty capability ceiling | Envelope-bound / Supervisor-bound contexts with `allowed_capabilities=()` FAIL CLOSED |
| `production_entry.prepare_context` | Sets `execution_envelope_id` from live reverify |
| `test_stale_authority_effect_time.py` | Supersede/revoke before write → zero filesystem effect |

## Do not touch

- Claude Vault/egress remaining callers
- Redesign #168 finalize/replan semantics

## Invariants

1. Authorization valid at planning ≠ sufficient at Operator effect.
2. EVER_GOVERNED / envelope-bound / Supervisor-bound + empty `allowed_capabilities` → FAIL CLOSED (never legacy unrestricted).
3. Never-governed legacy contexts without envelope/Supervisor binding may keep empty=unrestricted.
