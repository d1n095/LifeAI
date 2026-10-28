# Revoked Founder Session vs Account-Erasure Authority — `460e8ad8` (Priority 8)

- **Subject:** executable reassessment of the LOW/informational observation recorded by
  `fc_crossauth_v1` — whether an explicitly revoked Founder session can still satisfy
  account-erasure authority, and whether that stale authority can authorize a real
  protected-data deletion — on candidate head
  `460e8ad8cbd40b7fb0d62702f28c14412688e298`.
- **Suite:** `probes/fc_erasuresession_v1` — **7 passed** (`RESULTS_460e8ad.txt`), deterministic
  (run 3×). DB-backed via the target's own `tests.conftest`; drives the REAL production
  revocation functions and the REAL erasure state machine on the exact SHA. Loopback-only; no
  paid calls; no production mutation; target worktree unchanged. `first_cycle_go=false`.

## Verdict: CONFIRMED DEFECT — reclassified LOW/informational → **MEDIUM**

The prior dismissal ("the window closes the next second") is **refuted**. The session-binding
check `account_erasure_receipt_session_current` (migration `0086`, authoritative body — not
redefined later) decides currency with `rt.created_at > u.sessions_valid_after`, a comparison
of two **stored** timestamps that does **not** move with the wall clock (ES2, proven with rows
dated 2020), and it **never inspects `refresh_tokens.revoked_at`**. Every real revocation
primitive truncates `sessions_valid_after` down to the current second (to match the integer
`iat` check) and sets `revoked_at` on the revoked session; so a session whose refresh row was
created at a sub-second offset inside that same second remains `created_at > sessions_valid_after`
== TRUE after it is revoked, and the helper still treats it as current.

## Responsible component
`account_erasure_receipt_session_current(uuid, text)` in
`backend/alembic/versions/0086_account_erasure_reauth_binding.py`. It gates
`account_erasure_begin_operation` / `_set_phase` / `_complete_operation` and
`personal_recall_erasure_current` (the predicate every erasure-scoped grant delete consults).

## Why the certified AE9 "fail-closed" missed it
`fc_erasureauth` AE9 revokes with `sessions_valid_after = now() + interval '1 minute'` — a full
minute ahead of the token — and legitimately observes fail-closed. **No real revocation path
revokes that aggressively:** `reset_founder_password_with_token` (0095),
`revoke_current_founder_sessions` (0095) and `revoke_all_sessions_for_user`
(`app/token_revocation.py`) all set `sessions_valid_after` to the current second truncated
down. AE9's fixture over-revoked and so could not surface the sub-second same-second window.

## Examiner findings — all PASS (each a distinct proof)

| ID | Property examined | Result |
|---|---|---|
| ES1 | The REAL `reset_founder_password_with_token` revokes the session (`revoked_at` set, `sessions_valid_after` bumped), yet with a same-second `created_at` the helper returns **TRUE** | PASS |
| ES2 | Wall-clock independence: TRUE persists at +1s/+1min/years (stored-vs-stored); the different-second revoke is correctly FALSE — defect is strictly the sub-second same-second case | PASS |
| ES3 | **End-to-end:** under the revoked session a NEW erasure operation begins and a REAL `personal_recall_grants` row is DELETED and committed (protected data destroyed, not just a boolean) | PASS |
| ES4 | Mechanism matrix: the access-jti blocklist (single logout / family-reuse revocation) DOES close the gate; the `sessions_valid_after` bump used by reset / logout-all / revoke-sessions does NOT, same-second | PASS |
| ES5 | An already-STARTED operation continues under the revoked session even after the consumed receipt is EXPIRED — post-begin checks consult only session-currency + `consumed_at`, never receipt lifetime → exposure **unbounded** for an in-flight op | PASS |
| ES6 | Survives a fresh DB session / process restart (the discrepancy is in committed rows) | PASS |
| ES7 | Remediation is deterministic: `revoked_at IS NOT NULL` after every revocation, and the helper's EXISTS body PLUS `AND rt.revoked_at IS NULL` returns FALSE on the same rows | PASS |

## Answers to the seven questions
1. **Can an explicitly revoked refresh session still satisfy erasure authority?** Yes — ES1/ES4,
   in the same-second case, via `reset`, `revoke-sessions`/`logout-all` (the `sessions_valid_after`
   bump path). The access-jti blocklist path (single logout, family-reuse) does close it (ES4).
2. **Does the condition persist past 1s / 1min / until receipt expiry?** It persists
   indefinitely as a stored comparison (ES2). A *new* `begin_operation` is still bounded by the
   receipt's ≤5-min lifetime; an *in-flight* operation is not bounded at all (ES5).
3. **Does Founder password reset invalidate every previously issued erasure authorization?** No
   — not in the same-second window (ES1/ES3); the reauth receipt bound to the revoked session
   remains usable.
4. **Do logout / rotation / family revocation close the boundary?** Single logout and
   family-reuse revocation do (they blocklist the access jti — ES4). Logout-all / reset /
   revoke-sessions do not, same-second (ES4). Normal refresh rotation is the happy path, not a
   revocation signal.
5. **Can an already-started operation continue after the session is revoked?** Yes, and
   unbounded by receipt lifetime (ES5).
6. **Can the stale authority authorize a real protected-data deletion (not just return true)?**
   Yes — ES3 deletes and commits a real `personal_recall_grants` row under the revoked session.
7. **Does it survive process restart / fresh DB sessions?** Yes (ES6).

## Narrow remediation contract (for the development agent — examiner does NOT modify the product)
- **File / function:** `0086_account_erasure_reauth_binding.py` → `account_erasure_receipt_session_current`.
- **Minimal change:** add `AND rt.revoked_at IS NULL` to the EXISTS predicate (closes it for
  every revocation path, since each sets `revoked_at`). Optionally also align the freshness test
  with the fail-closed `iat` semantics: `date_trunc('second', rt.created_at) > u.sessions_valid_after`
  (or `rt.created_at >= u.sessions_valid_after + interval '1 second'`).
- **Scope discipline:** one forward migration that `CREATE OR REPLACE`s only this function; no
  table/privilege change; a one-predicate fix that must not widen other erasure/authority
  behavior.
- **Regression gate (Priority 9):** a session revoked by each real primitive in the same second
  fails session-binding; no new operation can begin and no in-flight operation can delete under
  a revoked session; the different-second revoke still fails closed; a genuinely live session
  still passes (no false lockout). ES1/ES3/ES5/ES7 are the executable templates.

## Scope / discipline
Read-only examiner; drove the real reset / revoke-sessions production functions and the real
erasure state machine via the target's own conftest. RECALL STAYS INACTIVE — the deleted row is
a Recall grant minted through the product's own privileged service purely as the erasure target;
nothing was ingested or retrieved. No paid calls; loopback only; no publication/deployment/
autonomy; no production branch modified. Candidate materialised detached at the exact SHA and
left unchanged. `first_cycle_go=false`.

## Reproduce
```
probes/fc_erasuresession_v1/run.sh ../../wtcert-460e8ad      # 7 passed (deterministic)
```
