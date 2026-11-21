# LifeAI V6 — Founder Alpha integration test lab

V6 is an isolated, reproducible acceptance lab for the Founder Alpha journey. It is a
**verifier**, not an activation switch: `first_cycle_go=false` remains unchanged.

The lab never references production containers, volumes, secrets or deployments. It uses a
separate Compose project (`lifeai-v6-testlab`), separate named volumes and loopback-only
ports.

## What is in the lab

| Service | Purpose | Host port |
| --- | --- | --- |
| `backend` | FastAPI API, migrations, acceptance driver | `18000` |
| `worker` | durable execution worker | internal |
| `frontend` | Next.js founder UI and Playwright target | `13000` |
| `postgres` | PostgreSQL 16 + pgvector, isolated test data | `55433` |
| `redis` | Redis/Valkey-compatible queue/cache | `56380` |
| `ollama` | configurable local chat/embedding models | `11435` |

Default local models are `qwen2.5:0.5b` (chat) and `all-minilm` (embedding); both can be
overridden with `OLLAMA_CHAT_MODEL` / `OLLAMA_EMBED_MODEL`.

The controller is `scripts/testlab/testlab.py`. It refuses a conflicting
`COMPOSE_PROJECT_NAME`, only ever uses `docker-compose.testlab.yml`, and `down` removes only
this project's containers and named volumes.

```bash
python scripts/testlab/testlab.py up
python scripts/testlab/testlab.py acceptance
python scripts/testlab/testlab.py soak --duration-seconds 14400
python scripts/testlab/testlab.py gate
python scripts/testlab/testlab.py down
```

`acceptance` writes the 12-step founder journey, runs the Playwright frontend journey, runs
the provider-egress probe and runs the focused R15/RLS JUnit suite into
`delivery/v6-testlab/evidence/`.

## The 12-step founder journey

`backend/scripts/acceptance/v6_founder_journey.py` exercises:

1. founder authentication;
2. protected-route rejection without a session;
3. a chat request through the configured provider;
4. SSE streaming reaching the client;
5. persisted history reopened from the database;
6. document upload and ingestion;
7. a pgvector knowledge query with a citation;
8. a supervised agent task;
9. provider-spend and approval/authorization boundaries;
10. only the explicitly approved isolated operation executing;
11. cancellation through the durable
    `cancel_requested → claimed → cancel_acknowledged → cancelled` state machine;
12. evidence, job state and audit records.

The driver records the exact Git SHA, provider mode, model, per-step results and timings in
machine-readable JSON. A `provider_mode=fake` run is useful for integration wiring but is
**not** local-AI acceptance evidence, and the release gate rejects it.

## Reliability runner

`backend/scripts/acceptance/v6_reliability.py` performs real HTTP interactions and injects:

- backend and worker restart;
- Redis interruption;
- database reconnection;
- Ollama timeout;
- browser/SSE disconnection;
- duplicate job delivery;
- authorization invalidation.

It records latency, throughput, recovery time, process/resource samples and error rates with
the exact Git SHA. A run shorter than `14400` seconds is reported as `F23 BLOCKED`; it is never
promoted to PASS.

The execution-authorization envelope has no TTL column in the current schema. Its only
non-active state is `superseded`, so the authorization-invalidation fault uses that real
transition and records it explicitly rather than inventing an `expired` status that the
database constraint correctly rejects.

## Fail-closed release gate

`backend/scripts/acceptance/v6_release_gate.py` requires:

- a clean tracked tree at the expected SHA;
- `founder-journey.json` with all 12 steps passing and a non-fake provider;
- `reliability.json` with a completed four-hour F23 run and passing fault injections;
- `playwright-founder-v6.json` with zero failures;
- `backend-junit.xml` with passing auth/RLS/security tests;
- `testlab-status.json` with healthy services and a single Alembic head;
- `provider-egress.json` proving no unauthorized provider egress.

Any missing, failed or blocked requirement makes the gate reject. `first_cycle_go` is never
set to true by the gate.

## Actual results in this sandbox

- Founder journey: **12/12 PASS** at base SHA
  `10bbfea3c43a0d81fa1fe5324a72e8ca945c4798` using the deterministic fake provider. This is
  integration proof, not real-local-AI proof.
- Provider-planning regression:
  `test_registry_adapter_defaults_to_active_chat_provider` **1 passed**.
- Reliability: an 8-second host run passed duplicate delivery, browser disconnection and
  authorization invalidation, but is **F23 BLOCKED** because the required duration is four
  hours.
