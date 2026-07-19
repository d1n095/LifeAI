# Built vs Designed — `savings-story-scanner` / My Money Master (HISTORICAL — NOT LifeAI)
**⚠️ This document describes a different, unrelated repository (`d1n095/savings-story-scanner`), not `d1n095/LifeAI`. See `10_HISTORICAL_DOMAIN_MATERIAL/README.md`. For LifeAI's actual built/designed status, see `03_ARCHITECTURE/BUILT_VS_DESIGNED.md`.**

**Sources:** All files in the original FKP v1 package. Verification status noted per item.

---

## BUILT AND VERIFIED (in `savings-story-scanner`'s repository)

| Component | Location | Verified? | Notes |
|-----------|----------|-----------|-------|
| Salary/OB/shift engine | `src/modules/salary/` | ✅ Code verified in repo | compute.ts, ob.ts, breaks.ts, etc. |
| Auth pattern (requireSupabaseAuth) | `src/integrations/supabase/` | ✅ Code verified in repo | Pattern for this repo's server functions — not LifeAI's pattern |
| Supabase client + server client | `src/integrations/supabase/` | ✅ Code verified in repo | Service role correctly isolated |
| OCR feature (Lovable Gateway + Gemini) | `src/lib/schedule-ocr.functions.ts` | ✅ Code verified in repo | Part of this repo, unrelated to LifeAI |
| Shadcn/UI design system | `src/components/ui/` | ✅ 50+ components in repo | This repo's design system |
| 9 original Supabase migrations | `supabase/migrations/` | ✅ Files verified in repo | Foundation for UPGRADE_26 v2, which targets this repo's schema, not LifeAI's |
| TanStack routing + layout | `src/routes/` | ✅ Code verified in repo | This repo's routing pattern |
| Existing Life OS-style routes | `src/routes/_app/` | ✅ Code verified in repo | idag, kalender, jobb, pengar, etc. — this repo's product surface |
| SMTP via Strato (implicit SSL) | Reported | ⚠️ Reported, not independently verified at the time | Note: LifeAI independently also uses Strato/implicit-SSL/port-465 SMTP — a coincidence of shared vendor choice, not evidence the two repos are related |

## DESIGNED AND TESTED LOCALLY — NOT APPLIED TO SUPABASE (for this repo's schema)

| Component | Location in original package | Test status | Notes |
|-----------|---------------------|-------------|-------|
| UPGRADE_26 v2 migration (10 tables, 18 functions) | FKP raw-originals archive, `01_sql_migration/UPGRADE_26_v2__main_ai_foundation.sql` | 26 PASS / 0 FAIL / 1 INCONCLUSIVE (local PG16) | Designed against this repo's Supabase schema. See `03_ARCHITECTURE/TARGET_ARCHITECTURE.md` for the caveat on adapting it to LifeAI instead. |
| AI provider abstraction (AnthropicProvider, AIGateway) | FKP raw-originals archive, Lovable prompt series | Code designed, not run | Written for this repo's stack; LifeAI already has its own independently-built provider abstraction — see `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md` |
| Context Builder | Lovable prompt series | Code designed, not run | |
| 7 Lovable implementation prompts | Lovable prompt series | Not executed | Targets this repo's stack (TanStack/Supabase), not LifeAI's (Next.js/FastAPI) |

## Note on a since-corrected mix-up

FKP v1.0's version of this document presented the above as LifeAI's own built/designed status. It described LifeAI-relevant design docs (Conversation Context Resolver, AI Resource Orchestration, Adaptive Work Orchestration, candidate requirements, Life City/LifeWeb, GitHub App integration) in the same table without distinguishing which repository each item actually applies to. Those stack-agnostic design items are correctly carried forward in FKP v1.1's `03_ARCHITECTURE/BUILT_VS_DESIGNED.md` and `02_DECISIONS/CANDIDATE_REQUIREMENTS.md`; only the `savings-story-scanner`-specific "BUILT AND VERIFIED" / "DESIGNED AND TESTED LOCALLY" rows above are historical/unrelated-product material.
