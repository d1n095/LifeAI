# MainAI Pre-Start Defect Ledger (blocker eradication)

Status vocabulary: DISCOVERED → REPRODUCED → ROOT_CAUSED → FIX_IN_PROGRESS →
LOCALLY_FIXED → INDEPENDENTLY_VERIFIED → MERGED → POST_MERGE_PROVEN → CLOSED.

CLOSED requires independent Claude evidence on exact tip SHA — not Cursor self-certification.

| ID | Issue | Status | Notes |
|---|---|---|---|
| AUTH-STOP-GRANT | Kill-switch vs grant race | LOCALLY_FIXED | FOR UPDATE fence + race tests |
| READY-MIG-GATE | blocking_migrations ungated | LOCALLY_FIXED | |
| READY-BOOT-AUDIT | boot audit mixed identity | LOCALLY_FIXED | |
| #236.* | six school/exec findings | LOCALLY_FIXED | |
| #218-PROP | must_surface on #219–#227 | LOCALLY_FIXED | |
| EVID-SEM | evidence supports claim | LOCALLY_FIXED on #237 | |
| #235-REPLAN | stale assumption replan | LOCALLY_FIXED | |
| #235-CONCUR | same-owner cycle deadlock | LOCALLY_FIXED | |
| SIBLING-RACES | work_candidate / proposal / capability / prediction / roi | LOCALLY_FIXED | tip + #220/#226 SAVEPOINT recovery |
| #229-LABEL | race loser labeled created | LOCALLY_FIXED | idempotency_key match + promote recovery |
| #219-ISO | structured_claims cross-owner entity refs | LOCALLY_FIXED on #219 | app + composite FK 0069 |
| #218/#229/#224 | Claude independent verify | OPEN | examiner-owned |
| #227 | 1000-tick soak | DEFERRED | post-start |

**Claude exam target (P0 pass):** `ecae232` — `docs/CLAUDE_CERTIFICATION_ATTACK_ecae232.md`  
**Cursor P1 tip after siblings:** record `git rev-parse HEAD` on `cursor/mainai-safe-internal-startup`.

#237 MUST NOT MERGE until Claude independent attack + CI + post-merge tip proof.
