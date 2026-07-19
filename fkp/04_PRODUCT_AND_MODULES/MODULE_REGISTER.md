# Module Register — LifeAI, verified 2026-07-19
**Sources:** Direct inspection of `d1n095/LifeAI` (this FKP v1.1 pass); `01_FOUNDER_VISION/DOMAIN_AND_REQUIREMENT_MAP.md` for future scope.
**Replaces:** FKP v1.0's `MODULE_REGISTER.md` Tier 2, which listed `savings-story-scanner` modules (salary/OB calculation, calendar, planning, finance score, schedule OCR, dashboard routes) as "BUILT" for LifeAI. They were built, but for a different, unrelated product. That table now lives, correctly labeled, in `10_HISTORICAL_DOMAIN_MATERIAL/MODULE_REGISTER_SAVINGS_STORY_SCANNER.md`.
**Note:** Modules marked BUILT are verified directly in this repository. Others are designed or planned.

---

## Tier 0 — Core platform (prerequisite for everything)

| Module | Status | Location | Notes |
|--------|--------|----------|-------|
| Cookie-based auth + session | BUILT ✅ | `backend/app/routers/auth.py`, `backend/app/security.py`, `backend/app/cookies.py` | Argon2id, CSRF, refresh rotation + reuse detection, revocation |
| Database + migrations | BUILT ✅ | `backend/alembic/versions/0001–0004` (+`0005` on `claude/founder-only-launch`, unmerged) | SQLAlchemy + Alembic, not Supabase-managed |
| AI provider abstraction | BUILT ✅ | `backend/app/providers/` | 5 hosted providers + local Ollama, fallback order, pricing |
| RAG pipeline (ingest/chunk/retrieve/trust) | BUILT ✅ | `backend/app/rag/` | pgvector-backed, confidence scoring surfaced to chat UI |
| Same-origin API proxy | BUILT ✅ | `frontend/app/api/[...path]/route.ts` | Browser never talks to backend directly |
| Rate limiting + cleanup jobs | BUILT ✅ | `backend/app/limiter.py`, `backend/app/cleanup.py`, `backend/app/scheduler.py` | Redis-backed |
| Combined single-container deployment | BUILT ✅ | `Dockerfile.combined`, `scripts/entrypoint-combined.sh`, `render.yaml` | CI-verified real Docker build |
| CI pipeline | BUILT ✅ | `.github/workflows/ci.yml` | See `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md` for job list |
| MainAI Foundation (UPGRADE_26 v2-equivalent) | DESIGNED | `03_ARCHITECTURE/TARGET_ARCHITECTURE.md` | Not applied; needs translation to LifeAI's stack first |

## Tier 1 — Founder-only (Step 1)

| Module | Status | Location | Notes |
|--------|--------|----------|-------|
| Founder-only auth | BUILT ✅, CI-GREEN, **not merged** | `backend/app/founder.py`, `backend/app/deps.py` (`require_founder`) | `claude/founder-only-launch@bf31fad`, reviewed at `e9b4b76` |
| Founder provisioning (bootstrap) | BUILT ✅, CI-GREEN, **not merged** | `backend/app/bootstrap.py` | Idempotent on fixed UUID primary key; no hardcoded email/password |
| Public registration block | BUILT ✅, CI-GREEN, **not merged** | `backend/app/routers/auth.py` (`register()` → 404 in production), `frontend/app/register/page.tsx` (dead redirect) | Server-side, not just UI |
| Protected route guard | BUILT ✅, CI-GREEN, **not merged** | `require_founder()` on chat/conversations/documents/knowledge/projects/admin routers | Deliberately excludes login/`/me`/logout-all/account — see D-28 |

## Tier 2 — MainAI features (designed, not built in LifeAI)

| Module | Status | Source |
|--------|--------|--------|
| Founder Vault | DESIGNED (analysis only) | `05_SECURITY_AND_TRUST/SECURITY_REQUIREMENTS.md`, `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md` OD-03 |
| MFA/passkey for founder | DESIGNED (analysis only) | `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md` OD-02 |
| Ownership/scope column on documents/projects/tasks | DESIGNED (analysis only) | `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md` OD-04 |
| Knowledge promotion (`raw_material` → `knowledge_objects`) beyond current RAG ingest | DESIGNED | `03_ARCHITECTURE/TARGET_ARCHITECTURE.md` (needs stack translation) |
| Conversation Context Resolver | DESIGNED | `08_HANDOVER/AGENT_CONTEXT_RULES.md` |
| Agent capability registry / task graph | DESIGNED | `03_ARCHITECTURE/AI_RESOURCE_ORCHESTRATION.md`, `ADAPTIVE_WORK_ORCHESTRATION.md` |
| Task/decision/handover views (UI) | DESIGNED | Prior Lovable prompt series (unrelated stack — treat as inspiration only) |
| Export (beyond current account export) | PARTIALLY BUILT | `backend/app/routers/account.py` has account data export; full knowledge/project export not built |

## Tier 3 — Existing generic Life OS product surface (already built in LifeAI, distinct from MainAI)

| Module | Status | Location | Notes |
|--------|--------|----------|-------|
| Conversations UI + history | BUILT ✅ | `frontend/app/(shell)/chat/`, `backend/app/routers/conversations.py` | |
| Document upload/list/delete | BUILT ✅ | `frontend/app/(shell)/documents/`, `backend/app/routers/documents.py` | Single-owner today, see OD-04 |
| Knowledge search | BUILT ✅ | `frontend/app/(shell)/knowledge/`, `backend/app/routers/knowledge.py` | pgvector-backed |
| Projects/tasks | BUILT ✅ | `frontend/app/(shell)/projects/`, `backend/app/routers/projects.py` | Single-owner today, see OD-04 |
| Admin: provider status, cost/usage summary | BUILT ✅ | `frontend/app/(shell)/admin/`, `backend/app/routers/admin.py` | |
| Account management (export/delete) | BUILT ✅ | `frontend/app/(shell)/account/`, `backend/app/routers/account.py` | Not founder-gated, see D-28 |

## Tier 4 — Future (deferred, not built, not scheduled)

| Module | Status | Source |
|--------|--------|--------|
| Founder UserAI (separate identity from MainAI) | DEFERRED | `01_FOUNDER_VISION/MASTER_PRODUCT_VISION.md`, `02_DECISIONS/CANDIDATE_REQUIREMENTS.md` §8 |
| Future UserAI (isolated, per future user) | DEFERRED | Vision documents |
| Life City / LifeWeb | DEFERRED | `01_FOUNDER_VISION/DOMAIN_AND_REQUIREMENT_MAP.md` §8, §11 |
| GitHub App / Update Center integration | DEFERRED | `02_DECISIONS/CANDIDATE_REQUIREMENTS.md` §9 |
| Source Hub (multi-tool import) | DEFERRED | `01_FOUNDER_VISION/DOMAIN_AND_REQUIREMENT_MAP.md` §1 |
| Health, smart home, and other broad Life OS domain modules | DEFERRED | `01_FOUNDER_VISION/DOMAIN_AND_REQUIREMENT_MAP.md` §12 |
| Investigation / Trust Engine system | DEFERRED | `05_SECURITY_AND_TRUST/SECURITY_REQUIREMENTS.md` §Trust Engine |
| Local model routing | DEFERRED | `03_ARCHITECTURE/AI_RESOURCE_ORCHESTRATION.md` |
| Digital twin / test city | DEFERRED (explicit future candidate) | `02_DECISIONS/CANDIDATE_REQUIREMENTS.md` §10 |