- Playwright: **BLOCKED** at browser launch because Chromium's
  `headless_shell` cannot load `libatk-1.0.so.0`; the sandbox has no root/sudo to install
  Playwright system dependencies.
- Docker Compose lab: **BLOCKED** because the Docker daemon socket is permission-denied.
- Ollama: **BLOCKED** because no local Ollama runtime is installed; only the hosted
  `ollama.com` API is reachable, which is not local-model evidence.
- Therefore the release gate rejects the candidate. No F23/F24 PASS is claimed.

## Genuine defect fixed

`RegistryPlanningAdapter` previously left `provider_name` and `model` empty when a caller
supplied neither, so provider-spend reservation rejected the plan with
`PROVIDER_SPEND_NOT_AUTHORIZED` in a hot retry loop. It now resolves the active chat provider
and model for that owner when both are omitted. Regression coverage is
`backend/tests/backend/mainai/test_provider_assisted_planning.py::test_registry_adapter_defaults_to_active_chat_provider`.

## V7 portability and recovery hardening (2026-11-21)

V7 continues from `56190209cdfb462f2127f4bc04f91fc2b9b5efe3` on branch
`freebuff/v7-testlab-5619020`. It does not rebuild the V6 lab; it makes the existing harness
portable and closes runner defects found by auditing it.

### Portable execution

- `scripts/testlab/preflight.py` checks Python test dependencies, Docker CLI/daemon, Compose
  config, loopback ports, disk, evidence writability, Node/Playwright package, Chromium cache
  and required shared libraries. `--mode host` instead checks a running backend, frontend and
  Ollama endpoint. Missing prerequisites are `BLOCKED` with an exact remediation.
- `scripts/testlab/install_deps.sh` reproducibly installs `backend/requirements-dev.txt`,
  `npm ci` and Playwright Chromium with system dependencies.
- `scripts/testlab/ci.sh` is the unattended path: preflight, isolated `up`, founder
  acceptance, soak, fail-closed gate, and deterministic teardown through an `EXIT` trap.
- `.github/workflows/v7-testlab.yml` is a manual `ubuntu-24.04` workflow with a duration
  input and evidence artifact upload. It uses no production secrets or resources.
- `testlab.py` now supports `preflight`, environment overrides for URLs/evidence, model
  readiness checks, `soak --no-docker`, `all --keep`, complete evidence collection and
  deterministic teardown.

### Genuine runner defects fixed

- The backend image never installed `requirements-dev.txt`, so the in-container JUnit step
  could not run pytest at all. Test dependencies are now opt-in via `INSTALL_TEST_DEPS=true`
  in `docker-compose.testlab.yml`; the production default remains false.
- `/data/storage` is created and owned by `appuser` in the image, so the non-root backend can
  write uploads and the JUnit XML into the named volume.
- `acceptance` used to abort on the first failed leg, leaving no JUnit or egress evidence. It
  now runs journey, Playwright, provider-egress and JUnit independently and reports every
  failed leg.
- F23 previously trusted the configured duration. It now requires actual elapsed time
  `>= 14400`, so an interrupted or replayed run cannot become PASS.
- A Redis pause made `/api/health` return 503; the runner wrongly counted that expected
  degradation as an application failure. It now requires recovery instead.
- Browser disconnects are caught as any `httpx.HTTPError`, not only `ReadError`.
- Ollama recovery is verified through `/api/tags` and a real chat, not just API health.
- Regression contracts: `backend/tests/backend/test_v7_testlab_contract.py` **3 passed**.

### Actual V7 sandbox results

- Docker preflight: **BLOCKED** — `docker_daemon` permission denied on
  `/var/run/docker.sock`; Playwright shared libraries `atk-1.0`, `atk-bridge-2.0`, `gbm` and
  `asound` are missing.
- Host preflight with the isolated test backend/frontend: backend and frontend PASS, Ollama
  unreachable, same Playwright library blocker.
- 12-second host reliability run: duplicate delivery, browser disconnection and authorization
  invalidation **PASS**; **F23 BLOCKED** because 12 seconds is far below 14,400.
- Therefore the Docker lab, browser journey and real local-model evidence remain unavailable
  in this sandbox. F23/F24 are not claimed as PASS.

## Transfer

The branch is `freebuff/v6-testlab-10bbfea`. If GitHub write credentials are unavailable, the
transfer artifact is a verified Git bundle plus patch and `SHA256SUMS` under
`delivery/v6-testlab/`, with the exact `git clone` / `git fetch` commands in its README.
