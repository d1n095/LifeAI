# Founder Identity / Session Revocation / Recovery — `460e8ad8` (migration 0095 + auth routes)

- **Subject:** the Founder identity and authentication boundary on candidate head
  `460e8ad8cbd40b7fb0d62702f28c14412688e298`, examined at the **application + PostgreSQL**
  boundary (real FastAPI auth routes via the product TestClient, plus the migration-0095 DB
  guards/functions):
  - `security_kernel_founder_token_guard` — runtime cannot mint/alter/delete any Founder auth
    proof (refresh / password-reset / email-verification tokens).
  - `reset_founder_password_with_token(token, hash)` — the only runtime-reachable Founder
    password-reset path; single-use, expiry/consumed checks; bumps `sessions_valid_after` and
    revokes all Founder refresh tokens.
  - `get_current_user` — rejects an access token whose jti is revoked OR whose `iat <=
    users.sessions_valid_after`.
  - `/api/auth` login / refresh (rotation + family reuse-detection) / logout / me.
- **Suite:** `probes/fc_founderauth_v1` — **10 passed** (`RESULTS_460e8ad.txt`). DB-backed via the
  target's own `tests.conftest`; drives the real auth routes and DB functions on the exact SHA.
  Loopback-only egress guard; no paid calls; no production mutation; target worktree unchanged.
  `first_cycle_go=false`.

## Scope / non-duplication
Does NOT duplicate the builder's `test_security_kernel_db_boundary` coverage (the users-identity
guard, forged-GUC session revocation, `revoke_current_founder_sessions`), nor the completed
STOP-clear / publication / DB-authority suites. `reset_founder_password_with_token` is tested
nowhere in the product; the app↔DB session-invalidation integration (a DB `sessions_valid_after`
bump actually causing the real routes to reject previously-issued tokens) is the examiner's
distinct contribution.

## Examiner findings — all PASS

| ID | Property examined | Result |
|---|---|---|
| FI1 | `reset_founder_password_with_token` rejects a malformed/too-short proof (`invalid Founder password-reset proof`), an unknown token, an expired token, and an already-consumed token (`invalid, expired, or consumed`) | PASS |
| FI2 | A valid reset **bumps `sessions_valid_after` and revokes every Founder refresh token** (live count → 0), and marks the proof `used_at` | PASS |
| FI3 | A reset proof is **single-use** — replaying it is rejected (`invalid, expired, or consumed`) | PASS |
| FI4 | **INTEGRATION** — a previously-issued, still-unexpired Founder access token is rejected by the real `/api/auth/me` route (401) after a reset (the DB `sessions_valid_after` bump reaches the app token check) | PASS |
| FI5 | **INTEGRATION** — the previously-issued Founder refresh token is rejected by `/api/auth/refresh` (401) after a reset | PASS |
| FI6 | **INTEGRATION** — logout immediately invalidates the still-live access token: `/me` → 401 afterwards (server-side jti revocation, not just cookie clearing) | PASS |
| FI7 | The founder-token guard fences **password-reset and email-verification** proofs too (not only refresh tokens): a runtime INSERT of either for the Founder is refused (`runtime role cannot mint, alter, or delete Founder authentication proof`) | PASS |
| FI8 | **STOP activation does not block Founder authentication** — with the Founder owner's STOP active, login, `/me`, and refresh all still succeed. Recorded as **expected behaviour (not a defect)**: the Founder must remain able to authenticate in order to clear a STOP; STOP governs agent authority, not Founder identity | PASS |
| FI9 | **Fail-closed** — a missing access cookie, a tampered/invalid JWT, and a (logout-)revoked-jti access token are all rejected with 401 | PASS |
| FI10 | **INTEGRATION** — replaying a rotated Founder refresh token is detected as reuse and revokes the whole family: the replay → 401, and the legitimate *new* token is then dead too | PASS |

### Interpreted result
On its exact SHA, the Founder identity / session boundary holds along every axis the task named:

- **Identity pinning / session restoration** — FI4/FI6/FI10 (refresh and `/me` consistently
  re-derive and re-validate the session against the DB revocation signal).
- **Password change / reset authorization** — FI1/FI2/FI3 (`reset_founder_password_with_token`).
- **Previously-issued tokens after credential changes** — FI4 (access) / FI5 (refresh).
- **Revoked / rotated sessions; replay / stolen session** — FI6 (logout), FI10 (rotation +
  family reuse-detection).
- **Step-up / privileged grants after authority changes** — the reset/logout bump of
  `sessions_valid_after` is the same signal that invalidates erasure/recall/step-up session
  binding (see `fc_erasureauth` AE9 and the cross-boundary suite `fc_crossauth`).
- **STOP during authentication** — FI8 (by-design non-blocking, truthfully recorded).
- **Fail-closed** — FI9.

**No defect found in the Founder identity / session revocation / recovery boundary on
`460e8ad8`.** The one behaviour that could look surprising — STOP not blocking Founder login
(FI8) — is intended and necessary (the Founder clears STOP), not a weakness; it is reported as
expected behaviour, distinct from a defect.

## Scope / discipline
Read-only examiner; drove the real auth routes via the product TestClient and the real DB
guards/functions through the target's own conftest. No paid calls; loopback only; no
publication/deployment/autonomy; no production branch modified. Candidate materialised detached
at the exact SHA and left unchanged. `first_cycle_go=false`.

## Reproduce
```
probes/fc_founderauth_v1/run.sh ../../wtcert-460e8ad      # 10 passed
```

## Next (continuation)
Priority 7 — Cross-Boundary Authorization Consistency: the interaction of Founder session
validity, authorization epochs, worker authorization, revocation of active privileged
operations, and durable recovery — focusing on paths that could restore previously-revoked
authority or permit execution under stale credentials.
