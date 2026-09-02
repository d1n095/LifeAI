# MainAI Pre-Start Defect Ledger (blocker eradication)

Status vocabulary: DISCOVERED → REPRODUCED → ROOT_CAUSED → FIX_IN_PROGRESS →
LOCALLY_FIXED → INDEPENDENTLY_VERIFIED → MERGED → POST_MERGE_PROVEN → CLOSED.

CLOSED requires independent Claude evidence on exact tip SHA — not Cursor self-certification.

| ID | Issue | Status | Notes |
|---|---|---|---|
| AUTH-STOP-GRANT | Kill-switch vs grant race — live authority after STOP | LOCALLY_FIXED | FOR UPDATE fence; grant refuses while stopped; global revokes all; race test |
| KS-OWNER-GLOBAL | Durable owner/global stop | LOCALLY_FIXED | 0069 + observability durable |
| KS-BOOT-CLEAR | Boot auto-clear | LOCALLY_FIXED | |
| READY-IMPORT | Importable==healthy | LOCALLY_FIXED | |
| READY-MIG-GATE | blocking_migrations did not gate readiness | LOCALLY_FIXED | now BLOCKED |
| READY-BOOT-AUDIT | record_boot_blocked scope/seq mix | LOCALLY_FIXED | one blocking identity |
| EVID-SEM | #213/#220 evidence supports claim | LOCALLY_FIXED on #237 | tip still needs merge |
| MEM-REPLAY | #211 replay contract | LOCALLY_FIXED | |
| #236.1 | safe_composed auto-clear | LOCALLY_FIXED on #237 | LIVE on tip until merge |
| #236.2 | cross-owner metrics | LOCALLY_FIXED | owner-scoped counters |
| #236.3 | verification_status case | LOCALLY_FIXED | normalize case; fail closed |
| #236.4 | curriculum random idempotency | LOCALLY_FIXED | stable owner/domain/skill key |
| #236.5 | double route_local_first | LOCALLY_FIXED | removed pre-route |
| #236.6 | registry #235 Öppen vs mergad | LOCALLY_FIXED | status Mergad |
| #235-REPLAN | stale assumption indefinite replan | LOCALLY_FIXED | open-problem join only |
| #235-CONCUR | same-owner cycle deadlock | LOCALLY_FIXED | advisory xact lock + scoped keys |
| #218-PROP | must_surface cleared on stacked K–S | LOCALLY_FIXED | ported to #219–#227 |
| #229-LABEL | race loser labeled created | OPEN | P1 |
| SIBLING-RACES | work_candidate/proposal/prediction/roi/capability | OPEN | P1 |
| #219-ISO | structured_claims cross-owner FK | OPEN | P1 |
| #218 | Claude independent verify | OPEN | Claude-owned |
| #229 | Claude race verdict | OPEN | Claude-owned |
| #224 | cognitive-load | OPEN | Claude-owned |
| #227 | 1000-tick soak | DEFERRED | post-start |

**#237 MUST NOT MERGE** until Claude independent attack on latest head.

Current candidate after this eradication pass: record SHA from `git rev-parse HEAD` on `cursor/mainai-safe-internal-startup`.
