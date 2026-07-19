# Module Register — `savings-story-scanner` / My Money Master (HISTORICAL — NOT LifeAI)
**⚠️ This document describes a different, unrelated repository (`d1n095/savings-story-scanner`), not `d1n095/LifeAI`. See `10_HISTORICAL_DOMAIN_MATERIAL/README.md`. For LifeAI's actual module register, see `04_PRODUCT_AND_MODULES/MODULE_REGISTER.md`.**

**Sources:** `15_EXISTING_LIFE_OS_MODULES_AND_PRODUCT_SCOPE.md`, `03_COMPLETE_DOMAIN_AND_REQUIREMENT_MAP.md`, `MY_MONEY_MASTER_SPEC_V3.md`, `My_Money_Master.zip` (from the original FKP v1 raw-source archive).

---

## Modules (verified in `savings-story-scanner`'s repository)

| Module | Status | Location | Notes |
|--------|--------|----------|-------|
| Salary / OB / shift calculation | BUILT ✅ | `src/modules/salary/` | compute.ts, ob.ts, breaks.ts, conflicts.ts, parser.ts, templates.ts |
| Calendar | BUILT ✅ | `src/modules/calendar/` | holidays.ts, namedays.ts, source.ts |
| Planning / rotations | BUILT ✅ | `src/modules/planning/` | rotations.ts, vacation.ts, views.ts, tax.ts |
| Finance / score | BUILT ✅ | `src/modules/finance/` | score.ts |
| Schedule OCR (Gemini Flash) | BUILT ✅ | `src/lib/schedule-ocr.functions.ts` | Part of this repo, unrelated to LifeAI's own RAG/OCR-equivalent work |
| Dashboard | BUILT ✅ | `src/routes/_app/dashboard.tsx` | |
| Today (idag) | BUILT ✅ | `src/routes/_app/idag.tsx` | |
| Work (jobb) | BUILT ✅ | `src/routes/_app/jobb.tsx` | |
| Calendar view | BUILT ✅ | `src/routes/_app/kalender.tsx` | |
| Finance view | BUILT ✅ | `src/routes/_app/pengar.tsx` | |
| Planning view | BUILT ✅ | `src/routes/_app/planering.tsx` | |
| Import | BUILT ✅ | `src/routes/_app/importera.tsx` | |
| Insights | BUILT ✅ | `src/routes/_app/insikter.tsx` | |
| Settings | BUILT ✅ | `src/routes/_app/installningar.*` | |

## Note on a since-corrected mix-up

FKP v1.0's `MODULE_REGISTER.md` presented this table as LifeAI's own "Tier 2 — Life OS modules (existing, built)." It was not — these modules belong to `savings-story-scanner`/My Money Master, a separate product. LifeAI's actual module inventory (auth, RAG, provider abstraction, conversations, documents, knowledge search, projects, admin, founder-only launch) is in `04_PRODUCT_AND_MODULES/MODULE_REGISTER.md`. The salary/calendar/planning/finance domain represented here remains legitimate future-scope material for Life OS's broader vision (see `01_FOUNDER_VISION/DOMAIN_AND_REQUIREMENT_MAP.md` §12), should the founder ever choose to bring equivalent functionality into LifeAI — but nothing here should be read as already existing in LifeAI's codebase.
