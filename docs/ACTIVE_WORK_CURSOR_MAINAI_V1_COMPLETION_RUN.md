# ACTIVE WORK — Cursor: MAINAI V1 COMPLETION RUN

**Owner:** Cursor (primary implementation lane)
**Authorized by:** Founder (2026-08-29)
**Goal:** Move MainAI materially toward usable V1 autonomous development — not another
collection of isolated hardening PRs.

## V1 target shape

FOUNDER PROVIDES: repository + bounded goal + authority + spend authorization

MAINAI THEN: understand → plan → execute → test → detect failures → repair → continue →
recover from restart/takeover → complete → stop

without a human translating every step.

## Stages (mandatory order)

| Stage | What | Status |
|---|---|---|
| 0A | #182 Window B ambiguous provider invoke | **MERGED** #196 |
| 0B | #183 heal operation identity | **MERGED** #198 |
| 0C | `_require_context` owns freshness | **MERGED** #199 |
| 0D | Genuine cancel vs finalize race | **MERGED** #200 |
| 0E | True restart + fresh DB session | **MERGED** #201 |
| 1 | Autonomous gap/repair live loop | **MERGED** #202 |
| 2 | Lease expiry + takeover continuation | **MERGED** #203 |
| 3 | Long autonomous soak (8–12 tasks) + report | **MERGED** #204 |
| 4 | First real bounded self-improvement on LifeAI | **MERGED** #205 |
| 5 | Goal intake / bootstrap production path | **MERGED** #206 |
| 6 | `docs/MAINAI_V1_READINESS.md` audit | **IN PROGRESS** — `cursor/mainai-v1-readiness-audit` |

## Landed evidence

- Stage 1–2 tests: gap/repair live loop; lease-expiry takeover
- Stage 3: `docs/MAINAI_LONG_AUTONOMY_RUN_REPORT.md`
- Stage 4: `docs/MAINAI_FIRST_SELF_IMPROVEMENT_RUN_REPORT.md`
- Stage 5: `docs/MAINAI_GOAL_INTAKE_PATH_A_REPORT.md` (Path B remains Claude #197)
- Stage 6: this PR lands `docs/MAINAI_V1_READINESS.md` + companions on tip

## Operating rules (non-negotiable)

MODEL OUTPUT != AUTHORITY. PROCESS MEMORY != AUTHORITY. UNKNOWN EXTERNAL EFFECT != NO EFFECT.
Security fixes need negative controls. Concurrency claims need real sessions. Autonomy tests
forbid hidden harness bridges. Do not stop after every PR; continue through the program.
Do not overwrite Claude-owned review/design work.

Full founder prompt text lives in the session that authorized this run; this doc is the
durable stage tracker. Update status as each stage merges.
