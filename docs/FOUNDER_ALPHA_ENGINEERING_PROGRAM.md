# Founder Alpha engineering program

**Branch:** `freebuff/founder-alpha-engineering-program`
**Base:** V9 `6b3848d96e99926d2d2175f03582f86c0947e906`
**Candidate code SHA:** `6601fac3b83d29e6ac755affc2eac3abe1912d2c`
**Status:** all executable workstreams implemented and verified; overall result **BLOCKED** only
because the required 24-hour endurance run was not possible in this environment.
`first_cycle_go=false`.

## What this program adds

### Operational liveness and readiness

`backend/app/routers/health.py` now exposes two distinct contracts without changing the
existing `/api/health` payload:

- `GET /api/health/live` answers process liveness only. It deliberately performs no database,
  Redis or model probe, so a dependency outage cannot cause an orchestrator to restart a
  healthy API process.
- `GET /api/health/ready` checks the database and a configured Redis and fails closed with a
  generic `503 {"status": "unavailable"}` body. No connection strings or exception details
  are exposed.

`backend/tests/backend/test_health.py` now covers the split and the fail-closed readiness
cases. The existing `/api/health` behavior, worker heartbeat and local-model reporting remain
unchanged.

### Executable program runner

`backend/scripts/acceptance/founder_alpha_program.py` orchestrates the existing V5–V9 tools
instead of creating a second test framework. It:

1. validates the exact commit SHA, clean tracked tree, PostgreSQL, Redis, PostgreSQL client
   tools and Node/npm;
2. creates a disposable `lifeos_program_*` database, installs the `vector` extension and runs
   `ensure_app_role -> alembic upgrade head -> apply_runtime_privileges`;
3. starts the real API and durable worker through `scripts/ci/run_e2e_backend.py`;
4. runs the 12-step founder journey against the real HTTP API and database;
5. verifies login, `/me`, refresh, CSRF rejection, project/task creation and scoped project
   detail, logout and post-logout rejection;
6. restarts the backend and reopens the persisted conversation, indexed source and supervised
   task events;
7. runs the existing recovery matrix, backup/restore check, migration rollback/re-upgrade,
   resource probe and a short soak;
8. runs security/health/startup regression tests in a separate pytest database;
9. runs the real Playwright project-detail journey against a browser backend with secure
   cookies;
10. writes machine-readable and human-readable summaries and drops its databases.

The browser leg uses a separate short-lived backend with `COOKIE_SECURE=true`, because
Chromium treats localhost as a secure context and rejects the session cookie when the local
backend sets `COOKIE_SECURE=false`. The API checks keep the non-secure local setting so httpx
can use the same session over plain HTTP.

### Harness correctness fixes

- `resource_probe.py` and `v5_soak.py` used an obvious placeholder provider key, which
  `app.providers.base` deliberately treats as unconfigured. They now use a non-placeholder
  local-only key so the deterministic E2E provider is actually selected.
- The program runner stops its main backend before probes that start their own worker,
  preventing two workers from racing for the same import jobs.

No authorization, RLS, Security Kernel, migration chain or production data was changed.

## Workstream results

| Workstream | Implementation / evidence | Result |
| --- | --- | --- |
| 1. Reproducible environment | Fresh disposable DB, pgvector, real migrations `0092`, runtime grants, seeded founder, API+worker, teardown | **PASS** |
| 2. Real application lifecycle | 12/12 founder journey: auth, SSE, persistence, ingestion, cited retrieval, project task, approval/envelope, execution, cancel, audit | **PASS** |
| 3. Persistent state and restart | Backend restart preserved conversation, indexed source and task events; recovery matrix valid/corrupt/partial restores | **PASS** |
| 4. Operational reliability | `/live`, `/ready`, fail-closed startup, 59 security/health/startup/db-retry tests, resource and queue samples | **PASS** |
| 5. Capacity and endurance | Resource probe PASS; 20-second soak had no request failures but is **BLOCKED** for the 24-hour requirement | **BLOCKED** |
| 6. Upgrade, migration and recovery | `0092 -> 0091 -> 0092` rollback/re-upgrade; backup/restore into disposable DB and API verification; install matrix **12/12** | **PASS** |
| 7. Browser and API acceptance | Playwright project-detail journey: **1 expected, 0 unexpected, 0 flaky**; frontend lint/build PASS | **PASS** |
| 8. Security preservation | Anonymous routes 401, wrong password 401, missing CSRF 403, logout invalidates access, RLS/security/startup regressions 59 passed | **PASS** |
| 9. Release rehearsal | Exact-SHA clean-tree run, fresh install, founder acceptance, restart, recovery, upgrade, backup/restore, security regressions, browser | **PASS** (with the workstream-5 endurance blocker) |

