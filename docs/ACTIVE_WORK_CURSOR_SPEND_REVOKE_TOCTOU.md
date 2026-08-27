# ACTIVE WORK — Cursor spend revoke-before-reserve TOCTOU

**Owner:** Cursor  
**Branch:** `cursor/toctou-spend-revoke-before-reserve`  
**Base tip:** `77d3f1e` (#177 merged)  
**Started:** 2026-08-27  

## Claimed

| Surface | Purpose |
|---|---|
| `_live_provider_spend_authorized` | Re-read live grant before `plan_with_provider` |
| `test_spend_revoke_before_reserve.py` | Revoke after eligibility → zero adapter calls |

## Do not touch

- Claude Vault remaining callers
- #168 finalize semantics / #177 envelope effect-time (already landed)
