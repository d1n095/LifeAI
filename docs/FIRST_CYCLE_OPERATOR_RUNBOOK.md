# First-cycle operator runbook (Founder-supervised, local-only)

Companion to `docs/FIRST_CYCLE_CODEX_INTEGRATION.md`. This is **not** activation, merge, or GO.

Frozen parent: `#288` @ `1ea41d7db424cba6b4d9ec93da79fa85d809d2fb`.
Parent `#289` exact SHA: `ca60ba54febb49e1c7585fb776811564d54e3d21` (freeze after 12 required checks).
This successor: `cursor/first-cycle-execution-hardening-p3-91c1`.

## Hard rules

- Synthetic providers only. No live paid API calls.
- `github_write_enabled` stays **false**. No push, open_pr against GitHub, merge, or deploy.
- No Recall. No Track B. Do not duplicate Codex transport or grants.
- First-cycle GO stays **false** until Codex transport/grants and a PASS-reviewed activation-security replacement compose independently.

## Prerequisites

Local Postgres as used by backend tests. From `backend/`:

```sh
test -x .venv/bin/python
.venv/bin/python -c "import app; print('ok')"
```

Expected: `ok`. If the venv is missing, recreate it from `backend/requirements.txt` — do not point at production.

Override the test DB if needed (conftest defaults may use 5433):

```sh
export LIFEAI_TEST_DATABASE_NAME=lifeos
export DATABASE_URL=postgresql://lifeos@localhost:5432/lifeos
```

## 1. Show current state / blockers / authorized actions / costs

The operator snapshot is produced by the production-path dry run (section 3). Independently:

```sh
cd backend
.venv/bin/python -m pytest tests/backend/test_first_cycle_ops_readiness_p2.py::test_founder_supervised_first_cycle_dry_run_stops_before_merge -q
```

Expected: `passed`. The test drives `snapshot_first_cycle()` before work and after STOP.

A passing snapshot has:

- `go: false`
- `merged: false`
- `github_write_enabled: false`
- `costs.live_paid_calls: false`
- `blockers` containing `first_cycle_stop` after STOP
- `authorized_actions` containing `founder_review_only` after STOP (never `merge`)
- every audit row has a 64-hex `evidence_sha`; local verification has a 40-hex git SHA

## 2. Explicit Founder approval for privileged actions

Privileged `repo_edit` in the dry run is `approval_required=True`. Dispatch without `approval_granted` raises `ApprovalRequiredError` and creates **no** `mainai_jobs` row.

Recovery of a Founder-cancelled dead job must **not** takeover. Verify:

```sh
cd backend
.venv/bin/python -m pytest \
  tests/backend/test_first_cycle_ops_readiness_p2.py::test_takeover_refuses_founder_cancelled_dead_job \
  tests/backend/test_first_cycle_ops_readiness_p2.py::test_auto_recovery_finalizes_cancelled_dead_job_without_takeover \
  -q
```

Expected: both `passed`. Task lands `cancelled`, job lands `cancelled`, no new dispatch.

## 3. Deterministic end-to-end dry run (synthetic provider, local effects)

```sh
cd backend
.venv/bin/python -m pytest \
  tests/backend/test_first_cycle_ops_readiness_p2.py::test_founder_supervised_first_cycle_dry_run_stops_before_merge \
  -q
```

Expected stages, in order:

1. `goal`
2. `clear_state`
3. `proposed_task`
4. `approval_required`
5. `founder_approval`
6. `dispatch`
7. `local_verification`
8. `STOP`

Expected result: goal `blocked`, `first_cycle_state=stop_before_merge`, `merged=false`, `go=false`, 40-hex `verification_sha`. No GitHub network, no paid provider.

Optional wrapper (same test, refuses unless local-only flags are set):

```sh
cd backend
.venv/bin/python scripts/mainai/first_cycle_local_dry_run.py
```

Expected stdout includes `STOP-before-merge` and `go=false`.

## 4. STOP / cancel / interrupted-job recovery

Retry after STOP must park, not redispatch:

```sh
cd backend
.venv/bin/python -m pytest \
  tests/backend/test_first_cycle_ops_readiness_p2.py::test_retry_tick_parks_due_task_on_stopped_goal \
  -q
```

Expected: task `blocked`, `next_retry_at` is null.

If a running job is interrupt-cancelled (Founder `cancel_requested`, lease expired): auto-recovery finalizes `cancelled` and does not mint a takeover job. Do **not** manually `POST /tasks/{id}/recover` to bypass that.

## 5. Exact-SHA audit trail

Every operator stage writes `MainAITaskEvent` `progress_updated` with `kind=first_cycle_ops_audit`, `evidence_sha` (SHA-256 of the canonical payload) and `local_head_sha` when git HEAD exists.

Do not treat `evidence_sha` as a Git commit. The local verification commit SHA is `verification_sha` / `local_head_sha` (40 hex).

## 6. Safe recovery procedures

| Situation | Safe action | Forbidden |
|---|---|---|
| Dry-run test failed mid-dispatch | Re-run the pytest; it uses a throwaway `tmp_path` git repo | Enabling `GITHUB_WRITE_ENABLED` |
| Task `retryable_failed` on a STOP goal | Leave it; worker parks it `blocked` | Manual retry / takeover |
| Job `cancel_requested` and lease expired | Let auto-recovery finalize `cancelled` | `execute_takeover` / `/recover` |
| Want to inspect after a local pytest | Read the test assertion / SQL on the test DB | Pushing the isolated branch |
| Codex transport still missing | Keep CRED-1/2/3, E2E-3, GRANT-PROD FAIL/BLOCKED | Implementing a second grant/transport |
| Staging JSON still says `go: false` | Correct — do not flip it | Claiming GO from this runbook |
| GitHub 5xx / `git fetch` failure during recovery | Leave the recovery record blocked; no paid replan | Treating it as `LOCAL_COMMITTED_NOT_PUSHED` |
| Second dead job after one auto-takeover | Leave the task `blocked` (FO-6 cap) | Another `execute_takeover` |

Founder-visible diagnostics are on `snapshot_first_cycle()["founder_diagnostics"]` (`go: false`, no tokens).

## Remaining blockers (not GO)

- CRED-1/2/3, E2E-3, GRANT-PROD (Codex Git transport / grants)
- TA-12/TA-13/PUB-2 production publication oracle
- CI-6 skipped-as-pass
- `#280` RETURN_TO_BUILDER dual Alembic 0089
