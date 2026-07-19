# Current Architecture — LifeAI, verified 2026-07-19
**Source:** Direct inspection of `d1n095/LifeAI`, branch `claude/det-kommer-mer-879lcm` (tip `f0a1975`) and `claude/founder-only-launch` (tip `bf31fad`, re-reviewed at `e9b4b76`), this FKP v1.1 pass.
**Replaces:** FKP v1.0's `CURRENT_ARCHITECTURE.md`, which described `savings-story-scanner`/My Money Master (TanStack Start, Bun, Supabase, `src/routes`, `src/modules/salary`) as if it were LifeAI's current state. It was not — that was a different, unrelated repository. See `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` CONFLICT-01 (resolved) and `10_HISTORICAL_DOMAIN_MATERIAL/` for where that material now lives.
**Status:** CONFIRMED by direct code inspection, not a report from an earlier AI session.

---

## Stack

- **Frontend:** Next.js (App Router), TypeScript, `frontend/app/`. Routes: `login`, `register` (dead redirect to `/login` — see below), `forgot-password`, `reset-password`, `verify-email`, and a `(shell)` route group holding the authenticated product surface: `chat`, `conversations` (implicit via chat), `documents`, `knowledge`, `projects`, `account`, `admin`.
- **API proxy:** `frontend/app/api/[...path]/route.ts` — same-origin proxy from the browser to the backend over loopback (`INTERNAL_API_URL=http://127.0.0.1:8000` in the combined container). The browser never learns the backend's real address; session cookies stay same-origin. `frontend/lib/api.ts` is the single client-side fetch wrapper (CSRF header injection, 401→refresh→retry, generic network-error messages).
- **Backend:** FastAPI, Python, `backend/app/`. Routers: `auth`, `account`, `chat`, `conversations`, `documents`, `knowledge`, `projects`, `admin`, `health`.
- **Database:** PostgreSQL via SQLAlchemy + Alembic (not Supabase-managed migrations). Current migration chain on the deploy branch: `0001_baseline_schema` → `0002_cookie_session_tables` → `0003_account_lifecycle` → `0004_pgvector_document_chunks` (4 migrations). `claude/founder-only-launch` adds a 5th, `0005_founder_role` (not yet merged).
- **Vector search:** pgvector, not Qdrant — `backend/app/models/document_chunk.py` and `backend/app/rag/vector_store.py`. Qdrant was used early in the project and was explicitly replaced (see `07_CONFLICTS_AND_GAPS/STALE_INFORMATION.md`).
- **Cache/rate-limit/session-revocation backing store:** Redis (Upstash Free in production), `backend/app/limiter.py`.
- **AI provider abstraction:** `backend/app/providers/` — `anthropic_provider.py`, `openai_provider.py`, `gemini_provider.py`, `deepseek_provider.py`, `openrouter_provider.py`, `ollama_provider.py` (local), plus `pricing.py` and `registry.py` for fallback ordering and cost lookup. This is a real, built multi-provider abstraction, not a design document — it directly implements the "provider-independent AI architecture" and "MainAI orchestrates by capability/cost/availability" principles from `01_FOUNDER_VISION/MASTER_PRODUCT_VISION.md` and D-15/D-13.
- **RAG pipeline:** `backend/app/rag/` — `extract.py`, `chunking.py`, `ingest.py`, `retrieve.py`, `trust.py` (confidence scoring: high/medium/low/none, surfaced to the frontend chat UI).
- **Auth/session model:** Cookie-based (HttpOnly, Secure, SameSite), not localStorage tokens. Argon2id password hashing, password policy (`backend/app/password_policy.py`), CSRF token issued in response bodies and checked on mutating requests, refresh-token rotation with reuse detection, session revocation via `sessions_valid_after` timestamp comparison against JWT `iat`.
- **Isolation mechanism:** `backend/app/rls.py` — LifeAI's own SQLAlchemy-session-scoped row isolation, **not** Supabase's `auth.uid()`-based Postgres RLS policy pattern that `05_SECURITY_AND_TRUST/SECURITY_REQUIREMENTS.md` describes. That document's specific SQL pattern is written for a different stack; treat it as a design target to translate, not literal code that exists today. See `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md`.
- **Deployment topology:** Single Render Free web service (`render.yaml`, service name `LifeAI`, matching the live `https://lifeai-1.onrender.com`). Next.js and FastAPI run as sibling processes inside one container (`Dockerfile.combined`, `scripts/entrypoint-combined.sh`) — Next.js binds publicly to `$PORT`, FastAPI binds only to `127.0.0.1:8000`, unreachable from outside the container by construction. Postgres (Supabase Free, with pgvector) and Redis (Upstash Free) are external, not Render-managed. `autoDeploy: false` — deploys are gated on CI (`deploy-render` job) and remain a founder-approval action, not automatic.
- **Email:** SMTP via Strato, implicit TLS/SSL on port 465 (not STARTTLS). Mandatory in production — `app/main.py`'s `_check_smtp_configured`/`_check_smtp_mode` fail startup if misconfigured. Dev fallback logs emails instead of sending.

