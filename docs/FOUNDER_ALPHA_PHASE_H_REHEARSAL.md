# Founder Alpha — Phase H end-to-end release rehearsal

This is a rehearsal, not a re-audit: it re-executes a sample of what Phases A-G already
verified, from a genuinely fresh installation, on the exact current candidate commit — it does
not infer a pass from any older commit's result.

## Exact source SHA

`5d02edc662fb492144c4da84028f02f0376d9753` (PR #478, branch
`claude/founder-alpha-phase-g-review`, base `integration/founder-alpha-20261121`). Re-confirmed
via `git rev-parse` against `origin/claude/founder-alpha-phase-g-review` before branching, not
trusted from an abbreviated hash.

No application code changed in this phase — see "Outstanding defects" below for the one thing
found and not fixed (a reproduction-command portability gap in docs, not application code).

## Installation outcome — real, fresh, in a disposable sandbox

- Created a brand-new PostgreSQL database (`lifeos_phaseh_live`) that had never been touched.
- `backend/scripts/setup/bootstrap_database.py` (fresh install): all 7 steps `ok`
  (`ensure_app_role`, `migrate`, `apply_privileges`, `verify_schema`, `verify_boot`,
  `verify_grants`, `verify_s1a`).
- Same command again with `--verify-only`: clean, confirming idempotency (the exact regression
  this script's docstring says it exists to catch).
- `npm ci` + `npm run build` in `frontend/`: clean build, no new TypeScript/ESLint findings.
- Started the *actual* production entrypoints, not a dev server: `node .next/standalone/server.js`
  for the frontend (the literal `CMD` in `frontend/Dockerfile`) and `python -m uvicorn
  app.main:app` / `python -m app.worker` for the backend/worker — all booted clean against the
  freshly-installed database.

## Real-model readiness — re-checked, not assumed

Ran `scripts/acceptance/founder_workflow.py --supervise` against the live stack. Steps 1
(start) and 2 (authenticate — real login, real cookie, real CSRF, through the frontend's
same-origin proxy) passed. Step 3 failed exactly as Phase C and Phase D found:

```
local_model not reachable: {'runtime': 'ollama', 'reachable': False, ...}
```

This sandbox has no `ollama` binary at all (not just blocked model weights) — confirmed with
`which ollama` (empty) and a direct probe of `127.0.0.1:11434` (connection refused). Same
confirmed infra limitation as Phases C/D, independently re-confirmed here on a separate fresh
install rather than assumed to still hold. The harness correctly stops rather than printing a
false pass; it does not fabricate a model response.

## Functional acceptance

Re-running `founder_workflow.py` end-to-end was blocked at step 3 by the above, by design (it
refuses to continue past an unverifiable step). Exercised the remaining steps directly against
the live stack instead:

- Real login through the proxy (`POST /api/auth/login`): real session cookie + CSRF issued.
- `GET /api/health`: `{"status":"ok","worker":"alive","storage":{"writable":true}}`.
- Real document upload (`POST /api/library/import`): accepted, job created.
- Worker claim confirmed for real: polling the job afterward shows status moved
  `pending → blocked` (not stuck on `pending`), `progress_current: 1/1`, with the exact correct
  fail-closed reason (`"Ingen API-nyckel är konfigurerad..."` — no embedding provider reachable,
  file safely stored for automatic retry). This is the Phase C worker-service fix, confirmed
  live on a brand-new install, not re-derived from that phase's own evidence.
- Real project creation (`POST /api/projects`): succeeded, real row returned.
- Full backend suite reuse (all four exact reproduction commands copied verbatim from Phase G's
  `docs/FOUNDER_ALPHA_INDEPENDENT_REVIEW_PACKAGE.md`, run against fresh disposable test
  databases): see Security results below — all of these are also functional-acceptance
  evidence (`tests/founder_acceptance/`, `test_worker.py`, `test_session_auth.py`).
- Real-browser spot check (Playwright, real Chromium, against the live stack):
  `founder-console.spec.ts`'s first test ("renders the Living Core and real backend status")
  **passed** against a live backend over the real proxy. Its second test (real local-model
  answer) failed at the same confirmed Ollama boundary — not a new defect, the same limitation
  as above, now also visible through the actual UI rather than only the API.

## Security results — Phase G's exact commands, re-run verbatim

All four reproduction commands from `docs/FOUNDER_ALPHA_INDEPENDENT_REVIEW_PACKAGE.md`, copied
exactly, run against this exact head:

| Property | Command (from the review package) | Result |
|---|---|---|
| Central egress / lease / local-only / RLS | `pytest backend/tests/backend/providers/test_chat_with_fallback_central_authorization_boundary.py -v` | 7/7 passed |
| Grant issuance | `pytest backend/tests/backend/mainai/test_agent_dispatch_authorization_boundary.py backend/tests/backend/mainai/test_system_activation_authorization.py -v` | 21/21 passed |
| Owner/tenant isolation (RLS) | `pytest backend/tests/security/ -k rls_isolation -v` | 50/50 passed (59 deselected by the filter) |
| Proposal workspace isolation | `pytest backend/tests/backend/test_proposal_worktree_isolation.py backend/tests/security/test_proposal_isolation_adversarial.py backend/tests/backend/test_proposal_mode_checkout_restoration.py -v` | 27/27 passed |
| Worker lease fencing / revocation | `pytest backend/tests/backend/providers/test_chat_with_fallback_central_authorization_boundary.py::test_2_... backend/tests/backend/mainai/test_recovery_takeover_authority_fencing.py backend/tests/backend/mainai/test_operator_lease_effect_time_race.py -v` | 26/26 passed |
| Recovery / revocation / dispatch / worker / founder acceptance | `pytest backend/tests/backend/test_database_recovery.py backend/tests/backend/mainai/test_recovery_takeover_authority_fencing.py backend/tests/security/test_session_auth.py backend/tests/backend/jobs/test_worker.py backend/tests/founder_acceptance/ -v` | 66 passed, 1 skipped (expected — the model leg, same documented skip as every prior phase) |

**Finding — reproduction-command portability gap (doc-only, not a security defect):** every
command above, run exactly as the review package wrote it with no extra environment variables,
fails in this sandbox with `connection to server at "localhost", port 5433 failed`. This host's
real PostgreSQL listens on **5432**; `backend/tests/conftest.py`'s default
(`postgresql://lifeos@localhost:5433/...`) assumes the CI container's port instead. Confirmed
this is purely the port assumption, not a deeper problem: every command above passes 100% once
`DATABASE_URL`/`APP_DATABASE_URL` are exported pointing at port 5432 first. An independent
reviewer on a bare-metal host (rather than the CI container layout) needs to know this before
concluding the commands are broken. Recorded here rather than silently worked around, per Phase
G's own scope boundary (surface gaps for the reviewer, don't quietly fix them) — this is Phase
H's own discovery, not something Phase G should have caught, since Phase G's own evidence was
gathered inside a CI-shaped environment where the default was already correct.

