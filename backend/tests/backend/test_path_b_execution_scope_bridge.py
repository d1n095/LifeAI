"""Path B execution-scope bridge (V1 readiness gap #0, docs/MAINAI_V1_GOAL_TO_AUTONOMY.md).

Before this: a goal created directly via POST /api/mainai/execution/goals ("Path B") could be
planned/decomposed but had NO route to ever create an execution-scope proposal -- it could
never reach real execution authority through any real API interaction. Path A (a goal derived
from a founder-authorized WorkCandidate) got a proposal automatically as a side effect of
authorize_work_candidate(); Path B had nothing analogous.

This suite proves the new bridge (POST /api/execution-envelopes/goals/{goal_id}/propose)
closes the gap, end to end, through the real production chain:

  POST /api/mainai/execution/goals            (goal created)
  -> POST /api/mainai/execution/goals/{id}/plan  (AI decomposition, fake provider only)
  -> POST /api/execution-envelopes/goals/{id}/propose   (THE NEW BRIDGE -- proposal only)
  -> POST /api/execution-envelopes/proposals/{id}/authorize  (founder's own explicit grant)
  -> POST /api/mainai/execution/tasks/{id}/approve   (task approval)
  -> eligible_authorized_goals() includes the goal    (Supervisor CAN now pick it up)

No test-side manual SupervisorScope/WorkBinding/envelope/task-status/authority mutation
anywhere in this file -- every state transition is a real HTTP call or a real service
function called the same way its own router calls it.
"""

import pytest

from app.development_supervisor.production_entry import eligible_authorized_goals
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"

_PLAN_JSON = (
    '{"tasks": [{"description": "audit the repo", "task_type": "read_only_audit", "depends_on": []}]}'
)


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _create_and_plan_goal(client, headers) -> tuple[str, str]:
    """Real API calls: goal creation + AI decomposition (fake provider only). Returns
    (goal_id, task_id) for the single created task."""
    create_res = client.post(
        "/api/mainai/execution/goals",
        json={"title": "Path B bridge goal", "original_instruction": "Audit the repository for a tiny doc issue."},
        headers=headers,
    )
    assert create_res.status_code == 201, create_res.text
    goal_id = create_res.json()["id"]

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content=_PLAN_JSON, provider="openai", model=model, raw_usage={})

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(OpenAIProvider, "chat", _fake_chat)
        plan_res = client.post(f"/api/mainai/execution/goals/{goal_id}/plan", headers=headers)
    assert plan_res.status_code == 201, plan_res.text

    tasks_res = client.get(f"/api/mainai/execution/goals/{goal_id}/tasks", headers=headers)
    assert tasks_res.status_code == 200
    tasks = tasks_res.json()
    assert len(tasks) == 1
    return goal_id, tasks[0]["id"]


def test_full_path_b_chain_reaches_real_execution_authority(client, superuser_db):
    """The complete production-shaped E2E: goal -> decomposition -> proposal (new bridge) ->
    founder authorization -> task approval -> Supervisor-eligible."""
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}

    goal_id, task_id = _create_and_plan_goal(client, headers)

    # THE NEW BRIDGE. Founder's own stated proposal -- carries zero authority by itself.
    propose_res = client.post(
        f"/api/execution-envelopes/goals/{goal_id}/propose",
        json={"proposed_paths": ["README.md"], "proposed_capabilities": ["read_file"], "proposed_risk": "low"},
        headers=headers,
    )
    assert propose_res.status_code == 201, propose_res.text
    proposal = propose_res.json()
    assert proposal["status"] == "unreviewed"
    assert proposal["goal_id"] == goal_id

    # Founder's own separate, explicit authorization -- accepting the proposal as-is here,
    # but authorize_execution_scope() never copies it automatically (proven by test #2 below).
    authorize_res = client.post(
        f"/api/execution-envelopes/proposals/{proposal['id']}/authorize",
        json={"authorized_paths": ["README.md"], "authorized_capabilities": ["read_file"], "authorized_risk": "low"},
        headers=headers,
    )
    assert authorize_res.status_code == 201, authorize_res.text
    envelope = authorize_res.json()
    assert envelope["authorized_paths"] == ["README.md"]
    assert envelope["authorized_capabilities"] == ["read_file"]

    # Task approval -- the last founder-governed step.
    approve_res = client.post(f"/api/mainai/execution/tasks/{task_id}/approve", headers=headers)
    assert approve_res.status_code == 200, approve_res.text

    # Real production eligibility check -- the exact function Worker.run_once()'s poll loop
    # calls, on the SAME connection it genuinely uses (the superuser/_ClaimSession connection
    # -- eligible_authorized_goals() must see every owner's rows before any owner is known,
    # which no per-request RLS context could satisfy; confirmed via app/worker.py's own
    # module-level comment on _ClaimSession and its call site inside run_once()).
    from app.founder import FOUNDER_USER_ID

    goals = eligible_authorized_goals(superuser_db)
    goal_ids = {str(g.id) for g, _env in goals}
    assert goal_id in goal_ids, "goal must now be genuinely eligible for autonomous Supervisor pickup"
    matched_env = next(env for g, env in goals if str(g.id) == goal_id)
    assert matched_env.authorized_paths == ["README.md"]
    assert matched_env.owner_id == FOUNDER_USER_ID


