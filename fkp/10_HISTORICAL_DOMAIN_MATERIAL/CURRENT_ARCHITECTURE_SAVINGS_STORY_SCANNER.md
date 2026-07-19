# Current Architecture — `savings-story-scanner` / My Money Master (HISTORICAL — NOT LifeAI)
**⚠️ This document describes a different, unrelated repository (`d1n095/savings-story-scanner`), not `d1n095/LifeAI`. See `10_HISTORICAL_DOMAIN_MATERIAL/README.md`. For LifeAI's actual current architecture, see `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md`.**

**Sources:** `My_Money_Master.zip` (actual repository code), `REPO_INVENTORY_savings-story-scanner.md` (a prior session)
**Original warning (kept for record):** Verified against `d1n095/savings-story-scanner` commit `289420342b`. Whether `d1n095/LifeAI` matches was flagged UNVERIFIED in FKP v1.0 (OD-14) — **now resolved: it does not match, they are different products.** See `07_CONFLICTS_AND_GAPS/CONFLICT_REGISTER.md` CONFLICT-01.

---

## Stack (verified, for `savings-story-scanner`)
- **Framework:** TanStack Start (React 19), Bun runtime
- **Router:** TanStack Router v1.170.16
- **Database:** Supabase (PostgreSQL + Auth + Storage), @supabase/supabase-js ^2.108.2
- **UI:** Tailwind CSS + Shadcn/UI (50+ components) + Lucide icons
- **TypeScript:** ^5.9.3

## Server function pattern (for `savings-story-scanner` — does not apply to LifeAI's FastAPI backend)
```
createServerFn(...)
  .middleware([requireSupabaseAuth])
  .validator(zodSchema)
  .handler(async ({ data, context }) => { ... })
```
- `requireSupabaseAuth`: `src/integrations/supabase/auth-middleware.ts`
- Client: `src/integrations/supabase/client.ts`
- Server client (service role — isolated): `src/integrations/supabase/client.server.ts`

## Existing routes (`src/routes/`)
`__root.tsx`, `_app.tsx` (layout + nav), `auth.tsx` and variants, `_app/dashboard.tsx`, `_app/idag.tsx`, `_app/importera.tsx`, `_app/insikter.tsx`, `_app/jobb.tsx`, `_app/kalender.tsx`, `_app/pengar.tsx`, `_app/planering.tsx`, `_app/installningar.*`

## Existing modules (`src/modules/`)
- `salary/`: compute.ts, ob.ts, breaks.ts, conflicts.ts, parser.ts, templates.ts
- `planning/`: rotations.ts, tax.ts, vacation.ts, views.ts
- `calendar/`: holidays.ts, namedays.ts, source.ts
- `finance/`: score.ts

## Existing AI/OCR feature
`src/lib/schedule-ocr.functions.ts` — calls `ai.gateway.lovable.dev` (Lovable's AI Gateway) with Gemini Flash for schedule-image OCR. This is part of `savings-story-scanner`, unrelated to LifeAI's MainAI work.

## Database migrations (11 files in `supabase/migrations/`)
Covers: profiles, user_roles (`has_role()`, `update_updated_at_column()`), shifts, expenses, timeline_events, signals, ai_memory (simple: topic/fact/confidence — a different, much simpler thing than LifeAI's or UPGRADE_26 v2's knowledge model), reminders, templates, absences, weekly patterns, rotations, vacation_balance, teams, work_profiles, user_defaults. Two additional Fas A migrations.

## What did NOT exist in this repo (as of the original inventory)
- No `project` or `workspace` concept — all tables scoped directly via `user_id`
- No MainAI tables, routes, or server functions
- No GitHub App integration
- No AI provider abstraction layer (LifeAI has since built one, independently — see `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md`)

## Note on a since-corrected mix-up
FKP v1.0 included a "reported deployment state" section here citing `d1n095/LifeAI`, `claude/det-kommer-mer-879lcm`, `lifeai-1.onrender.com`, and other LifeAI-specific infrastructure details as if they were part of this same document/repository. That was the exact conflation this historical folder exists to correct — those details belong to LifeAI, not to `savings-story-scanner`, and now live in `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md` and `06_PROJECT_STATUS/CURRENT_STATUS.md` instead.