## Recovery results — spot-check, not re-audit

Covered by the same commands above (`test_database_recovery.py`,
`test_recovery_takeover_authority_fencing.py`, `test_operator_lease_effect_time_race.py`): DB
disconnection/restore, lease expiry takeover, and revocation-during-fencing all passed against
this exact head. Did not re-run the full 12-scenario Phase E matrix — this is a rehearsal
sample per the dispatch brief, not a re-audit.

## Outstanding defects

None found in application code. The one finding (reproduction-command port assumption, above)
is a documentation/portability gap in Phase G's package, not a code defect, and is recorded
rather than fixed here since fixing test infrastructure defaults was out of this phase's scope
and risks papering over the real CI-vs-bare-metal distinction a reviewer should know about.

Carried forward, unchanged by this phase: task #61 (founder-wide authorization primitive shape
— still a founder decision, still correctly fail-closed), the cosmetic defang-nesting
rendering issue (Phase 3/G), no lease-revocation-during-in-flight-provider-call test yet
(Phase G), secret-handling verified by manual grep rather than an automated test (Phase G).

## Release recommendation

**Ready for independent review and staging**, with the infra caveat already carried from
Phases C/D/F/G: this sandbox cannot exercise a real local-model response (no Ollama installed,
no outbound network to install or pull one) — every other path up to that exact boundary,
including the real browser and the real worker claim loop, is now independently re-confirmed
fresh rather than inherited from an older commit. Not a blocker for release; it is a sandbox
limitation, not a software defect, and is honestly reported as such rather than faked.

No merge to main, no deploy performed or recommended by this phase.
