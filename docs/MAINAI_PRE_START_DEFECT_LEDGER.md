# MainAI Pre-Start Defect Ledger (certification)

Status vocabulary: DISCOVERED → REPRODUCED → ROOT_CAUSED → FIX_IN_PROGRESS →
LOCALLY_FIXED → INDEPENDENTLY_VERIFIED → MERGED → POST_MERGE_PROVEN → CLOSED.

| ID | Issue | Status | Notes |
|---|---|---|---|
| KS-OWNER-GLOBAL | #234 process-global kill switch / cross-owner DoS | FIX_IN_PROGRESS | Durable `mainai_stop_state` owner vs global (0069) |
| KS-BOOT-CLEAR | Boot/composed auto `clear_kill_switch_for_recovery` | FIX_IN_PROGRESS | Boot surfaces BLOCKED_BY_KILL_SWITCH; fabricated ack denied |
| READY-IMPORT | Readiness importable==healthy | FIX_IN_PROGRESS | receipts.py: IMPORTABLE≠HEALTHY |
| READY-MIG | Migration check hardcoded healthy | FIX_IN_PROGRESS | verify_migration_head runs alembic |
| READY-BLOCKERS | Blocker list overwrite | FIX_IN_PROGRESS | accumulate via dict.fromkeys |
| EVID-SEM | #213/#220 evidence exists≠supports | FIX_IN_PROGRESS | `evidence_supports_claim` + gate verified_available |
| MEM-REPLAY | #211 memory→work replay contract | FIX_IN_PROGRESS | created_now_ids / canonical_candidate_ids / replayed |
| #218 | paraphrase follow-up | OPEN | deferred unless blocks start |
| #229 | independent race verdict | OPEN | Claude-owned; do not self-verify |
| #224 | current-truth/cognitive-load | OPEN | Claude-owned |
| #227 | 1000-tick soak | DEFERRED | post-start / serious autonomy |

CLOSED requires independent Claude evidence on exact tip SHA — not Cursor self-certification.
