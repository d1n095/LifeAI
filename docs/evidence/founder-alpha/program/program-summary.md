# Founder Alpha engineering program — BLOCKED

Commit: `6601fac3b83d29e6ac755affc2eac3abe1912d2c`
Run: `a5162d9876`

| Check | Status | Detail |
| --- | --- | --- |
| exact commit SHA | **PASS** | {"actual": "6601fac3b83d29e6ac755affc2eac3abe1912d2c", "expected": "6601fac3b83d29e6ac755affc2eac3abe1912d2c"} |
| tracked tree clean | **PASS** | clean |
| pg_dump/pg_restore available | **PASS** | /tmp/lifeai-testenv/root/usr/lib/postgresql/16/bin |
| PostgreSQL reachable | **PASS** | postgresql://postgres@127.0.0.1:55432/postgres |
| Redis reachable | **PASS** | redis://@127.0.0.1:56379/14 |
| Node/npm available | **PASS** | {"node": "v22.23.5", "npm": "10.9.9"} |
| fresh installation order | **PASS** | ensure_app_role -> alembic upgrade head -> apply_runtime_privileges |
| database at Alembic head | **PASS** | current=0092, head=0092 |
| liveness endpoint | **PASS** | {"status":"ok"} |
| readiness endpoint | **PASS** | {"status":"ok"} |
| anonymous protected routes rejected | **PASS** | {"library": 401, "mainai_goals": 401, "agent_tasks": 401, "incidents": 401} |
| wrong password rejected | **PASS** | {"status_code": 401} |
| founder login | **PASS** | {"user_id": "00000000-0000-0000-0000-000000000001", "email": "founder@lifeos.local"} |
| session /me | **PASS** | {"status_code": 200} |
| session refresh | **PASS** | {"status_code": 200} |
| mutating request without CSRF rejected | **PASS** | {"status_code": 403} |
| project create, task and scoped detail | **PASS** | {"project": 200, "task": 200, "detail": 200, "project_id": "49883c13-eb37-478d-bf10-dcdd5b2a3b89"} |
| logout | **PASS** | {"status_code": 200} |
| logout invalidates protected access | **PASS** | {"status_code": 401} |
| 12-step founder journey | **PASS** | {"exit": 0, "status": "PASS", "failures": [], "blockers": [], "provider_mode": "fake"} |
| liveness endpoint | **PASS** | {"status":"ok"} |
| readiness endpoint | **PASS** | {"status":"ok"} |
| restart preserves conversation, source and task evidence | **PASS** | {"conversation": 200, "source": 200, "task": 200, "conversation_ok": true, "source_ok": true, "task_ok": true} |
| backup/restore corrupt and partial rejection | **PASS** | {"exit": 0, "status": "PASS", "checks": [{"check": "valid dump created", "status": "PASS", "detail": "ok"}, {"check": "valid dump has a readable TOC", "status": "PASS", "detail": "ok"}, {"check": "valid dump restores", "status": "PASS", "detail": "ok"}, {"check": "valid restore row counts match", "s |
| backup restore into disposable database and API verification | **PASS** | {"exit": 0, "status": "PASS", "summary": null, "stderr_tail": ""} |
| migration upgrade / rollback / re-upgrade | **PASS** | {"steps": [{"step": "upgrade_head", "exit_code": 0, "current": "0092"}, {"step": "downgrade_one", "exit_code": 0, "current": "0091"}, {"step": "upgrade_head_again", "exit_code": 0, "current": "0092"}], "final_head": "0092"} |
| resource and latency probe | **PASS** | {"exit": 0, "status": "PASS", "latencies": {"/api/documents": 14.74, "/api/conversations": 9.73, "/api/mainai/project-status": 85.71}, "blockers": []} |
| endurance soak | **BLOCKED** | {"exit": 3, "status": "BLOCKED", "duration_seconds": 20, "required_seconds": 86400, "f23_status": "BLOCKED", "blockers": ["duration_seconds=20 < required 14400; F23 cannot be claimed from this run"], "failures": []} |
| security, health and regression tests | **PASS** | {"exit": 0, "totals": {"tests": 59, "failures": 0, "errors": 0, "skipped": 0}, "tail": " objects to represent datetimes in UTC: datetime.datetime.now(datetime.UTC).\n    deleted_at=datetime.utcnow() if deleted else None,\n\n-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html\n- g |
| liveness endpoint | **PASS** | {"status":"ok"} |
| readiness endpoint | **PASS** | {"status":"ok"} |
| browser acceptance | **PASS** | {"exit": 0, "tail": "                  \"annotations\": [],\n                  \"expectedStatus\": \"passed\",\n                  \"projectId\": \"chromium\",\n                  \"projectName\": \"chromium\",\n                  \"results\": [\n                    {\n                      \"workerInd |

## Evidence

- `program-summary.json`
- `run-a5162d9876/founder-journey.json`
- `run-a5162d9876/recovery-matrix.json`
- `run-a5162d9876/backup-restore.json`
- `run-a5162d9876/resource-probe.json`
- `run-a5162d9876/soak.json`
- `run-a5162d9876/backend-tests.xml`

## Limitations

- The founder journey and API checks use the existing deterministic E2E provider; real Ollama is not available.
- A soak shorter than 24 hours is BLOCKED for endurance, never PASS.
- Browser acceptance is BLOCKED when Chromium cannot load its system libraries.
- Docker daemon access is unavailable; no Docker/Compose lab was started.
