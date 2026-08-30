# MainAI — Goal Intake Path A Worker Live Report (Stage 5)

**Branch:** `cursor/goal-intake-path-a-worker`  
**Proof test:** `backend/tests/backend/mainai/test_goal_intake_path_a_worker_live.py`  
**Program:** `docs/ACTIVE_WORK_CURSOR_MAINAI_V1_COMPLETION_RUN.md`  
**Design context:** `docs/MAINAI_V1_GOAL_TO_AUTONOMY.md` (Claude #197)

## Scope boundary (explicit)

This Stage 5 proof covers **Path A only** — system-derived origination via
Document / KnowledgeClaim → interpretation → WorkCandidate →
`authorize_work_candidate`.

**Path B** (direct founder-typed `create_goal` + the founder-invoked
`propose_execution_scope` bridge for directly-created goals) is owned by
**Claude PR #197** and is **not duplicated** on this branch. Do not copy #197 Path B
route/test code here.

## What ran

Live Worker → Supervisor path after founder Path A bootstrap:

1. Document + KnowledgeClaim → interpretation proposal → promote
2. WorkCandidate appears; founder `authorize_work_candidate` creates the
   `MainAIGoal` (asserted: candidate `status=authorized`,
   `authorized_goal_id == goal.id` — not bare `create_goal` alone)
3. Path A auto-proposal (`proposal_strategy=work_candidate_authorization_v1`,
   empty `proposed_paths`) from authorize side-effect
4. Founder `create_plan` + `grant_task_approval` + authorize that Path A proposal
   (founder-supplied paths; calculator envelope)
5. Worker tick **before** spend → `PROVIDER_SPEND_NOT_AUTHORIZED` park; zero
   provider calls; no multiply edit
6. Founder `authorize_provider_spend` (fake-local only)
7. Worker ticks → Safe Planner ACCEPT → Driver → Operator edit/verify → task +
   goal `completed` via production finalize

Fake provider adapter is the sole fake boundary. Calculator helpers reused from
`test_composed_autonomy_milestone` / soak (`_candidate_payload`).

## Forbidden harness bridges (not used)

- No hand `PlanCandidate` / WorkBinding after start
- No `task.status = …` mutations after start
- No test-side `record_final_report`
- No direct `run_supervisor` / `run_driver` as top-level orchestration
- No Path B `POST …/goals/{id}/propose` bridge

## Outcome

Goal reaches `completed` with `final_outcome` set. WorkCandidate remains
`authorized` and linked to that goal. Multiply helper lands in the Supervisor
goal worktree. Idle later tick: zero extra provider calls; goal stays completed
and drops out of `eligible_authorized_goals`.

## Follow-ups (not this PR)

- Path B execution-scope bridge remains Claude #197
- Stage 6: `docs/MAINAI_V1_READINESS.md` audit
