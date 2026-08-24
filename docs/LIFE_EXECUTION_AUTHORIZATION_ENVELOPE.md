# Life Execution Authorization Envelope (FOUNDER DECISION — execution authority model)

## Why this exists

Every foundation built so far in the closing-phase cognition chain (claims ->
interpretation -> ProjectEntity -> WorkCandidate -> MainAIGoal) answers "what should Life
work on." None of them answer the much more dangerous question: "what is Life actually
allowed to DO to the repository while working on it." `app/development_supervisor/service.py`
already has a real, working `run_supervisor()` with a `SupervisorScope` (`owner_id, goal_id,
repository_identity, allowed_paths, allowed_capabilities, maximum_risk, ...`) — but a direct
grep across the whole codebase confirms **zero non-test construction sites** for
`SupervisorScope`/`WorkBinding` exist anywhere. The Supervisor has never been reachable from
production. Before it can be, something durable and founder-governed has to decide what goes
into `allowed_paths`/`allowed_capabilities`/`maximum_risk` for a given `MainAIGoal` — and that
decision cannot be made by Life itself.

This is the FOUNDER DECISION (2026-08-xx): a fourth application of the
**SIGNAL PRODUCER != TRUTH WRITER** pattern already proven three times in this mission
(`interpretation_proposals`, `work_candidates`, and now this):

```
ProjectEntity/evidence -> WorkCandidate -> [MainAI PROPOSES a scope] -> execution_scope_proposals
                                                                              |
                                                                    [founder review]
                                                                              v
                                                              execution_authorization_envelopes
                                                                  (the ONLY authority-bearing table)
                                                                              |
                                                              MainAIGoal -> plan/tasks (narrower)
                                                                              |
                                                            SupervisorScope <- reconstructed FROM
                                                            the envelope, never invented ad hoc
                                                                              |
                                                                    run_supervisor()
```

**Founder decision summary, verbatim:** *Life may propose the authority it believes it needs.
Only the founder grants the authority. Tasks receive narrower delegated authority. Life may
never expand its own authorized envelope.*

## PROPOSED_SCOPE != AUTHORIZED_SCOPE

`execution_scope_proposals` (migration 0057) is structurally incapable of granting authority:
it has no `authorized_by`, no code path treats it as executable, and nothing in the codebase
ever constructs a `SupervisorScope` from a proposal directly. `app/work_candidates/service.py`'s
`authorize_work_candidate()` calls a new, SAVEPOINT-isolated, non-fatal helper,
`_propose_execution_scope_if_actionable()`, which derives a *proposed* capability set from the
`ProjectEntity.entity_type` (`task_reference`/`decision` -> `repo_read, repo_edit, run_tests`;
`idea` -> `repo_read` only; anything else -> no proposal at all) — this is exactly the "AI/
planner/classifier may suggest, never self-grant" boundary from the founder decision.
`proposed_paths` defaults to `[]`: MainAI does not guess file paths from a claim alone, it
proposes only what it actually has evidence for.

## The one path to real authority: `authorize_execution_scope()`

`app/execution_envelopes/service.py`'s `authorize_execution_scope()` is the only function
capable of writing to `execution_authorization_envelopes`, and it requires the caller to
supply `authorized_by`, `authorized_paths`, `authorized_capabilities`, and `authorized_risk`
explicitly — it never copies the proposal's own values. `app/routers/execution_envelopes.py`
is the only production caller, and it hardcodes `authorized_by="founder"` server-side exactly
like `app/routers/project_entities.py` already establishes; `authorized_paths` /
`authorized_capabilities` / `authorized_risk` ARE accepted from the request body, because
those are the founder's own content-level judgment call (accept as proposed, narrow, or
explicitly expand) — not an identity a client could spoof.

## Never mutate, always supersede

Re-authorizing a goal that already has an active envelope does not edit the old row: it marks
the prior envelope `status="superseded"`, sets the new envelope's `supersedes_envelope_id` to
point at it, and inserts a brand-new `status="active"` row. `get_current_execution_envelope()`
only ever returns the single `status="active"` row for a goal (or `None`); the full audit
trail remains readable via `list_execution_authorization_envelopes()`. History is never
rewritten — matching the same discipline already established for `ProjectEntity` supersession
in `docs/LIFE_PROJECT_ENTITIES_INTERPRETATION.md`.

## Ownership: composite, owner-anchored FKs from day one