def test_negative_1_no_founder_approval_means_zero_consequential_effect(client, superuser_db):
    """A proposal alone, never authorized, must never make the goal Supervisor-eligible."""
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}
    goal_id, _task_id = _create_and_plan_goal(client, headers)

    propose_res = client.post(
        f"/api/execution-envelopes/goals/{goal_id}/propose",
        json={"proposed_paths": ["README.md"], "proposed_capabilities": ["read_file"], "proposed_risk": "low"},
        headers=headers,
    )
    assert propose_res.status_code == 201, propose_res.text

    goals = eligible_authorized_goals(superuser_db)
    assert goal_id not in {str(g.id) for g, _env in goals}


def test_negative_2_proposed_scope_broader_than_founder_grants_never_widens_effective_authority(
    client, db_session
):
    """Even if the proposal (founder's own stated suggestion, or in principle a future
    provider-derived one) requests broad paths/capabilities, the founder's own authorization
    call is what actually grants authority -- and here we deliberately authorize NARROWER than
    proposed, proving the envelope reflects the founder's explicit grant, never the proposal's
    content."""
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}
    goal_id, _task_id = _create_and_plan_goal(client, headers)

    propose_res = client.post(
        f"/api/execution-envelopes/goals/{goal_id}/propose",
        json={
            "proposed_paths": ["README.md", "backend/", "app/development_operator/service.py"],
            "proposed_capabilities": ["read_file", "patch_file", "create_file", "delete_file"],
            "proposed_risk": "high",
        },
        headers=headers,
    )
    assert propose_res.status_code == 201, propose_res.text
    proposal = propose_res.json()
    assert proposal["proposed_capabilities"] == ["read_file", "patch_file", "create_file", "delete_file"]

    # Founder grants far LESS than what was proposed.
    authorize_res = client.post(
        f"/api/execution-envelopes/proposals/{proposal['id']}/authorize",
        json={"authorized_paths": ["README.md"], "authorized_capabilities": ["read_file"], "authorized_risk": "low"},
        headers=headers,
    )
    assert authorize_res.status_code == 201, authorize_res.text
    envelope = authorize_res.json()
    assert envelope["authorized_paths"] == ["README.md"]
    assert envelope["authorized_capabilities"] == ["read_file"]
    assert envelope["authorized_risk"] == "low"
    assert "backend/" not in envelope["authorized_paths"]
    assert "delete_file" not in envelope["authorized_capabilities"]


def test_negative_3_cancel_before_approval_means_no_executable_authority_appears(client, superuser_db):
    """Rejecting the proposal instead of authorizing it must leave the goal with no
    executable authority -- same observable outcome as never proposing at all."""
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}
    goal_id, _task_id = _create_and_plan_goal(client, headers)

    propose_res = client.post(
        f"/api/execution-envelopes/goals/{goal_id}/propose",
        json={"proposed_paths": ["README.md"], "proposed_capabilities": ["read_file"], "proposed_risk": "low"},
        headers=headers,
    )
    assert propose_res.status_code == 201, propose_res.text
    proposal_id = propose_res.json()["id"]

    reject_res = client.post(
        f"/api/execution-envelopes/proposals/{proposal_id}/reject",
        json={"reason": "founder changed their mind"},
        headers=headers,
    )
    assert reject_res.status_code == 200, reject_res.text
    assert reject_res.json()["status"] == "rejected"

    goals = eligible_authorized_goals(superuser_db)
    assert goal_id not in {str(g.id) for g, _env in goals}

    # A REJECTED proposal cannot be authorized after the fact either.
    authorize_res = client.post(
        f"/api/execution-envelopes/proposals/{proposal_id}/authorize",
        json={"authorized_paths": ["README.md"], "authorized_capabilities": ["read_file"], "authorized_risk": "low"},
        headers=headers,
    )
    assert authorize_res.status_code == 400


def test_negative_4_retrying_the_same_proposal_content_never_duplicates_or_widens(client, db_session):
    """The new route's content-hash idempotency key: an accidental retry with IDENTICAL
    payload is a true no-op (same row, not a second proposal); a DELIBERATE new proposal with
    DIFFERENT content after a rejection is never blocked by history."""
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}
    goal_id, _task_id = _create_and_plan_goal(client, headers)

    payload = {"proposed_paths": ["README.md"], "proposed_capabilities": ["read_file"], "proposed_risk": "low"}
    first = client.post(f"/api/execution-envelopes/goals/{goal_id}/propose", json=payload, headers=headers)
    assert first.status_code == 201
    retry = client.post(f"/api/execution-envelopes/goals/{goal_id}/propose", json=payload, headers=headers)
    assert retry.status_code == 201
    assert retry.json()["id"] == first.json()["id"], "identical retry must return the SAME row, not a duplicate"

    # Reject, then propose genuinely DIFFERENT content -- must succeed, not be blocked.
    client.post(
        f"/api/execution-envelopes/proposals/{first.json()['id']}/reject",
        json={"reason": "reconsidering"},
        headers=headers,
    )
    different_payload = {"proposed_paths": ["docs/"], "proposed_capabilities": ["read_file"], "proposed_risk": "low"}
    second = client.post(f"/api/execution-envelopes/goals/{goal_id}/propose", json=different_payload, headers=headers)
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]
    assert second.json()["status"] == "unreviewed"


# Negative test #5 (concurrent scope-proposal creation -> one canonical result, no
# IntegrityError) is a real two-thread/two-connection race, matching this session's
# established gold-standard pattern -- covered at the service layer, not duplicated here, in
# tests/backend/mainai/test_execution_envelopes.py::
# test_two_genuinely_concurrent_proposals_with_the_same_idempotency_key_converge_on_one_canonical_row
