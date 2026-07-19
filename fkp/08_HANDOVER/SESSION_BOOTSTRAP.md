# Session Bootstrap — MainAI / LifeAI
**Status:** This is FKP v1.1's operative bootstrap, adapted from the review overlay's `CORRECTED_SESSION_BOOTSTRAP.md` (which the audit, `FKP_V1_AUDIT.md` K-03, recommended replace FKP v1.0's original bootstrap) and updated with facts verified directly in this pass. **Higher operative priority than any other bootstrap-like document in this package or FKP v1.0.**
**Gäller tills direkt kodinspektion visar något annat.**

## Identity and goal

- Product: MainAI / Life OS.
- Canonical repo: `d1n095/LifeAI`.
- Deploy branch: `claude/det-kommer-mer-879lcm`, tip `f0a1975` as of this pass — verify current tip before work.
- Founder-only launch branch: `claude/founder-only-launch`, tip `e9b4b76` (based on `bf31fad`) — CI-green, independently re-reviewed, **not merged**.
- This FKP v1.1 package's own branch: `claude/fkp-v1.1`, docs-only, based on the deploy branch (deliberately excludes the founder-only-launch code changes — the two are independent).
- MainAI is founder-only and must never become a shared organizational AI.
- Founder UserAI is a separate AI identity tied to the founder's account — **does not exist in LifeAI yet**, see `01_FOUNDER_VISION/DOMAIN_AND_REQUIREMENT_MAP.md` §2 and `02_DECISIONS/CANDIDATE_REQUIREMENTS.md` §8.
- Future users will later get isolated UserAI instances; that is not Step 1 and is not scheduled.

## Current technical state — verified directly, not reported

`d1n095/LifeAI` is a Next.js (App Router) frontend + FastAPI backend + Postgres/SQLAlchemy/Alembic + pgvector + Redis product, deployed as a single combined Docker container to Render Free, with Supabase (Postgres/pgvector) and Upstash (Redis) as external free services and Strato for SMTP. **This is not** the TanStack/Bun/Supabase `savings-story-scanner`/My Money Master material — that is a different, unrelated repository, now correctly filed as historical/domain material in `10_HISTORICAL_DOMAIN_MATERIAL/`. Do not use its paths or server-function patterns in LifeAI without direct evidence they apply.

Full detail: `03_ARCHITECTURE/CURRENT_ARCHITECTURE.md`, `06_PROJECT_STATUS/CURRENT_STATUS.md`.

## Most recently reported infrastructure — not secrets

- Render web service `LifeAI` exists (`https://lifeai-1.onrender.com`); `autoDeploy: false`.
- Supabase Free project "MainAI" in Oregon, pgvector reported present (version not re-verified this pass).
- Upstash Redis Free "MainAI" in Oregon.
- Strato SMTP uses implicit SSL on port 465.
- Secrets go only into approved secrets/environment handling (Render dashboard `sync: false` variables) — never into chat or Git. This session did not request, view, or handle any secret.

All live-status claims above are carried forward as reported, not re-verified live in this pass — see `07_CONFLICTS_AND_GAPS/MISSING_INFORMATION.md`.

## Current goal: Step 1 is built — the next goal is the dependency staircase

Step 1 (founder-only launch) is built and CI-green, awaiting merge/deploy approval. The next goal, per the founder's explicit 2026-07-19 instruction, is **not** to ask the founder to choose between four open decisions (OD-02/03/04/17 — analysis exists, see `02_DECISIONS/OD_02_03_04_17_ANALYSIS.md`), but to keep moving down the dependency staircase toward knowledge import and project memory. See `09_DEPENDENCY_STAIRCASE/DEPENDENCY_STAIRCASE.md`.

## Before changing code

1. Check repo, branch, tip, status, and any local unpushed changes.
2. Read the actual auth, database, migration, and test code — don't trust a prior session's summary of it.
3. Report briefly what's already built, what's missing, and the smallest safe diff.
4. Do not ask about small, reversible details resolvable by code inspection — see the H-01 amendment in `08_HANDOVER/AGENT_CONTEXT_RULES.md`.
5. Stop and ask only for cost, external action, deploy/merge, irreversible migration, security/identity choice, or a genuinely vision-affecting choice.

## Do not do now

- Do not apply UPGRADE_26 v2 wholesale — compare against LifeAI's real schema first (see `03_ARCHITECTURE/TARGET_ARCHITECTURE.md` caveat).
- Do not run the old Lovable prompt series — it targets a different stack.
- Do not build Life City, LifeWeb, browser automation, the full agent workforce, or the Update Center yet — deferred, see `09_DEPENDENCY_STAIRCASE/SOURCE_PHASE_GATES.md` Phases 4–7.
- Do not make old `savings-story-scanner` paths binding for LifeAI.
- Do not lock any of OD-02/03/04/17's candidate answers into final architecture — analysis only exists, no decision has been made.
- Do not merge `claude/founder-only-launch` or deploy anything without explicit founder approval.

## Context behavior

- Interpret the current message first.
- Read the last 3–5 messages verbatim with roles/timestamps.
- Build a time-aware summary of the last 15.
- Let later corrections replace older assumptions.
- Understand Swedish ellipses and phrases in relation to the active task.
- State what you've actually verified; distinguish observation, hypothesis, recommendation, and decision.
- Keep working safely without unnecessary questions, per the H-01 amendment — but never pass a founder gate without approval.
