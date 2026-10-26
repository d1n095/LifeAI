# Autonomous-loop acceptance — executed, not planned

Builds on the readiness matrix (examiner `5f050483`). Where the autonomous cycle is implemented, this
**executes** the real code and asserts the safety property; where it is not, it points to the exact
gap. No speculative scaffolding. Subject: #312 `22458257` (carries the base workforce subsystem).
`probes/autoloop_v1` — **4 passed**; no provider invoked (egress-guarded). `first_cycle_go=false`.

## Material correction to the earlier #310 DELEGATE-WIRING finding
The capability-based **selector + delegation broker are REAL and execute** — `app.workforce.selector`
(`score_candidates`/`select_best_candidate`, evidence-scored from `WorkforcePerformanceRollup` +
`capability_reality`, explicitly "capability_reality_is_not_authority", not hard-coded by name) and
`app.workforce.broker` (`submit_delegation_request`/`resolve_delegation`: select → package context →
mint a task-scoped assignment, with `assert_grant_allowed` at mint). The #310 DELEGATE-WIRING gap is
therefore narrower than first stated: it is specifically that #310's `self_improve` **policy stub does
not call this existing machinery** — the machinery itself is present and acceptance-passing.

## Executed legs (real code, PASS)
| Leg | Acceptance | Evidence |
|---|---|---|
| **Real delegation + dynamic capability selection** | `run_low_risk_public_text_delegation` selects the evidenced agent (`research-strong`, not hard-coded) and mints a task-scoped assignment; **no provider call** | AL1 (egress.provider_calls == []) |
| **STOP boundary** | a Founder STOP makes `resolve_delegation` fail closed at `assert_grant_allowed` | AL2 (+ positive control `assert_not_killed` raises) |
| **Independent verification at delegation** | verifier == builder → `BUILDER_CANNOT_SELF_VERIFY` | AL3 |
| **Evidence-driven, no invention** | no selectable agent → `SelectorError`, never a fabricated agent | AL5 |
| **Authorization scope / secret non-disclosure** | `TaskScopedAuthority` bounds (no execution effects, scoped paths); scenario denies `api_key`/`provider_credential` context kinds | exercised inside AL1 (scenario raises if a secret kind is disclosed) |

## Legs already proven elsewhere (not duplicated here)
- **STOP at provider ingestion + independent-verification gate + dedup** → `fc312_v1` (PI-STOP / ICV-WIRING / PI-DEDUP).
- **Spending**: ceilings, revoke, expiry, idempotent reservation, forged-approval rejection → `spend_authority_v1`.
- **Founder responsiveness + bounded self-improvement + restart durability of handover state** → `absence_v1`.
- **Duplicate-assignment blocking** → `unattended_v1` UA-7 (broker reuses the same `assert_no_duplicate_task_assignment`).

## Remaining gaps to a single wired autonomous cycle (owners)
1. **DELEGATE-WIRING (P2 · Cursor)** — connect #310 `self_improve` (and the absence loop) to the real
   `workforce.broker.resolve_delegation` so an evidence-backed proposal is actually delegated, not just
   prepared. The selector/broker are ready to be called.
2. **SPEND-STOP (P3 · Cursor)** — spend reserve should also consult the kill switch (defense in depth);
   `spend_authority_v1` G1.
3. **Single composed candidate** — #309-line provider authority (#312) + #310 absence loop + workforce
   delegation currently live on separate branches; a first unattended cycle needs them composed on one
   release line (Founder/Cursor branch decision), then re-examined.
4. **Founder live authority + real round trip + Gate-2 resources** — unchanged; out of examiner scope.

## Reproduce
```
probes/autoloop_v1/run.sh ../../wtcert-2245825   # 4 passed (real delegation/selection/STOP/self-verify)
```
