# Built vs Designed — LifeAI, verified 2026-07-19
**Source:** Direct inspection of `d1n095/LifeAI`, this FKP v1.1 pass.
**Replaces:** FKP v1.0's `BUILT_VS_DESIGNED.md`, which listed `savings-story-scanner` modules (`src/modules/salary/`, `src/routes/_app/*`) as LifeAI's built state. That codebase is unrelated to LifeAI — see `10_HISTORICAL_DOMAIN_MATERIAL/`.
**Status:** CONFIRMED by direct code/test inspection.

---

## BUILT and CI-verified (on `claude/det-kommer-mer-879lcm`, the deploy branch)

- FastAPI backend with cookie-based auth, Argon2id passwords, password policy, email verification, password reset, session revocation, logout-all
- Multi-provider AI abstraction (5 chat/embedding providers + local Ollama) with pricing/cost tracking (`usage_log`)
- RAG pipeline: document ingestion, chunking, pgvector retrieval, confidence scoring surfaced to chat
- Alembic migration chain (0001–0004), replacing an earlier `create_all()` approach
- Redis-backed distributed rate limiting, idempotent token-cleanup scheduler
- Next.js frontend: login, register (public, on this branch), forgot/reset password, verify-email, account management, chat with voice input and animated orb, conversation history, document upload, project/task views, admin provider/usage panel
- Same-origin API proxy (`app/api/[...path]/route.ts`) — browser never talks to the backend directly
- Combined single-container deployment (`Dockerfile.combined`) with loopback isolation of the backend process, verified in CI with a real Docker build (not just process-level local checks)
- `render.yaml` Blueprint for a single Render Free web service + external Supabase Postgres/pgvector + Upstash Redis + Strato SMTP (implicit SSL, port 465)
- Full pytest suite (auth, RLS-equivalent isolation, account lifecycle, rate limiting, cleanup jobs, migration round-trip) and Playwright E2E suite (auth, account, same-origin proxy, security headers, rate limiting)
- GitHub Actions CI covering all of the above, with CI-gated (not automatic) Render deploy

## BUILT and CI-verified, but not yet merged to the deploy branch

- **Founder-only launch** (`claude/founder-only-launch@bf31fad`, independently re-reviewed and fixed at `e9b4b76`): fixed-UUID founder identity, `require_founder()` on the MainAI product surface, production-only registration block, `0005_founder_role` migration (including a fixed rollback path — see `06_PROJECT_STATUS/CURRENT_STATUS.md`), updated tests and E2E coverage, updated docs (`README.md`, `docs/RENDER_DEPLOY.md`, `docs/OPERATIONS.md`). CI-green. Awaiting explicit founder approval to merge — not merged, not deployed.

## DESIGNED, not built in LifeAI

These exist as design documents in this package (carried forward from FKP v1.0, some originally written against a different stack — see caveats on each file) and have no corresponding LifeAI code yet:

| Design | Where documented | Caveat |
|---|---|---|
| UPGRADE_26 v2 knowledge-object data model (`raw_material` → `knowledge_objects` promotion, 10 tables, 18 SECURITY DEFINER functions) | `03_ARCHITECTURE/TARGET_ARCHITECTURE.md`, raw SQL in the FKP v1 raw-originals archive | Written against Supabase/Postgres RLS with `auth.uid()`. Must be compared table-for-table and capability-for-capability against LifeAI's actual SQLAlchemy/Alembic model before any SQL is applied — see `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` CONFLICT-04. LifeAI's existing `backend/app/rag/` document-chunk pipeline already covers a simpler subset of this (ingest + retrieve), which is not the same as the full epistemic promotion model. |
| Conversation Context Resolver (context assembly order, message classification, correction/supersession lifecycle) | `08_HANDOVER/AGENT_CONTEXT_RULES.md` | Stack-agnostic; no LifeAI implementation exists. Would live somewhere in the chat/conversation pipeline once "project memory" work begins — see `09_DEPENDENCY_STAIRCASE/`. |
| Multi-agent capability registry, task graph, checkpoint/handover contract, operating modes (crawl/stair/interval/sprint/safe-stop) | `03_ARCHITECTURE/AI_RESOURCE_ORCHESTRATION.md`, `03_ARCHITECTURE/ADAPTIVE_WORK_ORCHESTRATION.md` | Stack-agnostic, no runtime implementation. LifeAI's existing `backend/app/providers/registry.py` fallback-order mechanism is a narrow, already-built precursor to the "resource-aware routing" idea, not the full registry. |
| Founder Vault (encrypted root-level store) | `05_SECURITY_AND_TRUST/SECURITY_REQUIREMENTS.md`, `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md` (OD-03) | Analysis exists, no schema, no code. |
| Founder Vault MFA/passkey | `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md` (OD-02) | Analysis exists, no code. |
| Data-zone/scope taxonomy for future multi-user isolation | `05_SECURITY_AND_TRUST/SECURITY_REQUIREMENTS.md`, `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md` (OD-04) | Written against Supabase RLS pattern; LifeAI's actual isolation mechanism (`app/rls.py`) differs and needs its own translation. |
| Life City, LifeWeb, Founder UserAI as separate identity, Source Hub, GitHub/Update Center | `01_FOUNDER_VISION/DOMAIN_AND_REQUIREMENT_MAP.md`, `02_DECISIONS/CANDIDATE_REQUIREMENTS.md`, `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md` | Explicitly deferred; must not interrupt Step 1 per the founder's own instruction. |

## DEPRECATED / superseded within LifeAI's own history

- **Qdrant** vector store — replaced by pgvector (`backend/app/rag/vector_store.py`), part of the single-free-service redesign. See `07_CONFLICTS_AND_GAPS/STALE_INFORMATION.md`.
- **`ADMIN_EMAIL`/`ADMIN_PASSWORD`/admin-role model** — superseded by `FOUNDER_EMAIL`/`FOUNDER_PASSWORD`/founder-role on `claude/founder-only-launch`. Any documentation still referencing the old names describes a state that no longer matches the founder-only branch (this was found and fixed for `README.md`/`docs/RENDER_DEPLOY.md`/`docs/OPERATIONS.md`/`docs/MAINAI_0.1_PLAN.md` during the independent review, commit `e9b4b76`) — but is still accurate for the deploy branch itself until founder-only-launch merges.
