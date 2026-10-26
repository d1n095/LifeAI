# Priority 3 — first-cycle integration acceptance gate (test-first)

- **Suite:** `probes/fc_firstcycle_v1` — the acceptance gate for the **next corrected autonomous-cycle
  candidate**. Established now against the current best composed path **#314 `2308b697`**:
  **4 passed, 1 xfailed(strict), 2 skipped (BLOCKED)**. Read-only; no provider calls; `first_cycle_go=false`.
- **Usage for the successor:** `fc_firstcycle_v1/run.sh ../../wtcert-<corrected-sha>` — ACCEPT-1 flips
  from xfail to pass, and ACCEPT-6/7 become runnable, when the wiring is corrected.

| ID | Requirement | #314 `2308b697` | Meaning |
|---|---|---|---|
| ACCEPT-1 | real selector → valid coordination recipient → **broker-issued** assignment | **xfail(strict) = FAIL** | UNMET: SELECTOR-ASSIGN-NAMESPACE (agent_key rejected by `service.assign`) + DELEGATE-BROKER-BYPASS (broker never used) |
| ACCEPT-2 | no hardcoded fallback **masking** a selection failure | **PASS** | the capability-default fallback is transparently labeled (`no_workforce_agent_use_capability_default`), never presented as real selection |
| ACCEPT-3 | no simulated provider event reported as genuine delivery | **PASS** | `vendor_delivery_claimed=False`, `independently_verified=False` |
| ACCEPT-4 | Founder interaction available during background work | **PASS** | interactive admitted; background cannot monopolize |
| ACCEPT-5 | STOP halts the cycle | **PASS** | STOP → absence `stop_blocked`, not `cycled` |
| ACCEPT-6 | durable recovery w/o duplicate **broker** assignments / external effects | **BLOCKED** | needs ACCEPT-1 first (no WorkforceAssignment is issued by a #314 tick) |
| ACCEPT-7 | spend reserved within grant during the live delegation leg | **BLOCKED** | SPEND-STOP closed at the spend layer, but the cycle never reaches a paid reservation; needs corrected wiring + Founder live authority |

## Verdict
**The first-cycle acceptance gate is built and anchored.** On #314 `2308b697` the composed cycle meets
the honesty/availability/STOP legs (ACCEPT-2..5 PASS) but **does not** meet the core delegation
requirement (ACCEPT-1 FAIL — the two P2 defects from `fc314`), and the recovery/spend legs are **BLOCKED**
pending that wiring. Nothing missing is reported as PASS. When Cursor publishes a corrected successor of
#314, re-run this exact suite against that SHA: ACCEPT-1 must flip to PASS and ACCEPT-6/7 must be
enabled and pass before the composed cycle can be considered for first-cycle acceptance.
`first_cycle_go=false`. Read-only; no merge/deploy/GO.

## Reproduce
```
probes/fc_firstcycle_v1/run.sh ../../wtcert-2308b69     # 4 passed, 1 xfailed(ACCEPT-1), 2 skipped
# corrected successor: probes/fc_firstcycle_v1/run.sh ../../wtcert-<new-sha>
```
