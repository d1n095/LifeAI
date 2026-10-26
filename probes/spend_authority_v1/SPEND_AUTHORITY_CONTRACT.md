# Spend-authority boundary acceptance — provider spend cannot exceed a Founder grant

**Property:** external-provider execution, retries, follow-ups and continuation cannot exceed a valid
Founder spending grant. **Candidate:** #309 `aff5de1a`. **Suite:** `probes/spend_authority_v1` —
**11 passed, 1 xfailed(strict)**. Drives the real `app.provider_spend.service` reservation ledger
(`authorize` / `reserve` / `settle` / `release` / `revoke`) + execution-envelope + goal, as the
product's own tests build it. Read-only; egress-guarded (no live/paid calls). Codex's Security Kernel
NOT modified. `first_cycle_go=false`.

## Existing controls — enforced today (PASS, executable negative tests)
| ID | Boundary | Mechanism |
|---|---|---|
| A1 | negative ceilings rejected at grant creation | `authorize_provider_spend` validation |
| B1 | no live grant → reservation refused | `get_current_provider_spend_authorization` None → error |
| B2 | request ceiling (`max_requests`) enforced | reserve raises "request ceiling exhausted" |
| B3 | cost ceiling (`max_cost_usd`) enforced | reserve raises "cost ceiling exhausted" |
| B4 | provider/model allowlist enforced | `_provider_allowed` → error on non-allowlisted |
| C1 | revoked grant refuses continuation | `revoke_provider_spend` → status revoked → not current |
| C2 | expired grant refuses (naive-UTC `expires_at`) | `_mark_terminal_if_needed` → expired |
| D1 | duplicate `source_ref` is idempotent, no double-hold | unique `(owner, source_ref)` + idempotent return |
| D2 | a released `source_ref` cannot be resurrected (fail closed) | append-only usage rows |
| E1 | idempotency-key reuse with HIGHER ceilings rejected (no authority widening) | authority fingerprint compare |
| F1 | settlement records ACTUAL cost and frees the over-hold | `settle_provider_spend_call` |

**Concurrent reservations** are covered by D1's invariant: `reserve_provider_spend_call` inserts with
`ON CONFLICT DO NOTHING` on `(owner, source_ref)` and serializes budget via the authorization row lock,
so a concurrent twin returns `created=False` and never double-holds (the losers cannot invoke a paid
call). **Unavailable authority** is B1 (no live grant → fail closed, never an implicit allow).
**Provider-forged approval** is E1 (a replayed grant key with widened authority is rejected by the
fingerprint; a provider cannot mint or widen a grant).

## Required defense-in-depth not yet implemented (xfail strict)
- **SPEND-STOP (G1):** `reserve_provider_spend_call` / `get_current_provider_spend_authorization` do
  NOT consult the kill switch, so a reservation can be taken while a Founder STOP is active. Contained
  today because `provider_worker.assert_not_killed` gates EXECUTION upstream (and `activate_kill_switch`
  revokes WorkforceAssignments, not spend grants), so no paid call runs under STOP via the worker path.
  But defense-in-depth requires the spend layer itself to fail closed under STOP — otherwise a future
  caller that reserves+invokes without the worker gate could spend under STOP. Same shape as PI-STOP:
  wire an existing, sound authority store (`assert_not_killed(db, owner_id)`) into the spend reserve
  path. Owner: Cursor/MainAI (wiring). Not a Security-Kernel change.

## Separation of controls vs unimplemented integration
- **Implemented & enforced:** the entire reservation ledger (ceilings, allowlist, revoke, expiry,
  idempotency, append-only, fingerprint, accurate settlement). These are real and hold today.
- **Unimplemented integration:** SPEND-STOP (spend layer ↔ kill switch). This is the one gap; it is a
  wiring requirement, not a missing primitive, and it is contained behind the worker STOP gate today.

## Reproduce
```
probes/spend_authority_v1/run.sh ../../wtcert-aff5de1   # 11 passed, 1 xfailed (SPEND-STOP)
```
