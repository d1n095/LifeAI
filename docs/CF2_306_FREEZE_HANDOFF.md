# #306 formal exact-SHA code freeze

This document records the Founder-directed code freeze of `#306`.
It does **not** modify product code on `cursor/cf2-ledger-backing-classification-91c1`.
Freezing is **not** merging, deploying, Recall activation, or `first_cycle_go`.

## Frozen candidate

| Field | Value |
|---|---|
| PR | [#306](https://github.com/d1n095/LifeAI/pull/306) |
| Branch | `cursor/cf2-ledger-backing-classification-91c1` |
| Product SHA | `2444bcc3da8ffa5ca10ef3295bac13aa045769c6` |
| Parent | `#305` @ `50ca651de9648afaea31273521bba01d82aeace7` |
| Scope | CF-2 ledger backing classification; CF-1 preserved; zod `4.6.7` inherited |

Do **not** amend `#303`–`#306` product after this freeze.

## Exact-SHA CI (both required workflows)

| Event | Run | Aggregate |
|---|---|---|
| `push` | [40132328565](https://github.com/d1n095/LifeAI/actions/runs/40132328565) | `All required checks passed` = success |
| `pull_request` | [40132334370](https://github.com/d1n095/LifeAI/actions/runs/40132334370) | `All required checks passed` = success |

The 12 required jobs succeeded on **both** suites at this SHA:

- Frontend — TypeScript & ESLint
- Frontend — npm audit (patched `zod@4.6.7`; policy unchanged)
- Frontend — build (docker)
- Frontend — build (vercel)
- Backend — unit/integration tests
- Backend — RLS & session-security tests
- Backend — account lifecycle & rate-limit tests
- Backend — Alembic migration check
- E2E — Playwright (full stack)
- E2E — Library / Founder Knowledge Studio
- E2E — same-origin proxy
- All required checks passed

## Independent examination (Cursor-recorded, Claude-owned)

Claude independently confirmed:

- CF-1 remains closed (cold-start bridge persist).
- CF-2 is closed (`ledger_backing` is not stamped on real assignments; repair exists).
- No remaining Scope-A blocker on this SHA.

Claude owns **final certification**. This freeze handoff is evidence for that
certification. Cursor does not self-assert `independently_verified`.

## What this freeze is

- Exact-SHA product lock of `#306` @ `2444bcc3da8ffa5ca10ef3295bac13aa045769c6`.
- Permission for an **isolated** successor to compose this SHA with `#307`
  connectivity work. That successor is not certified by this freeze.
- Handoff packet for Claude's final exam.

## What this freeze is not

- Not a merge.
- Not a deploy.
- Not publication.
- Not Recall or autonomy GO.
- Not `first_cycle_go=true`.
- Not live vendor connectivity.
- Not a rewrite of `#306` product files.

`first_cycle_go` stays false. Codex retains Security Kernel and STOP-clear
enforcement.
