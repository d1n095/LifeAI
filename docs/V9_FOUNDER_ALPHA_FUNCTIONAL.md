# V9 — Founder Alpha functional completion

**Branch:** `freebuff/v9-founder-alpha-functional-8dafd2f`
**Code checkpoint:** `97b16b7bf28d76b992230c64ef24ff7fd97a3aaf`
**Base:** V8 `8dafd2f5cb12e19a2fa8416cf93ae6b204a0a97d`
**Status:** implemented and verified for the feasible local scope. `first_cycle_go=false`.
**Ownership:** Freebuff owns founder-facing workflows and frontend/backend contracts. Claude
owns integration/R15 reconciliation; Z.ai owns Security Kernel R17–R20 and RAG isolation.
No production deployment, default-branch merge, force-push or autonomous activation was done.

## What changed

### Project detail is now a real, scoped product surface

- Added `GET /api/projects/{project_id}` (`backend/app/routers/projects.py`) returning one
  project, only its own tasks, and only the founder's own non-deleted knowledge sources.
  The query is explicitly owner- and project-scoped; the UI never filters a global library
  response and presents it as project membership.
- Added `ProjectDetailOut` (`backend/app/schemas.py`).
- Added `/projects/[id]` with project knowledge, project tasks, a scoped task composer, and
  an honest MainAI panel. The panel labels goals as global because `mainai_goals` has no
  project foreign key; it does not invent a project association.
- Project links now navigate to the detail page, and task creation can assign a project
  instead of always creating `project_id=NULL`.

### Knowledge and Workbench contracts

- `/knowledge` now uses the attributed hybrid library search (`/api/library/search/hybrid`)
  instead of the raw retrieval endpoint. Every hit links to its real source and displays
  classification, truth status and whether the result came from exact text matching.
- Degraded semantic search is shown explicitly instead of being presented as “no results”.
- Workbench clears a selected document when the project changes and only offers sources
  belonging to the selected project, preventing a stale cross-project document id from
  producing a silently empty analysis.

### Session and supervised-work UX

- `AuthGuard` now distinguishes an expired session (`AuthError`, redirect handled by
  `lib/api.ts`) from a temporary network/backend failure. The latter shows a retryable error
  instead of discarding a valid HttpOnly session and bouncing to `/login`.
- Chat shows the backend-selected active chat provider/model as read-only state, with a link
  to the existing admin configuration when none is available. No second model picker or
  provider bypass was introduced.
- The existing MainAI Execution view now displays a task's dependencies and verification
  plan alongside the real approval, cancellation, checkpoint, wait and event history.

## Verification actually run

| Check | Result |
| --- | --- |
| `tests/backend/test_projects_detail.py` | **3 passed** |
| Focused backend suite: workbench, streaming, library routes, project-status work chain, founder-only auth | **78 passed** |
| `ruff check` on changed backend files | **PASS** |
| `python -m py_compile` on changed backend files | **PASS** |
| Frontend `npm run lint` | **PASS** (0 errors; 1 pre-existing `window.location.href` warning in `lib/api.ts`) |
| Frontend `npm run build` (Next.js 16.4.6) | **PASS**; `/projects/[id]` is generated as a dynamic route |
| Backend E2E harness with isolated PostgreSQL/Redis | **PASS**: `/api/health` 200, worker alive, Alembic `0092` |
| Playwright project-detail journey | **BLOCKED**: Chromium cannot start because `libatk-1.0.so.0` is absent |

The Playwright spec is present in `frontend/e2e/founder-alpha-resilience.spec.ts` and was
executed. It did not reach the application: the browser process exited with
`error while loading shared libraries: libatk-1.0.so.0`. This is reported as BLOCKED, never
as a passing browser test.

## Reproduction

```bash
# Backend regression tests against an isolated PostgreSQL + Redis
cd backend
export DATABASE_URL='postgresql://postgres@127.0.0.1:55432/lifeos_v9_test'
export APP_DATABASE_URL='postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_v9_test'
export REDIS_URL='redis://127.0.0.1:56379/1'
/tmp/r15-venv/bin/python -m pytest tests/backend/test_projects_detail.py -q

# Frontend checks
cd ../frontend
npm run lint
npm run build

# Browser acceptance (requires Playwright system libraries)
E2E_FOUNDER_PASSWORD='TestFounderPassword123!' \
E2E_INTERNAL_API_URL='http://127.0.0.1:8010' \
npx playwright test e2e/founder-alpha-resilience.spec.ts --grep 'project detail' --workers=1
```

The E2E harness uses the existing deterministic provider in
`backend/scripts/ci/run_e2e_backend.py`; it does not call a real model or mail server.

## Remaining blockers

- Playwright Chromium is BLOCKED by missing system libraries (`libatk-1.0.so.0`); the new
  browser journey is written but not browser-executed in this environment.
- Docker daemon access is denied and local Ollama is unavailable, so Docker/Ollama-specific
  acceptance remains BLOCKED.
- V7 still has nine real conflicts and migration heads `0079`/`0092`; Claude owns that
  reconciliation. V9 does not resolve or weaken those checks.
- GitHub write access is absent; no push or draft PR was attempted. The verified bundle is
  the transfer path.

## What this does not claim

- No claim of a fully green browser suite, production deployment, Ollama acceptance, or
  project-scoped MainAI goals. The data model has no `mainai_goals.project_id`, so V9 shows
  the global work chain honestly rather than fabricating project ownership.
