# Decision Register
**Sources:** `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` (MainAI_Conversation_Knowledge_Pack_2026-07-19), `LIFE_OS_ARCHITECTURE_SUMMIT_2026-07-16.md`, `04_DECISIONS_CORRECTIONS_AND_OPEN_ITEMS.md` (MainAI_chat_handover_2026-07-17), and direct repository verification (this FKP v1.1 pass, 2026-07-19).
**Note:** Every decision entry links to its source file. No decision is inferred without citation. D-01–D-26 are carried forward unchanged from FKP v1.0. D-27 onward are new in v1.1, recording what has since been verified or shipped.

---

## Product and identity decisions

| ID | Decision | Date | Source | Status |
|----|----------|------|--------|--------|
| D-01 | MainAI is founder-only | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |
| D-02 | MainAI is not a shared organizational AI and not every user's AI | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |
| D-03 | Founder UserAI is separate from MainAI/FounderAI | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |
| D-04 | Future users receive private UserAI, not MainAI | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |
| D-05 | Public registration removed for Step 1 | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED — **implemented and verified**, see D-27 |
| D-06 | Future onboarding via Life City is deferred | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |

## Deployment and cost decisions

| ID | Decision | Date | Source | Status |
|----|----------|------|--------|--------|
| D-07 | Target additional infrastructure cost is 0 SEK/month during MVP | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |
| D-08 | Expensive Render Blueprint topology rejected | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |
| D-09 | Vercel set aside | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |
| D-10 | Combined Render Free service with external free data services chosen | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED — **built**: `Dockerfile.combined`, `render.yaml` (single `LifeAI` web service) |
| D-11 | Supabase Free, Upstash Free, Strato to be used | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED — wired into `render.yaml`/`docs/RENDER_DEPLOY.md`, not yet deployed |
| D-12 | Auto-deploy remains off until configuration and tests complete | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED — `render.yaml: autoDeploy: false`, still true in v1.1 |

## AI architecture decisions

| ID | Decision | Date | Source | Status |
|----|----------|------|--------|--------|
| D-13 | Multiple agents work where strongest; MainAI orchestrates by capability/cost/availability | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED — DESIGNED, not implemented |
| D-14 | Manual web chats can be used without additional API spending but cannot be auto-controlled for free | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |
| D-15 | Long-term direction is provider-independent and increasingly local-first | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED — LifeAI backend already has multi-provider abstraction (5 chat/embedding providers, fallback order) |
| D-16 | MainAI cannot change foundational rules, approve own privilege increase, spend money, publish, delete, or bypass founder approval | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |
| D-17 | UPGRADE_26 v2 migration designed; 26 PASS / 0 FAIL / 1 INCONCLUSIVE in local PG16 tests | 2026-07-17 | `TEST_RESULTS_2026-07-17.md` (CLAUDE_CODE_HANDOVER package) | DESIGNED — **not applied to LifeAI**; written against a different (Supabase/`savings-story-scanner`) schema, see `03_ARCHITECTURE/TARGET_ARCHITECTURE.md` caveat and `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` CONFLICT-04 |
| D-18 | `ai_decisions` standalone table permanently retired; decisions are `knowledge_objects.object_type='decision'` | 2026-07-17 | Design review sessions | CONFIRMED — design-level only, no LifeAI table exists yet |
| D-19 | `raw_material` is untrusted-by-design; promotion to `knowledge_objects` requires human confirmation | 2026-07-17 | Design review sessions | CONFIRMED — design-level only |

## Technical architecture decisions (ratified 2026-07-16)

| ID | Decision | Date | Source | Status |
|----|----------|------|--------|--------|
| D-20 | Technical excellence over prestige — best solution wins regardless of origin | 2026-07-16 | `LIFE_OS_ARCHITECTURE_SUMMIT_2026-07-16.md` | RATIFIED |
| D-21 | AI-independent development — GitHub is source of truth; no single AI sole knowledge bearer | 2026-07-16 | `LIFE_OS_ARCHITECTURE_SUMMIT_2026-07-16.md` | RATIFIED |
| D-22 | Platform before implementation — 100-module test, generality, long-term maintainability | 2026-07-16 | `LIFE_OS_ARCHITECTURE_SUMMIT_2026-07-16.md` | RATIFIED |

## Context and memory decisions

| ID | Decision | Date | Source | Status |
|----|----------|------|--------|--------|
| D-23 | Conversation Context Resolver is a binding standard for MainAI and future UserAI | 2026-07-19 | In-conversation directive | CONFIRMED — design-level only, no code implementation yet |
| D-24 | Current message has highest priority; last 3–5 verbatim; last 15 as timeline | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |
| D-25 | Explicit corrections supersede older beliefs; superseded info preserved with pointer | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |
| D-26 | AI responses carry enough semantic context to function as portable handovers | 2026-07-19 | `02_DECISIONS_CONSTRAINTS_AND_CORRECTIONS.md` | CONFIRMED |

## New in FKP v1.1 — verified by direct repository inspection, 2026-07-19

| ID | Decision | Date | Source | Status |
|----|----------|------|--------|--------|
| D-27 | Founder-only launch (D-01/D-05 made concrete): fixed-UUID founder identity, `require_founder()` gating on MainAI-surface routers only (not generic auth), production-only registration block, full pytest + Playwright coverage | 2026-07-19 | `claude/founder-only-launch@bf31fad`, re-reviewed at `e9b4b76` | CONFIRMED, CI-GREEN, **not yet merged or deployed** |
| D-28 | `require_founder()` scope is deliberately narrower than "all authenticated-session operations" — login, logout, `/me`, password reset, and self-service account export/delete stay on plain `get_current_user`; only the actual MainAI product surface (chat/conversations/documents/knowledge/projects/admin) is founder-gated | 2026-07-19 | Architectural correction made during independent review, this session | CONFIRMED — reasoned decision, not founder-instructed; open to founder override |
| D-29 | Repository identity resolved: `d1n095/LifeAI`, current stack is Next.js (App Router) frontend + FastAPI backend + Postgres/SQLAlchemy/Alembic + pgvector + Redis, **not** the TanStack/Bun/Supabase `savings-story-scanner` stack described throughout FKP v1.0 | 2026-07-19 | Direct repository inspection | CONFIRMED — resolves OD-14 and CONFLICT-01 |
| D-30 | FKP v1's `PACKAGE_MANIFEST.json`/`SHA256SUMS.txt` self-inconsistency (K-04 in the review overlay audit) is corrected in FKP v1.1 by generating the manifest and checksums only after all v1.1 content files are final, listing only files actually present in this package | 2026-07-19 | `FKP_V1_AUDIT.md` finding K-04, this session's fix | CONFIRMED |
