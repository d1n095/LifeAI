# Account-Erasure Operation Authority — `460e8ad8` (migrations 0085/0086/0088)

- **Subject:** the PostgreSQL authority for account erasure — the bridge between a recent
  password reauthentication and the phase-gated deletion of protected data, on candidate head
  `460e8ad8cbd40b7fb0d62702f28c14412688e298`:
  - `account_erasure_reauth_receipts` — governed; runtime SELECT-only; BEFORE-write guard on
    `current_user='mainai_app'`; RLS `WITH CHECK(false)`. Created only by the privileged reauth
    service after a real password check.
  - `account_erasure_operations` — the state machine (status/phase); after 0086 the runtime
    loses INSERT/UPDATE/DELETE (SELECT-only) and may only drive it through SECURITY DEFINER
    functions `account_erasure_begin_operation` / `_set_phase` / `_complete_operation`.
  - `personal_recall_erasure_current(owner)` — the gate every erasure-scoped delete consults;
    true ONLY when the erasure GUCs match a LIVE operation row in the exact phase whose
    consumed reauth receipt is still bound to a current founder session.
  - Migration 0088 enforces ORDERED phases: `started → personal_recall_erasure →
    personal_data_erasure`, and completion requires the final phase.
- **Suite:** `probes/fc_erasureauth_v1` — **10 passed** (`RESULTS_460e8ad.txt`). DB-backed via the
  target's own `tests.conftest`; authority substrate built through the product's own privileged
  services bound to the canonical Founder identity + a live access JTI. Loopback-only egress
  guard; no paid calls; no production mutation; target worktree unchanged. `first_cycle_go=false`.
- **RECALL KEPT INACTIVE:** no ingestion/retrieval/activation; only the substrate needed to
  examine the erasure authority. No prior certification relied upon.

## Scope / non-duplication
None of the erasure guard/state-machine messages exercised here is asserted anywhere in
`tests/` (verified by grep). The existing erasure-adjacent tests (`test_production_activation`)
cover the happy-path crypto/lifecycle, not this adversarial DB authority boundary.

## Examiner findings — all PASS (intended privileged behaviour; no defect)

| ID | Property examined | Result |
|---|---|---|
| AE1 | Reauth receipts are governed — the runtime cannot write them: SELECT-only (privilege denied) AND, under a temporary future GRANT, the guard refuses the runtime identity (`receipts are governed; raw runtime writes are forbidden`) | PASS (dual layer) |
| AE2 | Operations are governed — the runtime cannot fabricate a fake ACTIVE erasure op: SELECT-only AND guard-refused (`operations are governed; use account erasure functions`). This is what would otherwise unlock every phase-gated delete | PASS (dual layer) |
| AE3 | A **forged erasure GUC + a random operation id** (with a real founder session) cannot unlock a protected-data delete — `erasure_current` finds no matching op row → the recall-grant delete is refused; the grant survives | PASS (fail-closed) |
| AE4 | `begin_operation` validates the reauth receipt: a missing/wrong-owner receipt (`missing or wrong owner`), a receipt whose session JTI does not match the current session (`not bound to a current session`), and an already-consumed receipt (`already consumed`) are all rejected | PASS |
| AE5 | The reauth receipt is **single-use under concurrency**: two racing `begin_operation` calls → exactly one operation begins, the other fails (`already consumed` / `concurrently consumed`) | PASS |
| AE6 | **Cross-owner**: a founder's receipt cannot begin an operation for another owner (`missing or wrong owner` / `requires current owner context`); and a legitimate founder operation cannot be used from an attacker (other-owner) session to delete the founder's grant — the delete matches zero rows (RLS + owner-bound `erasure_current`); the grant survives | PASS |
| AE7 | Phase transitions are bounded: an unsupported phase (`unsupported account erasure phase`), a wrong owner context (`requires current owner context`), and a **skipped ordering** `started→personal_data_erasure` (`phase transition is invalid or lacks current reauth authority`) are all rejected | PASS |
| AE8 | A protected-data delete is unlocked **only in the exact authorized phase** — blocked while the op is in `started`, allowed after `set_phase('personal_recall_erasure')`; a mid-phase DELETE then ROLLBACK leaves the grant intact and the operation still `active/personal_recall_erasure` (resumable), and a re-issued delete then commit removes it | PASS (transaction boundary / recovery) |
| AE9 | Losing the founder session mid-operation **re-closes the gate**: after advancing `sessions_valid_after` past the refresh token, `receipt_session_current` → false → `erasure_current` → false → the protected delete is refused again; the grant survives | PASS (fail-closed on authority revocation) |
| AE10 | Completion **closes the gate and preserves the record**: after the ordered progression to `personal_data_erasure` and `complete_operation`, the op is `completed/completed` (no longer `active`) so further deletes are refused; the operation row persists and the reauth receipt remains consumed-by-this-op (durable audit) | PASS |

### Interpreted result — "can protected data be deleted outside a legitimately authorized erasure?"
**No.** A protected-data delete (demonstrated on a Recall grant, the same gate used by the
transport/spend/recall delete paths) is unlocked only when ALL of the following hold
simultaneously: a real `account_erasure_operations` row, `status='active'`, the **exact** phase
`personal_recall_erasure`, matching erasure GUCs, a consumed reauth receipt bound to that
operation, and a **currently-live founder session**. Removing any one — forged GUC (AE3),
fabricated op (AE2), wrong/earlier phase (AE7/AE8), cross-owner (AE6), or a revoked session
(AE9) — fails the delete closed. The operation itself can only be created/advanced through the
SECURITY DEFINER state machine (AE1/AE2), never by raw runtime SQL, and every operation + its
consumed receipt persist as a durable audit record (AE10).

**No defect found in the account-erasure operation authority boundary on `460e8ad8`.** All
observed denials are intended privileged behaviour; all observed allowances require the full,
ordered, session-bound authority chain.

## Scope / discipline
Read-only examiner; drove the real reauth/begin/set_phase/complete SECURITY DEFINER functions,
the governed-table write guards, `personal_recall_erasure_current` gating, RLS, and the ordered
phase machine through the target's own conftest. Recall kept inactive. No paid calls; loopback
only; no publication/deployment/autonomy. Candidate materialised detached at the exact SHA and
left unchanged. `first_cycle_go=false`.

## Reproduce
```
probes/fc_erasureauth_v1/run.sh ../../wtcert-460e8ad      # 10 passed
```

## Next (continuation)
Priority 5 — Founder Capability and Authority Changes (migration `0094`): capability grants,
authority-change proposals, privilege escalation, stale authority, concurrent changes, replay,
owner isolation.
