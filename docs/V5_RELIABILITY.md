# LifeAI V5 — Production Reliability & Self-Healing

Status: implementation and focused verification complete. F23 remains **BLOCKED** because no
four-hour run has been executed. No production deployment, default-branch merge, force-push or
change to another agent's branch was performed. `first_cycle_go=false` is unchanged.

Branch: `freebuff/v5-reliability-2265a0e`
Base: `2265a0e5d62d678bb762f520dbf5848a66020387` (V4 Founder Alpha)

## What changed

### 1. Active runtime-health detection

`backend/app/incidents/health.py` adds a bounded sweep that reuses the existing runtime-incident
table and worker poll loop:

- detects `running` MainAI jobs and import jobs with an expired or missing lease;
- records durable, deduplicated incidents with a correlation id and safe structured details;
- requeues only idempotent internal job types: `corpus_review`, `message_sequence_backfill`,
  `message_source_backfill`;
- never blindly requeues `task_execution`, because its external effect may be unknown;
- marks an internal job `failed` when its existing retry budget is exhausted;
- surfaces stale queued work without claiming it;
- leaves import jobs for the existing importer reclaimer, but makes the stall visible.

The worker calls the sweep once per poll cycle and commits its own transaction. A sweep failure
rolls back and is logged; it cannot stop the poll loop.

### 2. Retry accounting and lease fencing

`claim_next_mainai_job()` now increments `retry_count` when it reclaims an expired `running` job
and excludes jobs whose retry budget is already exhausted. `lease_generation` still fences every
later write, so a stale worker cannot continue after another worker has reclaimed the job.

### 3. Provider circuit breaker

`backend/app/providers/circuit_breaker.py` adds a small Redis-backed breaker keyed by a hash of
provider/model. It stores only counters and an open marker, never prompts, replies, credentials
or raw provider errors. Five failures within 60 seconds open the circuit for 120 seconds. Both
the JSON and streaming fallback paths skip an open provider and report `circuit_open` as a
retryable category. Redis failure fails open: chat availability is never taken down by the
breaker store.

### 4. Streaming lifecycle fix

The streaming attempt previously used the request-scoped SQLAlchemy session after FastAPI had
already returned the `StreamingResponse`. In a real uvicorn run the dependency cleanup could
close that session first, producing:

```
InvalidRequestError: Instance '<Message ...>' is not persistent within this Session
```

The background attempt and the interrupted-stream cleanup now own fresh `SessionLocal`
sessions, set the same owner RLS context, and close them in `finally`. A disconnect or
unexpected failure still marks an invoked external effect as `external_effect_unresolved`,
records an incident, and blocks a blind retry.

### 5. Owner-scoped health API and UI

`GET /api/incidents/health` returns `healthy`, `recovered`, `degraded` or `blocked`, open
incident counts by severity, stalled job/import counts, worker heartbeat, and per-component
state. It is founder-gated and owner-scoped. The incidents page renders the same state and
keeps the existing local-only/no-telemetry-upload behavior.

## Verification

| Check | Result |
| --- | --- |
| V5 reliability + fault-injection tests | **23 passed** |
| Focused streaming/incident/jobs regression | **32 passed** |
| Security/R15 and MainAI recovery suites | **158 passed** |
| Frontend production build | **PASS** |
| Real PostgreSQL fault matrix (F13 subset) | **6/6 PASS** |
| Soak harness validation, 8 seconds | **no request failures; F23 BLOCKED by duration** |
| Four-hour soak (F23) | **BLOCKED — not run** |

The PostgreSQL matrix covers rollback, concurrent deadlock, lock timeout, statement timeout,
terminated backend plus reconnect, and concurrent writes.

The soak harness is `backend/scripts/acceptance/v5_soak.py`. It starts the real E2E backend and
durable worker against an isolated local database/Redis, exercises health, incident health, JSON
chat, SSE streaming, document import/retrieval, MainAI job create/cancel, concurrency and
optional restarts, and records latency, process, database and Redis metrics. It intentionally
reports `BLOCKED` for any run shorter than 14,400 seconds.

Example (isolated local services only):

```bash
cd backend
PYTHONPATH=. python scripts/acceptance/v5_soak.py \
  --database-url postgresql://postgres@127.0.0.1:55432/lifeos_v5_soak \
  --app-database-url postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_v5_soak \
  --redis-url redis://127.0.0.1:56379/13 \
  --duration-seconds 14400 \
  --out ../delivery/founder-alpha-v5-reliability/evidence/soak.json
```

## Unresolved issues / blockers

1. **F23 four-hour soak is BLOCKED.** The harness is executable and a short validation produced
   zero request failures, but that is not four-hour evidence.
2. **F24 inherits F23.** No full QA gate claim is made.
3. **GitHub push / draft PR is blocked.** No `gh`, token or usable SSH credential is available
   in this environment. The verified branch bundle and patch are the transfer artifact.
4. **Exact-head GitHub Actions is blocked** for the same reason; the workflow also does not
   dispatch on this branch/PR base.
5. **Real Ollama/pgvector acceptance was not rerun here.** The local health probe cannot reach
   Ollama. Existing successful Ollama evidence is not treated as a failure.
6. No schema migration was added; health and breaker state use existing tables/Redis.

## Security invariants preserved

- Authentication and founder authorization are unchanged; the health endpoint uses the existing
  `require_founder` dependency.
- Owner/tenant isolation and RLS remain the enforcement boundary; health queries are explicitly
  owner-scoped.
- Incident details pass through the existing allowlist sanitizer; prompts, replies, documents,
  credentials, tokens and raw provider payloads are dropped.
- `LOCAL_ONLY_MODE`, Life Vault egress controls, worker leases/cancellation, provider budgets and
  retry accounting are not bypassed.
- No automatic telemetry transfer or external diagnostics upload was added.
