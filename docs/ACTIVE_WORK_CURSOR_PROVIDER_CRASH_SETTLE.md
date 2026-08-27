# ACTIVE WORK — Cursor provider crash-before-settle

**Owner:** Cursor  
**Branch:** `cursor/provider-crash-before-settle`  
**Base tip:** `8b2057d`  
**Depends:** merge #181 first preferred (independent otherwise)

## Claimed

| Fix | Proof |
|---|---|
| Unresolved `reserved` source_ref → settle conservatively, **never re-invoke** | `test_crash_after_reserve_before_settle_refuses_second_provider_invoke` |
| Released source_ref cannot be resurrected as free hold | reserve raises; planning allocates `:aN` |
| Settle twice idempotent | `test_settle_twice_is_idempotent_single_spend` |

## Negative control

Crash/re-invoke test **FAILED** on pre-fix tip (second adapter call). Passes with this branch.