## Authentication and access model (as of `claude/founder-only-launch`, not yet merged)

- Exactly one intended account, provisioned idempotently on startup via a fixed, non-secret UUID sentinel (`FOUNDER_USER_ID = uuid.UUID(int=1)`, `backend/app/founder.py` / `backend/app/bootstrap.py`), keyed by primary key so repeated startups (including per-test-run truncation in CI) don't create duplicates or re-run provisioning wastefully.
- `require_founder()` (`backend/app/deps.py`) checks both `user.role == UserRole.founder` and `user.id == FOUNDER_USER_ID` — defense in depth, not just a role flag.
- `require_founder()` gates only the actual MainAI product surface: `chat`, `conversations`, `documents`, `knowledge`, `projects`, `admin`. It deliberately does **not** gate `login`, `/me`, `logout-all`, or `account.py`'s export/delete routes — those stay on generic `get_current_user`, preserving the account-security machinery's own test coverage and correctness independent of the founder-only product gate. See D-28 in `02_DECISIONS/DECISION_REGISTER.md` for the reasoning.
- `POST /api/auth/register` returns `404` when `ENVIRONMENT=production` — blocked server-side, not just hidden in the UI. `frontend/app/register/page.tsx` is a dead client-side redirect to `/login`; `frontend/app/robots.ts` blocks search-engine indexing of the (now-inert) route.

## CI pipeline (`.github/workflows/ci.yml`)

Jobs, all required before `deploy-render` can run: `lint-and-typecheck` (frontend TS/ESLint), `npm-audit`, `frontend-build` (Docker mode + Vercel mode matrix), `backend-tests` (unit/integration), `rls-security-tests`, `account-rate-limit-tests`, `migration-check` (Alembic upgrade/downgrade round-trip against a real Postgres service container), `same-origin-proxy-test`, `combined-container-verify` (real `Dockerfile.combined` build + boot, gated to run only on specific branches, not every push), `all-checks-passed` (aggregation gate), `deploy-render` (only fires on the deploy branch, after everything else is green, and only if Render deploy-hook secrets are actually configured for that run — verified empty/no-op in the runs performed so far in this engagement).

## What does not exist yet in LifeAI (do not assume it does)

- No Founder Vault (OD-03 — analysis only, see `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md`)
- No MFA/passkey (OD-02 — analysis only)
- No `owner_id`/scope column on `documents`/`projects`/tasks-equivalent tables (OD-04 — analysis only)
- No knowledge-object promotion pipeline (`raw_material` → `knowledge_objects`) — `backend/app/rag/` ingests and retrieves document chunks for RAG chat context, which is a different, simpler thing than the UPGRADE_26 v2 epistemic model described in `03_ARCHITECTURE/TARGET_ARCHITECTURE.md`
- No Conversation Context Resolver implementation (D-23 is design-only)
- No multi-agent task graph/capability registry runtime (D-13, `ADAPTIVE_WORK_ORCHESTRATION.md`/`AI_RESOURCE_ORCHESTRATION.md` are design-only)
- No Founder UserAI as a separate identity — one identity exists today (the founder account)