## Exact measured results

| Check | Result |
| --- | --- |
| Program runner checks | **31 PASS, 0 FAIL, 1 BLOCKED** (32 total) |
| Founder journey | **12/12 PASS**, deterministic provider `openai/gpt-4o-mini`, provider mode `fake` |
| Backend regression/security tests | **59 passed, 0 failed, 0 errors, 0 skipped** |
| Install matrix | **12 PASS, 0 FAIL, 0 BLOCKED** |
| Browser project-detail journey | **1 expected, 0 unexpected, 0 flaky** |
| Recovery matrix | **PASS**; valid restore matches row counts; corrupt and partial dumps rejected; source unchanged |
| Backup/restore with API verification | **PASS** |
| Migration rollback/re-upgrade | **PASS**: `0092 -> 0091 -> 0092` |
| Resource probe | **PASS**; documents 14.74 ms, conversations 9.73 ms, project status 85.71 ms |
| Short soak | **BLOCKED** for endurance; 20 s, zero request failures, F23 cannot be claimed below 24 h |
| Frontend lint | **PASS**, 0 errors, 1 pre-existing `window.location.href` warning |
| Frontend production build | **PASS**; `/projects/[id]` dynamic route generated |

The founder journey artifacts include a persisted conversation with 2 messages, an indexed
source with 1 chunk, a cited retrieval, a completed `read_only_audit` task with 6 events, an
explicit execution envelope and provider-spend authorization, a cancelled durable job with
`cancel_acknowledged`, and 12 audit rows.

## Evidence

All evidence is under `docs/evidence/founder-alpha/program/`:

- `program-summary.json` and `program-summary.md` — the exact program result.
- `run-a5162d9876/founder-journey.json` — 12-step journey.
- `run-a5162d9876/recovery-matrix.json` — valid/corrupt/partial restore matrix.
- `run-a5162d9876/backup-restore.json` — backup/restore API verification.
- `run-a5162d9876/resource-probe.json` — measured latency/resource data.
- `run-a5162d9876/soak.json` — short soak and explicit F23 blocker.
- `run-a5162d9876/backend-tests.xml` — 59-test JUnit.
- `install-matrix.json` — 12/12 real-service installation matrix.
- `frontend-lint.txt` and `frontend-build.txt` — frontend checks.

## Reproduction

```bash
cd backend
export LD_LIBRARY_PATH=/tmp/lifeai-testenv/root/usr/lib/x86_64-linux-gnu:/tmp/lifeai-testenv/root/usr/lib
python scripts/acceptance/founder_alpha_program.py \
  --admin-url postgresql://postgres@127.0.0.1:55432/postgres \
  --redis-url redis://127.0.0.1:56379/14 \
  --pg-bin /tmp/lifeai-testenv/root/usr/lib/postgresql/16/bin \
  --out-dir ../docs/evidence/founder-alpha/program \
  --soak-seconds 20
```

A real 24-hour endurance run is the only way to turn workstream 5 from **BLOCKED** into
**PASS**; a short smoke is never relabeled as endurance.

## Remaining blockers

- **24-hour endurance:** not run. The local environment cannot supply a persistent isolated
  host for 24 hours, so F23 remains BLOCKED. The 20-second soak is only a smoke.
- **Real Ollama:** unavailable. The journey uses the existing deterministic E2E provider;
  real local-model acceptance is not claimed.
- **Docker:** daemon access is denied, so the Compose lab and Docker fault injection were not
  started.
- **V7/R15 integration:** nine conflicts and migration heads `0079`/`0092` remain
  Claude-owned. This program does not resolve or weaken them.
- **GitHub write access:** absent. No push, PR, merge, deployment or production change was
  made.

## Safety

No production deployment, default-branch merge, force-push, migration change, autonomous
activation or Security Kernel/RAG-isolation change was performed. All databases used by the
program were disposable and dropped at the end.