Both tables use `ForeignKeyConstraint`s against `mainai_goals(id, owner_id)` — the composite
`UNIQUE(id, owner_id)` constraint (`uq_mainai_goals_id_owner_id`) already added by migration
0032, reused directly rather than requiring a new constraint. `execution_authorization_
envelopes` additionally has its own `UNIQUE(id, owner_id)` so `execution_scope_proposals.
authorized_envelope_id` and `execution_authorization_envelopes.supersedes_envelope_id` can
both be owner-anchored composite FKs too. This applies the PR #140 lesson
("existing precedent is not automatically correct precedent" — bare FKs create a
cross-owner-reference vulnerability even when RLS covers the row's own `owner_id`) proactively,
before any review caught it here. `tests/security/test_rls_isolation_execution_envelopes.py`
proves this at the database layer specifically: `test_cannot_reference_another_owners_goal_
from_an_execution_scope_proposal` sets `owner_id` to the attacking session's own id (passes
RLS's `WITH CHECK` on the row itself) but points `goal_id` at another owner's goal — and the
composite FK, not RLS, is what rejects it.

## What is authorized here, and what is explicitly NOT built yet

This foundation makes the **proposed scope -> founder-authorized envelope** edge itself
**RUNTIME REACHABLE** end to end: `tests/backend/test_execution_envelopes_api.py::
test_real_claim_to_authorized_execution_envelope_through_both_founder_apis_end_to_end` proves a
real claim -> interpretation proposal -> (via `app/routers/project_entities.py`) promoted
`ProjectEntity` -> auto-derived `WorkCandidate` -> (via the same founder API) authorized into a
real `MainAIGoal`, whose authorization side-effect auto-proposes an execution scope -> (via
`app/routers/execution_envelopes.py`) founder-authorized into a real, active
`ExecutionAuthorizationEnvelope` — every governed step through a real HTTP request with real
founder authentication.

**Explicitly NOT built by this foundation** (the separate, larger piece the founder decision's
point 9-11 describes, to be built next): there is still no durable worker trigger that
reconstructs a `SupervisorScope` from an authorized envelope, derives bounded `WorkBinding`s,
and calls `run_supervisor()`. An authorized envelope existing does not yet cause any autonomous
execution to happen — it only makes such execution possible to build safely on top of. Until
that trigger exists, this foundation is **RUNTIME REACHABLE for the authorization edge**, not
**PRODUCTION E2E PROVEN for autonomous execution** — those are different claims and must not be
conflated.

## Goal-level ceiling, task-level narrowing (for the next piece to honor)

The envelope this foundation produces is the **maximum** for a goal, not a grant to any
individual task: `task.allowed_paths ⊆ envelope.authorized_paths`,
`task.allowed_capabilities ⊆ envelope.authorized_capabilities`,
`task.risk ≤ envelope.authorized_risk`. Effective execution authority for any single task must
be the narrowest intersection of (Authorized Goal Envelope) ∩ (SupervisorScope) ∩
(WorkBinding) ∩ (OperatorContext) — never a union. If a later plan requires more than the
envelope grants, the correct behavior is to block and surface a structured delta for founder
approval, never to silently expand the envelope. This foundation does not implement that
enforcement itself (there is no task-level consumer yet) — it exists here as the contract the
Supervisor-wiring piece must uphold, and as the reason `execution_authorization_envelopes` was
built as a strict ceiling-defining table rather than anything resembling a second, competing
autonomy/control plane.

## Existing goals without an envelope

Goals created before this foundation existed (or goals nobody has authorized) simply have no
`status="active"` row — `get_current_execution_envelope()` returns `None`. This is not treated
as an error or backfilled with an invented default; those goals remain valid for existing
manual/non-autonomous flows, they are just **not eligible for autonomous Supervisor execution**
until a founder explicitly authorizes one. Fail closed, additive, never retroactive.

## Endpoints

All under `/api/execution-envelopes`, all gated by `Depends(require_founder)`:

| Method | Path | Purpose |
|---|---|---|
| GET | `/proposals` | List (optional `status_filter`, `goal_id`) |
| GET | `/proposals/{id}` | Read one |
| POST | `/proposals/{id}/authorize` | The ONE path to a real, active envelope |
| POST | `/proposals/{id}/reject` | Mark reviewed-and-rejected, durable |
| GET | `/current?goal_id=` | The single active envelope for a goal, or `null` |
| GET | `/history?goal_id=` | Full audit history including superseded envelopes |
| GET | `/{envelope_id}` | Read one envelope |

No "create proposal" route exists, deliberately, matching
`docs/LIFE_PROJECT_ENTITIES_FOUNDER_API.md`'s own precedent: proposals are never a manual API
action in production, they are created automatically by `authorize_work_candidate()`'s own live
side effect.

## Test coverage

- `tests/backend/mainai/test_execution_envelopes.py` (14): structural non-authority of
  proposals, empty-paths-by-default, idempotency, DB `CHECK` on `proposed_risk`, authorization
  requires the caller's own explicit values, narrow/expand, double-authorization rejected,
  reject never deletes, supersede-never-mutate with full history verification, `None` when
  never authorized, list filtering, cross-owner fail-closed at both the proposal level and the
  `goal_id`-reference level.
- `tests/security/test_rls_isolation_execution_envelopes.py` (5): row-level cross-owner
  isolation for both tables at the database layer through the restricted `mainai_app` role,
  plus the reference-level cross-owner attack described above.
- `tests/backend/mainai/test_work_candidate_execution_scope_capture.py` (3): the live wiring
  from `authorize_work_candidate()`, including the non-fatal SAVEPOINT-isolation guarantee (a
  monkeypatched failure in the proposal side effect never breaks the goal authorization itself).
- `tests/backend/test_execution_envelopes_api.py` (15): auth, spoofing-proof (`authorized_by`/
  `owner_id` in the request body have zero effect), fails closed on nonexistent proposals/
  envelopes, exactly-one-route-can-authorize proof, narrow-through-the-API, reject durability,
  and the full production E2E chain described above.

## Explicitly deferred

- No frontend UI — API/service layer only, matching every other foundation in this mission.
- No Supervisor production entry trigger yet — see "What is authorized here" above. That is
  the next piece, to be built directly on top of this foundation without a further founder
  design-approval pause, per the founder decision's own instruction: *"Build the smallest
  coherent implementation, test it, then continue into the real Supervisor production entry."*
- No pagination beyond `status_filter`/`goal_id` query params, matching the same "smallest
  surface that makes the chain reachable" judgment already applied to the sibling founder API.
