# Founder Alpha acceptance commands

These commands run against an isolated local PostgreSQL/Redis/Chromium environment. They never
touch STRATO or production. `first_cycle_go=false`.

## Founder Alpha program runner

`founder_alpha_program.py` is the single executable orchestration over the tools below. It
creates a disposable database, applies the real install order, starts the real API+worker,
runs the 12-step journey, restart/recovery/backup/upgrade/resource/soak/security checks and
the real browser project-detail journey, then drops its databases. The summary is fail-closed:
a short soak and a browser launch failure are BLOCKED, never PASS.

```bash
cd backend
export LD_LIBRARY_PATH=/tmp/lifeai-testenv/root/usr/lib/x86_64-linux-gnu:/tmp/lifeai-testenv/root/usr/lib
python scripts/acceptance/founder_alpha_program.py \
  --admin-url postgresql://postgres@127.0.0.1:55432/postgres \
  --redis-url redis://127.0.0.1:56379/14 \
  --pg-bin /tmp/lifeai-testenv/root/usr/lib/postgresql/16/bin \
  --out-dir ../docs/evidence/founder-alpha/program
```

## Important: pytest owns `DATABASE_URL`

`backend/tests/conftest.py` has a session-scoped autouse fixture that **drops and recreates**
the database named by `DATABASE_URL`, then drops it again at session end. Never point pytest at
the persistent E2E database you want to back up. Use a dedicated test name:

```bash
cd backend
DATABASE_URL=postgresql://postgres@127.0.0.1:55432/lifeos_test_founder_alpha \
APP_DATABASE_URL=postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_test_founder_alpha \
LIFEAI_TEST_DATABASE_NAME=lifeos_test_founder_alpha \
REDIS_URL=redis://127.0.0.1:56379/13 \
PYTHONPATH=. pytest tests/founder_acceptance -q
```

## Fresh install order

Run the same order as the real entrypoint; do not run only `apply_runtime_privileges.py` after
migrations on an empty database:

```bash
ensure_app_role.py -> alembic upgrade head -> apply_runtime_privileges.py -> start API
```

The startup policy is self-healing for the `mainai_job_events`/`mainai_job_proposals` grants, so
a missing broad grant is repaired rather than turning into a failed boot.

## Backend acceptance

```bash
cd backend
PYTHONPATH=. pytest tests/backend/rag/test_library_import.py \
  tests/backend/test_documents_upload_durable_delivery.py \
  tests/backend/jobs/test_worker.py \
  tests/backend/test_db_retry.py \
  tests/backend/providers/test_openai_provider_failclosed.py \
  tests/backend/providers/test_ollama_provider_failclosed.py \
  tests/backend/test_startup_checks.py -q

PYTHONPATH=. pytest tests/backend/test_migration_roundtrip.py tests/backend/test_health.py -q
PYTHONPATH=. pytest tests/backend/rag/test_performance_measurement.py -q -s
```

## Playwright

```bash
cd frontend
E2E_INTERNAL_API_URL=http://127.0.0.1:8010 \
npx playwright test e2e/auth.spec.ts e2e/founder-knowledge-studio.spec.ts \
  e2e/shell-pages.spec.ts e2e/security.spec.ts e2e/account.spec.ts \
  e2e/model-routing.spec.ts --project=chromium
```

`shell-pages.spec.ts` now exercises `/library`; `/documents` is intentionally only a redirect.

## Isolated backup/restore

The source must already contain a document, chunk, conversation and two users. Seed it through
the real API (the E2E harness starts the worker), not through pytest:

```bash
cd backend
python scripts/acceptance/backup_restore_check.py \
  --source-url postgresql://postgres@127.0.0.1:55432/lifeos_e2e \
  --restore-url postgresql://postgres@127.0.0.1:55432/lifeos_restore_check \
  --restore-app-url postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_restore_check \
  --pg-bin /path/to/postgresql/bin \
  --redis-url redis://127.0.0.1:56379/12 \
  --seed-other-user
```

The script refuses to restore over its source, verifies the dump checksum, restores into a new
database, preserves ACLs, compares row counts, checks RLS with two real users, then starts the
API on the restored data and verifies login, documents, conversations, project status and chat.
Add `--out recovery.json` to persist the machine-readable PASS summary used by the QA gate.

## Installation matrix

Runs 12 real-service scenarios: clean install, existing data, invalid app role, unavailable
PostgreSQL, unavailable Redis, missing Ollama, bad model configuration, partial/aborted
migration, failed migration, restart after an aborted install and recovery after a partial
startup. Every scenario checks actual health/revision/grants, not just an exit code.

```bash
cd backend
python scripts/acceptance/install_matrix.py \
  --admin-url postgresql://postgres@127.0.0.1:55432/postgres \
  --existing-url postgresql://postgres@127.0.0.1:55432/lifeos_e2e \
  --existing-app-url postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_e2e \
  --redis-url redis://127.0.0.1:56379/15 \
  --pg-bin /path/to/postgresql/bin \
  --out ../docs/evidence/founder-alpha/install-matrix.json
```

