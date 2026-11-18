# Founder Alpha — operational readiness record

State of the private-start foundation on `freebuff/founder-alpha-ops-recovery-20261118`
(draft PR #461). Frozen PRs #459 (`dd69cb3`) and #460 (`d2d2ccc`) were not touched, nothing was
merged, nothing was deployed, and `first_cycle_go` remains `false`.

This record separates three things that are easy to blur: what is **verified by executable
evidence**, what is **prepared but not enforceable without repository administration**, and what is
**not verified at all**.

## Runtime reliability (M4)

| Property | Evidence | Status |
|---|---|---|
| Lease expiry judged by the database's own clock, in the columns' own type domain | `test_lease_clock_domain.py` (4 tests, incl. a `+3h` and `−3h` app-clock skew) | verified |
| At most one worker executes a job; every write re-proves the lease generation | `test_mainai_execution_checkpoint_fencing.py`, `test_runtime_failure_recovery.py::test_a_worker_that_lost_its_lease_never_reaches_the_push` | verified |
| An expired `task_execution` lease is never blind-reclaimed (no duplicate privileged execution) | `test_runtime_failure_recovery.py::test_an_expired_task_execution_lease_is_never_handed_to_a_second_worker` | verified |
| A lost database connection leaves a recoverable attempt, not a fabricated outcome | `test_runtime_failure_recovery.py::test_a_lost_database_connection_leaves_a_recoverable_attempt_not_a_false_outcome` | verified |
| Untrusted AI edits never touch the live checkout | `test_proposal_worktree_isolation.py` (6 tests), `test_proposal_mode_checkout_restoration.py` (3 tests, PI-1d) | verified |
| A hung test fails with a named stack instead of hanging the suite | `pytest.ini` (faulthandler + pytest-timeout), `conftest._clean_tables` lock_timeout, CI job cap | verified |

### Why the suite hung, and what was actually wrong

Two CI runs stalled for over an hour with no failing test. A faulthandler dump from a local
reproduction showed the exact frame: `_clean_tables`' `TRUNCATE` waiting inside
`pytest_fixture_setup` behind a row lock left open by a *previous* test. `TRUNCATE` needs
`ACCESS EXCLUSIVE`, so a single leaked transaction stops the whole suite silently.

The leak had a cause, and it was a defect introduced by the clock fix itself: `db_now()` returned
`clock_timestamp()` (`timestamptz`) while every column it judged is `timestamp without time zone`.
Comparing the two raises `TypeError`, which crashed the soak tests mid-transaction and left the
lock. Both helpers now return naive UTC in the columns' own type domain. Three things were fixed so
this class cannot return silently: the type domain, a bounded cleanup wait that reports the leak by
name, and fixtures that always roll back and close.

## Security controls (M5)

`docs/SECURITY_CONTROLS.md` is the control matrix. In short, on every push and PR the `Security`
workflow runs Semgrep (project rules, validated in CI and tested against a deliberately vulnerable
sample), gitleaks, OSV-Scanner, policy-integrity (SHA-256 lock over every control file, failing on
*newly added* control files too), and provenance; a final gate requires `success` from all five and
fails on `cancelled`/`skipped`. No workflow contains `continue-on-error`.

Not enforced, and not claimed:

* **CodeQL** — private repositories need GitHub Advanced Security. Prepared; the job is skipped
  until an administrator enables GHAS and sets `CODEQL_ENABLED=true`, and the gate prints
  `NOT ENFORCED` rather than green.
* **CODEOWNERS review requirement** — needs branch protection (administrator action).
* **The Security gate as a required status check** — needs branch protection.

What the gate found on its first runs was real: eight advisories in `python-multipart` (upgraded),
115 in `pypdf` (upgraded to 7.1.1), 32 in `PyJWT` (upgraded to 2.15.0), and advisories in
`postcss`/`sharp` (patched versions). Two dev-only transitive advisories and one dev-only `pytest`
advisory are recorded in `osv-scanner.toml` with reasons and hard expiry dates.

## Requirements and seams (M6)

`requirements/registry.json` holds 19 requirements — 15 mandatory before the private start, 2
necessary for safe autonomy, 1 production, 1 deferred — each with its source, responsible module,
dependencies, implementation evidence, integration evidence, security implications, test coverage,
status and gaps. `verify_registry.py` fails closed on a dangling dependency, a cycle, a missing
module or test, a symbol that does not exist, a `verified` claim with no test, or an unverified
entry with no stated gap. It runs in CI (`Requirements — registry, seam scan, verifiers`).

Confirmed dead seams (reported, deliberately not "fixed" by inventing callers): 67 unreferenced
module-level functions, including whole subsystems — `app/problem_learning/*` and
`app/mainai_executive/*` — with no router or worker entry point. These are real integration seams
and are listed in the CI artifact `dead-seams.json`. The one seam rule strong enough to enforce is
enforced: zero configuration flags that nothing reads.

## Acceptance (M7)

`backend/scripts/acceptance/founder_alpha_acceptance.py` runs the startup path and labels every
check with its evidence kind. Run locally against a freshly migrated PostgreSQL and Redis:
configuration, PostgreSQL, migration head, Redis, policy lock, capability manifest, registry and
seam scan pass with real evidence; the local model is reported `not_verified` with the reason.

## What is NOT verified

* **Local model deployment.** Protocol integration to a real Ollama server is exercised by
  `.github/workflows/real-local-ai-verification.yml`; the founder's inference gas and hardware are
  not deployed or verified here. This is a requirement gap in the registry, not a footnote.
* **Retrieval quality** on the founder's real corpus (pipeline correctness is tested; answer
  quality is a product judgement).
* **Real GitHub push/PR path** (`GITHUB_WRITE_ENABLED=true`): the isolated-worktree path is tested
  against real local git, but no test performs a real push to the remote, by design — that would be
  a deployment action.
* **Off-host backup retention** — the backup/restore mechanism is verified; the destination and
  retention policy are founder decisions.

## To start privately

1. Set real values for `SECRET_KEY`, `FOUNDER_EMAIL`, `FOUNDER_PASSWORD`; keep
   `GITHUB_WRITE_ENABLED=false` so MainAI stays in proposal mode.
2. Apply the administrator actions in `docs/SECURITY_CONTROLS.md` (code-owner review, required
   Security gate, optionally GHAS for CodeQL).
3. Run the acceptance harness against the target database and keep its `acceptance-report.json`
   with the release record.
4. `first_cycle_go` stays `false` until the founder authorizes it — this branch does not flip it.
