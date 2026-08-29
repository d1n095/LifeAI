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
| 3 | Long autonomous soak (8–12 tasks) + report | **IN PROGRESS** — `cursor/long-autonomy-soak-v1` |
| 4 | First real bounded self-improvement on LifeAI | waiting |
| 5 | Goal intake / bootstrap production path | waiting |
| 6 | `docs/MAINAI_V1_READINESS.md` audit | waiting |

## Stage 1 proof target (landed in #202)

Worker → Supervisor only (no harness bridges):

verification failure → structured gap → repair child → narrowed authority → repair executes →
source re-verifies → downstream dependency unlocks → goal continues.

Allowed founder edges: envelope/spend authorize; `grant_task_approval` for repair
`repo_edit` under `autonomous_development_work` (listed explicitly).

Forbidden harness: hand repair task / WorkBinding / PlanCandidate / status mutation /
dependency unlock / final report.

## Stage 2 proof target (landed in #203)

Real `supervisor_goal_leases` crash-hold → wall-clock expiry → Worker B reclaim (generation
bump) → goal continues; old worker ZERO further filesystem effect. Fresh Session B after
Session A closes (PROCESS MEMORY != AUTHORITY; ORM SESSION MEMORY != AUTHORITY).

## Stage 3 proof target

8-task Worker soak composing transient failure, out-of-scope deny, gap/repair, session
restart, lease takeover, finalize, and idle ZERO-effect ticks. Report:
`docs/MAINAI_LONG_AUTONOMY_RUN_REPORT.md`.

## Operating rules (non-negotiable)

MODEL OUTPUT != AUTHORITY. PROCESS MEMORY != AUTHORITY. UNKNOWN EXTERNAL EFFECT != NO EFFECT.
Security fixes need negative controls. Concurrency claims need real sessions. Autonomy tests
forbid hidden harness bridges. Do not stop after every PR; continue through the program.
Do not overwrite Claude-owned review/design work.

Full founder prompt text lives in the session that authorized this run; this doc is the
durable stage tracker. Update status as each stage merges.
