# LifeAI Founder Alpha — handover

This document is for you, the founder, not for another engineering session. It tells you
exactly what has been verified, what hasn't, and what you need to do before this is yours to
use day-to-day.

## 1. Exact SHA

Canonical candidate: `e9fcd73cf14e73b69b0049896fffd619d15d22f7`
(branch `claude/founder-alpha-ws1-canonical`, base `integration/founder-alpha-20261121`).

This is not merged to `main` and nothing has been deployed anywhere. It is the tip of a long
chain of draft PRs (#470 through #496) that each verified and built on the one before it.

## 2. Reproducible installation

**Docker (recommended):**
```bash
cp .env.example .env
# open .env and fill in at least one API key (e.g. OPENAI_API_KEY)
docker compose up --build
```
This starts Postgres+pgvector (`localhost:5432`), Redis (`localhost:6379`), the backend
(`localhost:8000`, Swagger at `/docs`), the worker (the *only* path that actually indexes
documents or executes MainAI jobs — without it, uploads are accepted but never processed),
and the frontend (`localhost:3000`).

To also run a local model with no cloud cost:
```bash
docker compose --profile local-llm up --build
docker exec -it lifeai-ollama-1 ollama pull llama3.1
```
Then set the `ollama` provider active for chat and/or embedding in the admin panel
(`localhost:3000/admin`).

**Without Docker** (requires a running Postgres+pgvector and Redis, e.g. via
`docker compose up postgres redis`):
```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example .env   # point DATABASE_URL/APP_DATABASE_URL/REDIS_URL at localhost

DATABASE_URL=... APP_DATABASE_URL=... python scripts/setup/bootstrap_database.py
uvicorn app.main:app --reload
# separate terminal:
python -m app.worker
```
```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

The `bootstrap_database.py` step is not optional — it sets up the database role, runs
migrations, and applies the runtime privilege policy in the correct order (the same sequence
`backend/docker-entrypoint.sh` runs inside a container). A failing `uvicorn` startup almost
always means this step was skipped, not an application bug.

You log in with the single founder account created automatically on first boot from
`FOUNDER_EMAIL`/`FOUNDER_PASSWORD` in `.env` — see section 9.

## 3. Verified running services

| Service | Status | Evidence |
|---|---|---|
| Backend (FastAPI) | Verified | Full regression suite, 3382 passed (PR #496) |
| Worker | Verified | Real claim-loop exercised repeatedly across this effort; a missing worker entrypoint in the local test harness was found and fixed in PR #494 (did not affect the real `app/worker.py` used by `docker compose up`, only the harness) |
| Frontend (Next.js) | Verified | `next build` 24/24 routes, typecheck clean, real-browser Playwright runs across multiple phases |
| PostgreSQL + pgvector | Verified | Fresh-install and upgrade-from-prior-state migration tests both pass; single migration head, 89 revisions, no duplicates or orphaned links |
| Redis | Verified | Recovery-from-Redis-outage test added and passing (a real blip-and-recover scenario, not just connection presence) |

## 4. Real-model acceptance evidence — read this precisely

There is **one** piece of genuine, non-mocked, live-model evidence in this entire integration
effort: GitHub Actions workflow run #14 on commit `eddddf53`, using the project's own
`real-local-ai-verification.yml` workflow (a real GitHub-hosted runner with real internet
access, separate from the sandbox this engineering work ran in). It:
- Installed real Ollama, pulled real `qwen2.5:0.5b` (chat) and `all-minilm` (embeddings).
- Ran a real streaming conversation: 81 incremental delta events over the wire, a real
  248-character model-generated reply.
- Ran a real document-upload → embed → retrieve → cited-answer cycle.
- Confirmed recovery after an abandoned connection.

**What this does and doesn't mean**: this proves the code path works correctly against a real
model. It does **not** mean the application has been run with a real model in an interactive
session, by a person, in a browser. That has never happened in this effort — every browser-based
acceptance pass used a mocked provider transport, consistently and honestly labeled as such.
Before you rely on this day-to-day, run it yourself once, interactively, with a real model.

## 5. Security and recovery — what was actually found and fixed

Full regression at the canonical head: **3382 passed, 18 skipped (all pre-existing/explained
infrastructure boundaries), 1 failed** — the one failure is the policy-lock check, explained in
section 7(a); it is not a code defect.

Across this engineering effort, real defects were found by actually running the application
(not just unit tests) and fixed — these are the ones worth knowing were real risks, not a full
changelog:

- A lease could be lost mid-write in a fallback execution path without the write being
  discarded — fixed; a worker without a valid lease can no longer commit a repo-edit proposal.
- A total AI-provider failure during a supervised-task plan proposal left the task stuck
  `running` forever instead of failing cleanly — fixed; it now fails closed with a clean error
  and the goal stays in a sane, retryable state.
- Any non-Ollama provider's streaming path could leave a reply **permanently stuck** on an
  in-flight marker if the underlying connection failed — every retry would then be rejected
  forever. Fixed and verified live (reproduced the stuck state, applied the fix, confirmed a
  clean error and a working retry).
- A missing parameter in the embedding call's error handling silently blocked **all** document
  indexing in the local test harness, misclassified as a generic error — fixed; this was caught
  only by actually running the document-upload journey end to end, not by any existing test.
- A reopened conversation whose response had been interrupted mid-stream silently displayed the
  truncated text as if it were the complete answer, with no way to retry — fixed; interrupted
  messages now carry their real status and offer a working retry.

## 6. Resource requirements

The real production VPS has ~2GB RAM. Its own `docker-compose.vps.yml` already budgets
1664MB of `mem_limit` (caddy 128m, backend 512m, worker 384m, frontend 512m, redis 128m) with
no local Postgres (Supabase-hosted) and no local model runtime — leaving no room to run a
second, colocated staging stack on the same box.

**Recommendation for staging: a separate, dedicated small instance, using a remote Ollama
endpoint rather than bundling model weights on the staging host.** This mirrors how production
already keeps Postgres off the thin app host. A ready-to-use (but unprovisioned — see below)
config exists: `docker-compose.staging.yml`, `Caddyfile.staging` (Basic-Auth-gated, non-standard
ports — never bound to 80/443), `scripts/vps/backup-staging.sh`, `scripts/vps/rollback-staging.sh`,
with the full resource math in `docs/STAGING_DEPLOYMENT.md`. This configuration has been
validated structurally (`docker compose config` resolves cleanly) but **has not been run** —
this engineering effort has no credentials for any real hosting infrastructure, so nothing has
actually been deployed anywhere.

## 7. Open blockers — precisely

a. **`security/POLICY_LOCK.json` needs your reviewed re-signing.** A CI workflow fix (renaming
   a test database so the real-model verification workflow would actually run) changed that
   workflow's file hash without the lock being regenerated. The independent review
   (`docs/POLICY_LOCK_REVIEW_eddddf53.md`) confirmed the only actual change is a 6-line
   database-name string — permissions, triggers, and secret handling are byte-for-byte
   unchanged — and shows the exact hash mismatch arithmetically so you can check it yourself.
   No one has re-signed this; that step is reserved for you.

b. **Z.ai's security work beyond round 12 remains unreachable.** Every claimed delivery
   (R20, R21–R24, R25–R30) has been checked repeatedly, most recently fresh in this round, and
   none exist as a reachable branch, PR, or bundle anywhere. There is nothing to integrate yet.
   A ready-to-follow procedure for when it does land is in
   `docs/ZAI_R21_R30_INTEGRATION_PROCEDURE.md`.

c. **The founder-wide (non-goal-scoped) authorization primitive shape is an open architecture
   decision only you can make** (tracked as task #61). Until it's decided, one specific
   conflict-detection path stays deliberately fail-closed rather than guessing at a shape.

d. **No independent third-party security review of this candidate has happened.** The review
   package (`docs/FOUNDER_ALPHA_INDEPENDENT_REVIEW_PACKAGE.md`, from an earlier phase) is built
   so a reviewer who wasn't part of this effort can reproduce every security claim themselves —
   but no one has actually done that review yet.

e. **No real local-model conversation has been exercised in an interactive session** — only the
   one CI run described in section 4. If you need stronger confidence than that before relying
   on the local-model path, run it yourself interactively.

f. **Unresolved discrepancy about the production VPS's current state.** The founder directive
   that kicked off this round states the production VPS "already runs an older LifeAI
   installation," but the repository's own deployment scripts (`scripts/vps/lib.sh`'s header
   comment) say no real server exists yet for them to run against. This may mean the
   `docker-compose.vps.yml`/Strato-VPS topology in this repo has never actually been deployed,
   and a different, earlier installation (possibly a since-abandoned Render-based deployment
   referenced elsewhere in the docs) is what's actually running. This needs a one-line
   confirmation from you — nothing below assumes either answer.

g. **Two founder-journey steps were not fully driven through the browser UI.** The supervised
   development task's full create → approve round trip was verified at the mechanism and
   authorization-boundary level (real tests, real database), but not click-by-click through the
   actual frontend in the most recent full browser pass. Worth a manual check before you rely
   on it end to end.

## 8. Rollback

Tested live, for real, in a disposable environment (not just read from the scripts):
`alembic downgrade 0079` (the pre-integration baseline revision) from the current head cleanly
reverses the schema; the runtime privilege policy re-verifies correctly at that older state;
rolling forward again (`alembic upgrade head`) is clean. The equivalent staging-specific script
is `scripts/vps/rollback-staging.sh`; production's existing `scripts/vps/rollback.sh` is
untouched by any of this work.

## 9. Opening and using LifeAI — for you, not an engineer

Once installation (section 2) has finished and all services are running:

1. Open `http://localhost:3000` (or your staging URL) in a browser.
2. You'll land on the login page. Log in with the founder email and password you set in
   `.env` as `FOUNDER_EMAIL`/`FOUNDER_PASSWORD` — this account is created automatically the
   first time the backend starts, already verified, no email step needed. There is no
   self-registration; this app has exactly one account, yours.
3. You'll land in the chat view. Type a message and send it — if you've configured at least
   one API key (or a local Ollama model, per section 2), you'll get a real response.
4. To use your own documents: use the upload control to add a file. Processing happens in the
   background (the worker service) — give it a few seconds, then ask a question about the
   document's contents in the same or a new conversation; the answer should cite what it used.
5. Conversations are saved automatically and listed in the sidebar — closing the tab and coming
   back later will show your history.
6. To ask LifeAI to work on a development task: look for the project/task area of the app,
   describe what you want, and you'll be shown a proposed plan before anything runs. You must
   explicitly approve it before any code-level action happens — nothing executes on its own.
7. If anything looks wrong or a response seems off, check the admin panel (`/admin`) for
   provider/model status, or the conversation's own error indicator — errors are shown plainly
   rather than silently swallowed.

---

**Release recommendation**: this candidate is ready for independent review and for deployment
to an isolated staging environment (not production). It is not yet ready for unsupervised,
everyday founder use in production — specifically because of the open policy-lock sign-off
(7a), the still-missing independent security review (7d), the unresolved production-VPS
discrepancy (7f), and the two not-fully-browser-verified journey steps (7g). None of these are
reasons to distrust the engineering that has been done — this round found and fixed five real,
previously-unknown defects by actually running the application rather than trusting its tests —
but they are real gaps between "verified by engineering" and "ready for you to depend on,"
and this document's job is to keep that line honest.
