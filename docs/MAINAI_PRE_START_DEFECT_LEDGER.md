# MainAI Pre-Start Defect Ledger (certification)

Status vocabulary: DISCOVERED → REPRODUCED → ROOT_CAUSED → FIX_IN_PROGRESS →
LOCALLY_FIXED → INDEPENDENTLY_VERIFIED → MERGED → POST_MERGE_PROVEN → CLOSED.

CLOSED requires independent Claude evidence on exact tip SHA — not Cursor self-certification.

| ID | Issue | Status | Notes |
|---|---|---|---|
| KS-OWNER-GLOBAL | #234 process-global kill switch / cross-owner DoS | LOCALLY_FIXED | Durable `mainai_stop_state` owner vs global (0069); dashboard/observability now use `query_stop_status` |
| KS-BOOT-CLEAR | Boot/composed auto clear | LOCALLY_FIXED | Boot surfaces BLOCKED_BY_KILL_SWITCH; fabricated ack denied |
| READY-IMPORT | Readiness importable==healthy | LOCALLY_FIXED | receipts.py: IMPORTABLE≠HEALTHY |
| READY-MIG | Migration check hardcoded healthy | LOCALLY_FIXED | verify_migration_head runs alembic |
| READY-BLOCKERS | Blocker list overwrite | LOCALLY_FIXED | accumulate via dict.fromkeys |
| EVID-SEM | #213/#220 evidence exists≠supports | LOCALLY_FIXED | `evidence_supports_claim` + gate verified_available |
| MEM-REPLAY | #211 memory→work replay contract | LOCALLY_FIXED | created_now_ids / canonical / replayed; idempotent park-path on `5b15f02` |
| P1-EXISTING-STATE | Existing-state rich DB boot | LOCALLY_FIXED | `seed_rich_safe_internal_state` + `--existing-state` |
| P1-PROVIDER-LEDGER | Independent provider zero-proof | LOCALLY_FIXED | boot receipt `provider_ledger_crosscheck` |
| P1-BACKUP-RESTORE | Safe-internal backup→restore | LOCALLY_FIXED | `safe_internal_backup_restore_proof.py` |
| P1-FRESH-PROCESS | Cold multi-process continuity | LOCALLY_FIXED | `fresh_process_continuity_proof.py` (A→B→C) |
| #218 | paraphrase follow-up | OPEN | deferred unless blocks start |
| #229 | independent race verdict | OPEN | Claude-owned; do not self-verify |
| #224 | current-truth/cognitive-load | OPEN | Claude-owned |
| #227 | 1000-tick soak | DEFERRED | post-start / serious autonomy |

**Claude exam target (P0):** `5b15f02a581fcb12ddd4c37f569175fcfb44a5d9` — see `docs/CLAUDE_CERTIFICATION_ATTACK_5b15f02.md`.
If HEAD moves, record new SHA; re-run changed surfaces only.
