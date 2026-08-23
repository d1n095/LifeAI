# Life Project Entities / Work Candidates — Founder API (production reachability)

## What this closes

`docs/LIFE_PROJECT_ENTITIES_INTERPRETATION.md` (migration 0054) and
`docs/LIFE_WORK_CANDIDATES.md` (migration 0055) built two governed manual edges in the
closing-phase cognition chain:

```
interpretation_proposal  -> [founder review/promotion]  -> ProjectEntity
WorkCandidate             -> [founder authorization]     -> MainAIGoal
```

Both were **DOMAIN COMPONENT PROVEN** (`tests/backend/mainai/test_project_entities.py`,
`test_work_candidates.py`) and **SERVICE COMPOSITION PROVEN**
(`tests/backend/mainai/test_claims_to_goal_composed_chain.py`, which calls
`promote_interpretation_proposal()`/`authorize_work_candidate()` directly with `superuser_db`
and `authorized_by="founder"` supplied by the test itself) — but neither was **RUNTIME
REACHABLE**: without a real API route, no actual production request could ever traverse
either edge. `app/routers/project_entities.py` closes that gap. This document distinguishes
those levels of proof deliberately — see `docs/CURSOR_ADVERSARIAL_RUNTIME_LANE_HANDOFF.md`'s
own `CODE EXISTS != CAPABILITY EXISTS` heuristic for why the distinction matters.

## Endpoints

All under `/api/project-entities`, all gated by `Depends(require_founder)` at the router
level (same `dependencies=[...]` pattern `app/routers/mainai_execution.py` already uses):

| Method | Path | Purpose |
|---|---|---|
| GET | `/interpretation-proposals` | List (optional `status_filter`) |
| GET | `/interpretation-proposals/{id}` | Read one |
| POST | `/interpretation-proposals/{id}/promote` | The ONE path to a real `ProjectEntity` |
| POST | `/interpretation-proposals/{id}/dismiss` | Mark reviewed-and-rejected, durable |
| GET | `/entities` | List current (optional `entity_type`) |
| GET | `/entities/{id}` | Read one |
| GET | `/work-candidates` | List (optional `status_filter`) |
| GET | `/work-candidates/{id}` | Read one |
| POST | `/work-candidates/{id}/authorize` | The ONE path to a real `MainAIGoal` |
| POST | `/work-candidates/{id}/dismiss` | Mark reviewed-and-rejected, durable |

No "create proposal"/"create candidate" route exists, deliberately: those are never manual
API actions in production either — `interpretation_proposals` are created automatically by
`app/rag/claims.py`'s live extraction wiring, and `work_candidates` are created automatically
by `promote_interpretation_proposal()`'s own live side effect. This router only exposes the
steps that genuinely require a human founder decision.

## Authority cannot be client-supplied

`owner_id`, `authority`/`basis` (on promotion), and `authorized_by` (on authorization) are
**never** accepted from the request body — every route derives them from `user.id`
(`Depends(require_founder)`, which verifies both `role == founder` AND the fixed
`FOUNDER_USER_ID`, see `app/deps.py`) and hardcodes `authority="founder"`, `basis="manual"`,
`authorized_by="founder"` server-side. A client submitting `{"authority": "ai_interpretation",
"owner_id": "<anything>", "authorized_by": "<anything>"}` has every one of those fields
silently ignored — proven by `test_owner_id_in_the_request_body_is_ignored_...`,
`test_authority_cannot_be_set_by_the_client_...`, and
`test_authorized_by_cannot_be_set_by_the_client` in `tests/backend/test_project_entities_api.py`.

This is not a weakening of `promote_interpretation_proposal()`/`authorize_work_candidate()`'s
own "caller must supply explicit authority" discipline — it is the one place in the system
that has actually verified the caller IS the founder, so it is the one place allowed to
supply `"founder"` and have it mean something. Matches
`app/routers/mainai_execution.py`'s own `create_goal(..., created_by="founder")` precedent
exactly.

## Test coverage (`tests/backend/test_project_entities_api.py`)

- **Auth**: every endpoint requires authentication; an ordinary member is denied; a row that
  merely claims `role=founder` but isn't the fixed `FOUNDER_USER_ID` is denied; the actual
  founder succeeds.
- **Spoofing**: `owner_id`/`authority`/`basis`/`authorized_by` in the request body have zero
  effect on the recorded result.
- **Fails closed**: promoting a nonexistent proposal, reading a nonexistent entity, promoting
  an already-dismissed proposal.
- **The governed edges themselves**: exactly one route can produce a `ProjectEntity`, exactly
  one route can produce a `MainAIGoal`; successful promotion produces the expected
  `WorkCandidate`; successful authorization produces the expected `MainAIGoal`; the dismiss
  path is durable (never deletes, remains readable).
- **`test_real_source_claim_to_real_authorized_goal_through_the_founder_api_end_to_end`**: the
  actual PRODUCTION E2E proof for these two edges specifically — a real claim, a real
  interpretation proposal (created the way production creates one), then every remaining step
  through the real `TestClient` with real founder authentication. The only steps not going
  through an HTTP route are claim extraction and proposal recording, because those are
  intentionally never manual actions in production either.

## Explicitly deferred

- No frontend UI ("Tolkningskö") — API/service layer only, matching every other foundation in
  this mission.
- No pagination/filtering beyond a single `status_filter`/`entity_type` query param — this is
  the smallest surface that makes the chain reachable, not a full review-queue product.
- No rate limiting beyond whatever `SlowAPIMiddleware`'s global default already applies —
  `app/routers/mainai_execution.py`'s own goal-creation route does add an explicit
  `@limiter.limit(...)`; this router's actions are founder-only, low-frequency, reviewed
  decisions, not a high-volume path, so the global default is judged sufficient for now.