## Resource probe

```bash
python scripts/acceptance/resource_probe.py \
  --database-url postgresql://postgres@127.0.0.1:55432/lifeos_e2e \
  --app-database-url postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_e2e \
  --redis-url redis://127.0.0.1:56379/15 \
  --out ../docs/evidence/founder-alpha/resource-probe.json
```

## QA gate

The gate is fail-closed: missing evidence is BLOCKED, never PASS. It checks the exact SHA, a
clean tracked tree, live PostgreSQL/Redis, Alembic head, the installation matrix, JUnit totals,
Playwright JSON, recovery JSON and performance JSON.

```bash
python scripts/acceptance/qa_gate.py \
  --expected-sha "$(git rev-parse HEAD)" \
  --database-url postgresql://postgres@127.0.0.1:55432/lifeos_e2e \
  --redis-url redis://127.0.0.1:56379/15 \
  --install-evidence ../docs/evidence/founder-alpha/install-matrix.json \
  --performance-evidence ../docs/evidence/founder-alpha/resource-probe.json \
  --recovery-evidence ../docs/evidence/founder-alpha/recovery.json \
  --playwright-json ../docs/evidence/founder-alpha/playwright-resilience.json \
  --pytest-junit ../docs/evidence/founder-alpha/backend-tests.xml \
  --out-dir ../docs/evidence/founder-alpha/qa-gate
```

## Browser E2E and cookies

`localhost` is a secure context in Chromium, so the E2E backend must run with
`COOKIE_SECURE=true`. With `false`, `SameSite=None` cookies are rejected by Chromium and every
protected page redirects to `/login`; this is a harness configuration issue, not a product bug.
Use `CI=1` so Playwright starts a fresh Next server instead of reusing a stale one, and set
`LD_LIBRARY_PATH` to the isolated Chromium library directory.

```bash
cd frontend
CI=1 E2E_FOUNDER_PASSWORD=... E2E_INTERNAL_API_URL=http://127.0.0.1:8010 \
  npx playwright test e2e/founder-alpha-resilience.spec.ts --reporter=json --workers=1
```

## MainAI capability acceptance (de tio grundarkapaciteterna)

`mainai_capability_acceptance.py` mappar de tio kapaciteter grundaren namngav för
Founder Alpha-kandidaten mot de sviter som redan bevisar dem, kör dem per miljönivå och härleder
PASS/FAIL/BLOCKED/NOT_RUN ur JUnit-bevis — samma tolkning som `program_matrix.py` använder, så
att en kapacitetsstatus och en workstream-status aldrig kan säga olika saker.

Det är en orkestrator, inte en andra testsvit: varje test den kör fanns redan. Den skapar inga
egna påståenden och ersätter ingen befintlig grind.

```bash
cd backend
python scripts/acceptance/mainai_capability_acceptance.py --list

python scripts/acceptance/mainai_capability_acceptance.py \
  --database-url postgresql://postgres@127.0.0.1:55432/lifeos_test_capability \
  --app-database-url postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_test_capability \
  --redis-url redis://127.0.0.1:56379/11 \
  --out-dir ../docs/evidence/mainai-capability/local
```

Nivåer (en kapacitet rapporteras per nivå, så "lokalt grönt, modellen otestad" aldrig kan läsas
som PASS):

| nivå | betyder | aktiveras av |
|---|---|---|
| `local` | Postgres + Redis + den begränsade app-rollen | alltid |
| `alpha_merge` | sviter som **bara finns på Founder Alpha-sidan**; tänds automatiskt efter Codex merge | alltid (NOT_RUN tills filen finns) |
| `needs_real_ollama` | riktig Ollama med pinnad chattmodell | `--allow-real-ollama`, och bara om modellen faktiskt är installerad |
| `needs_vps` | 16 GB VPS / produktionsmodell | aldrig lokalt |

Documenterade luckor skrivs ut i rapporten (`gap`), inte förbi dem: en kapacitet kan vara
`PASS` på det som finns och ändå bära en uttalad lucka.

## Founder Alpha merge-invarianter

`alpha_merge_invariants.py` gör varje konfliktdirektiv i
`docs/FREEBUFF_FOUNDER_ALPHA_INTEGRATION_HANDOFF.md` §6 körbart. Ett direktiv i prosa är inte
verifiering: efter Codex konfliktlösning skulle ingenting i repot märka om en sida tyst tappade
den andra sidans garanti.

```bash
cd backend
python scripts/acceptance/alpha_merge_invariants.py \
  --merged origin/scratch/alpha-merge \
  --policy-lock-base 86671468 \
  --out ../docs/evidence/founder-alpha/merge-invariants.json
```

`--merged` krävs för INV-11 (ingen förälders testfil får försvinna i mergen) och
`--policy-lock-base` för INV-12 (POLICY_LOCK är Z.ai:s). På en ogrenad gren är det **förväntat**
att INV-01/03/04/05/06 fallerar: det är exakt de oavgjorda konflikterna, och skriptet finns för
att bevisa att de är lösta — inte för att de ska se gröna ut före merge.
