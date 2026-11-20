# Founder Alpha acceptance commands

These commands run against an isolated local PostgreSQL/Redis/Chromium environment. They never
touch STRATO or production. `first_cycle_go=false`.

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
