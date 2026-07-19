# Implemented and Verified — LifeAI, 2026-07-19
**Source:** Direct repository/CI inspection. Replaces FKP v1.0's version, which listed `savings-story-scanner` items.
**Rule:** Everything here has direct evidence (file path, test name, commit hash, or CI run) — not a self-report from a prior AI session.

---

| Item | Evidence |
|------|----------|
| Cookie-based auth (Argon2id, CSRF, refresh rotation + reuse detection, session revocation) | `backend/app/security.py`, `backend/app/cookies.py`, `backend/tests/security/test_session_auth.py` |
| Password policy + email verification + password reset (Strato SMTP, implicit SSL/465) | `backend/app/password_policy.py`, `backend/app/email.py`, `backend/tests/account/test_password_reset.py`, `test_verification.py` |
| Account export + deletion (transactional, rollback-tested) | `backend/app/routers/account.py`, `backend/tests/account/test_account_deletion.py` |
| Redis-backed distributed rate limiting | `backend/app/limiter.py`, `backend/tests/account/test_rate_limiting.py`, `frontend/e2e/rate-limit.spec.ts` |
| Idempotent token cleanup job | `backend/app/cleanup.py`, `backend/tests/backend/test_cleanup_job.py` |
| Alembic migration chain, round-trip verified | `backend/alembic/versions/0001–0004`, CI job `migration-check` |
| Multi-provider AI abstraction (5 hosted + local Ollama) + cost tracking | `backend/app/providers/`, `backend/app/models/usage.py` (`Numeric(14,6)`) |
| RAG pipeline: ingest, chunk, pgvector retrieve, confidence scoring | `backend/app/rag/`, `backend/app/models/document_chunk.py` |
| Same-origin API proxy (no direct browser→backend calls) | `frontend/app/api/[...path]/route.ts`, `frontend/e2e/same-origin-proxy.spec.ts` |
| Combined single-container deployment, real Docker build verified in CI | `Dockerfile.combined`, `scripts/entrypoint-combined.sh`, CI job `combined-container-verify` |
| Render Blueprint (single free web service, external Supabase/Upstash/Strato, `autoDeploy: false`) | `render.yaml`, `docs/RENDER_DEPLOY.md` |
| Full CI pipeline, all jobs required before deploy is even possible | `.github/workflows/ci.yml` |
| **Founder-only launch: fixed-UUID founder identity, idempotent bootstrap** | `backend/app/founder.py`, `backend/app/bootstrap.py`, `backend/tests/account/test_founder_only.py`, branch `claude/founder-only-launch@bf31fad` |
| **Founder-only launch: `require_founder()` on MainAI-surface routers only** | `backend/app/deps.py`, `backend/app/routers/{chat,conversations,documents,knowledge,projects,admin}.py` |
| **Founder-only launch: production-only registration block** | `backend/app/routers/auth.py` (`register()` 404 in production), `frontend/app/register/page.tsx` |
| **Founder-only launch: migration rollback bug found and fixed** | `backend/alembic/versions/0005_founder_role.py`, verified against a real disposable Postgres — downgrade failed before fix (enum cast abort with a `role='founder'` row present), succeeded after |
| **Founder-only launch: CI green including the independent-review fix commit** | `claude/founder-only-launch@e9b4b76`, GitHub Actions run `29689918729` — completed, success |
| **Founder-only launch: stale `ADMIN_EMAIL`/self-registration documentation corrected** | `README.md`, `docs/RENDER_DEPLOY.md`, `docs/OPERATIONS.md`, `docs/MAINAI_0.1_PLAN.md`, commit `e9b4b76` |

## Explicitly NOT yet true (do not assume otherwise)

- `claude/founder-only-launch` is not merged into the deploy branch.
- Nothing is deployed to Render; the live `https://lifeai-1.onrender.com` still runs whatever was last actually deployed there (unverified from this session — Render dashboard state is founder-side only).
- No MFA, no Founder Vault, no owner/scope column on documents/projects/tasks, no knowledge-object promotion pipeline beyond RAG ingest, no Conversation Context Resolver, no multi-agent task graph runtime.
