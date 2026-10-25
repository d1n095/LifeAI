# Limitation Transparency & Authorized Resolution — acceptance contract (NEW Founder requirement)

MainAI must **report material restrictions rather than silently work around them**, resolve ordinary problems within
existing permissions, and pause for authorization when an alternative needs more privilege/spend or would violate a
restriction. Examiner-owned, test-first. `limits_v1` maps each sub-requirement to the real substrate and classifies it.
No production change. `first_cycle_go=false`.

## Status by sub-requirement (evidence = `limits_v1`)
| Requirement | Status | Substrate / evidence | Gap |
|---|---|---|---|
| Recognize + disclose a material limit (what/why) | **IMPLEMENTED** (LT-1) | every gate returns `outcome=blocked` + named `missing_capability` + `reason` | a unified cross-domain limit taxonomy (technical/legal/contractual/security/financial/authorization) is not centralized |
| Document the attempted operation + evidence | **IMPLEMENTED** (LT-2) | blocked result carries `transport_id`/`reason`/`missing_capability`/`live_http_attempted=False` | not yet written to a durable restriction log (see below) |
| Investigate alternatives WITHOUT unauthorized bypass | **PARTIAL** (LT-3) | no alternative auto-executes; live mutate hard-blocked even with enable flags | the *orchestrated* "try safe alternatives, then stop" workflow is NOT_IMPLEMENTED |
| Resolve ordinary technical problems within existing permissions | **IMPLEMENTED** (Gate A UA-1/6/8) | autonomous dispatch/next-task/stall-recovery within a grant | — |
| Pause + request authorization for privilege/spend/restriction | **IMPLEMENTED** (LT-5) | `escalate_privilege → privilege_rejected`; dispatch `APPROVAL_REQUIRED`; spend fail-closed | — |
| Never treat a loophole as authorization | **IMPLEMENTED** (LT-6) | `FORBIDDEN_RECEIPT_KEYS` → `forged_rejected` | — |
| Policies not overridable by external-agent instructions | **IMPLEMENTED** (LT-7, RAI/CR) | non-privileged actor / forbidden keys rejected | — |
| Durable, searchable, Founder-inspectable restriction history | **PARTIAL** (LT-8) | `founder_communication_ledger` append-only (deny-mutation trigger) + supersede-correctable; `inspectable_memory` | a **dedicated restriction-history writer** (record each limit + attempt + evidence) is NOT_IMPLEMENTED |
| Fact / hypothesis / approval / uncertainty distinction; learn only from verified | **IMPLEMENTED** (LT-9) | `MemoryTruthClaim` states `said→stored→planned→implemented→verified` + `verified_result` | — |
| Do not reuse expired/revoked/unrelated approvals | **IMPLEMENTED** (LT-10) | bridge revoke/expiry (CS-4), lease expiry (UA-8), spend-grant exams (#268-271) | — |
| **Unified limitation-report-and-authorized-resolution orchestrator** | **NOT_IMPLEMENTED** (LT-ORCH) | building blocks above all present | the single workflow tying disclose → document (durable) → investigate-without-bypass → pause-for-auth is absent |

## Negative controls (all PASS)
- Forged Founder authorization (role-only impostor) → 403 at `require_founder` (id-pinned) — LT-NEG.
- Provider suppression of a limitation report / payload flip → a blocked result cannot be turned to success by payload fields (LT-6/LT-7); `vendor_delivery_claimed=False`.
- Attempted secret storage → provider secret never echoed/stored in outputs — LT-NEG2.
- Tamper of restriction history → the append-only ledger's deny-mutation trigger rejects UPDATE/DELETE; correction is a NEW superseding row (history preserved) — LT-8.

## Minimal implementation contract for the NOT_IMPLEMENTED pieces (Cursor/MainAI)
1. **`report_limitation(db, *, owner_id, domain, operation, reason, missing_capability, evidence)`** — writes an append-only, owner-scoped, Founder-inspectable restriction record (reuse the `founder_communication_ledger` / an analogous governed table with the migration-0038 deny-mutation trigger). Correctable only by a superseding row; never a silent edit.
2. **Resolution policy**: autonomous retry/alternative ONLY within the current grant's scope; the moment an alternative needs a new privilege, additional spend, or would cross a restriction, STOP and emit a `decision_requested` record — never execute. A discovered loophole is explicitly NOT authorization (reuse the forbidden-key discipline at any new ingress).
3. **Truth discipline**: every restriction record carries a `MemoryTruthClaim`-style state (observed-fact vs hypothesis vs Founder-approval vs uncertainty); "learned" only on `verified_result`.
4. **Inspection/correction API**: Founder-only read + supersede, preserving audit integrity (no destructive edit).

## Acceptance when implemented
`limits_v1` LT-ORCH flips from skip to a positive end-to-end test: a blocked operation produces a durable inspectable restriction record with named cause + attempt + evidence; a safe in-grant alternative resolves autonomously; a privilege/spend/restriction-crossing alternative pauses with a `decision_requested` and executes nothing; the record is inspectable and correctable without mutating history; and no external-agent instruction or loophole alters any of it. LT-1..LT-10 + the negative controls remain green.
