"""Founder-driven recovery of a parked, envelope-governed goal -- no direct DB manipulation.

The F1 park (`supervisor_config_refusal_park`) is woken by exactly one thing: a NEW active
execution envelope for the same goal. Until these routes existed the only way to produce one
for an UNCHANGED scope was to edit Postgres by hand, because `propose_execution_scope()` is
idempotent on identical content and returns the already-`authorized` proposal, which
`authorize_execution_scope()` refuses to authorize twice. That made "widen the scope" the only
API-reachable way to resume a parked goal -- exactly the wrong incentive for a security
boundary.

Three founder acts, each narrow on purpose:

  * `PATCH /api/mainai/execution/goals/{id}/approval-policy` -- correct the one setting the
    refusal names, audited in `audit_log` in the same transaction;
  * `POST /api/execution-envelopes/goals/{id}/reauthorize` -- re-issue the CURRENT scope as a
    new envelope (content copied from the active row, so it structurally cannot widen);
  * `POST /api/mainai/execution/goals/{id}/resume-parked-tasks` -- run the existing durable
    wake reconciliation now, instead of waiting for the next Supervisor tick.

Nothing here is a second work engine or a second approval path.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.execution_envelopes import (
    authorize_execution_scope,
    get_current_execution_envelope,
    propose_execution_scope,
)
from app.founder import FOUNDER_USER_ID
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.mainai_execution.provider_wait_wake import (
    SUPERVISOR_REFUSAL_PARK_BLOCKER,
    SUPERVISOR_REFUSAL_PARK_REASON,
    is_supervisor_refusal_park,
)
from app.models.audit import AuditLog
from app.models.mainai_execution import MainAIGoal, MainAITask, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.request_context import current_user_id as current_user_id_var

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
PATHS = ["calculator.py", "test_calculator.py"]
CAPABILITIES = ["repo_read", "repo_edit", "run_tests"]


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def _login(client, email=FOUNDER_EMAIL, password=FOUNDER_PASSWORD) -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _goal(db_session, *, approval_policy="standard_repo_work") -> MainAIGoal:
    _set_rls_user(db_session, FOUNDER_USER_ID)
    goal = create_goal(
        db_session,
        owner_id=FOUNDER_USER_ID,
        title="parked goal recovery",
        original_instruction="Lägg till en subtract-funktion i calculator.py.",
        created_by="test",
        approval_policy=approval_policy,
    )
    create_plan(
        db_session,
        goal=goal,
        rationale="single task",
        tasks=[PlannedTaskSpec(description="Lägg till en subtract-funktion i calculator.py", task_type="repo_edit")],
        created_by="test",
    )
    db_session.commit()
    return goal


def _authorized_envelope(db_session, goal) -> uuid.UUID:
    _set_rls_user(db_session, FOUNDER_USER_ID)
    proposal = propose_execution_scope(
        db_session,
        owner_id=FOUNDER_USER_ID,
        goal_id=goal.id,
        idempotency_key=f"recovery-test-{uuid.uuid4()}",
        proposed_paths=PATHS,
        proposed_capabilities=CAPABILITIES,
        proposed_risk="low",
    )
    db_session.commit()
    _set_rls_user(db_session, FOUNDER_USER_ID)
    _, envelope = authorize_execution_scope(
        db_session,
        owner_id=FOUNDER_USER_ID,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=PATHS,
        authorized_capabilities=CAPABILITIES,
        authorized_risk="low",
        envelope_idempotency_key=f"recovery-envelope-{uuid.uuid4()}",
    )
    db_session.commit()
    return envelope.id


def _parked_task(db_session, goal, envelope_id) -> MainAITask:
    """A task in exactly the state `park_tasks_after_supervisor_refusal()` leaves it: blocked,
    no retry clock, and a durable reason naming the envelope it was refused under."""
    _set_rls_user(db_session, FOUNDER_USER_ID)
    task = db_session.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalar_one()
    task.status = MainAITaskStatus.blocked
    task.blocker_reason = SUPERVISOR_REFUSAL_PARK_BLOCKER
    task.next_retry_at = None
    db_session.flush()
    db_session.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=FOUNDER_USER_ID,
            event_type=MainAITaskEventType.blocked,
            detail={
                "reason": SUPERVISOR_REFUSAL_PARK_REASON,
                "phase": "CONFIG_REFUSED",
                "execution_envelope_id": str(envelope_id),
            },
        )
    )
    db_session.commit()
    assert is_supervisor_refusal_park(db_session, task=task)
    return task


# ---------------------------------------------------------------- auth


def test_the_new_routes_require_authentication(client):
    goal_id = str(uuid.uuid4())
    assert client.post(f"/api/execution-envelopes/goals/{goal_id}/reauthorize", json={}).status_code in (401, 403)
    assert client.patch(f"/api/mainai/execution/goals/{goal_id}/approval-policy", json={"approval_policy": "autonomous_development_work"}).status_code in (401, 403)
    assert client.post(f"/api/mainai/execution/goals/{goal_id}/resume-parked-tasks").status_code in (401, 403)


# ---------------------------------------------------------------- re-authorize


def test_reauthorize_copies_the_current_scope_and_supersedes_it(client, db_session):
    goal = _goal(db_session)
    first_id = _authorized_envelope(db_session, goal)
    csrf = _login(client)

    res = client.post(
        f"/api/execution-envelopes/goals/{goal.id}/reauthorize",
        json={"reason": "configuration corrected"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 201, res.text
    new = res.json()
    assert new["id"] != str(first_id)
    assert new["authorized_paths"] == PATHS
    assert new["authorized_capabilities"] == CAPABILITIES
    assert new["authorized_risk"] == "low"
    assert new["supersedes_envelope_id"] == str(first_id)
    assert new["status"] == "active"

    _set_rls_user(db_session, FOUNDER_USER_ID)
    current = get_current_execution_envelope(db_session, owner_id=FOUNDER_USER_ID, goal_id=goal.id)
    assert current is not None and str(current.id) == new["id"]
    history = db_session.execute(
        select(MainAIGoal).where(MainAIGoal.id == goal.id)
    ).scalar_one()
    assert history is not None
    from app.execution_envelopes import list_execution_authorization_envelopes

    rows = list_execution_authorization_envelopes(db_session, owner_id=FOUNDER_USER_ID, goal_id=goal.id)
    assert [row.status for row in rows] == ["superseded", "active"]


def test_reauthorize_cannot_widen_the_scope_even_if_the_client_sends_more(client, db_session):
    goal = _goal(db_session)
    _authorized_envelope(db_session, goal)
    csrf = _login(client)
    res = client.post(
        f"/api/execution-envelopes/goals/{goal.id}/reauthorize",
        json={
            "authorized_paths": ["/etc/passwd"],
            "authorized_capabilities": ["remote_write", "deploy"],
            "authorized_risk": "high",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["authorized_paths"] == PATHS
    assert body["authorized_capabilities"] == CAPABILITIES
    assert body["authorized_risk"] == "low"


def test_reauthorize_is_idempotent_on_its_key(client, db_session):
    goal = _goal(db_session)
    _authorized_envelope(db_session, goal)
    csrf = _login(client)
    key = f"retry-{uuid.uuid4()}"
    first = client.post(
        f"/api/execution-envelopes/goals/{goal.id}/reauthorize",
        json={"idempotency_key": key},
        headers={"X-CSRF-Token": csrf},
    )
    second = client.post(
        f"/api/execution-envelopes/goals/{goal.id}/reauthorize",
        json={"idempotency_key": key},
        headers={"X-CSRF-Token": csrf},
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]


def test_reauthorize_without_a_current_envelope_is_a_clear_400(client, db_session):
    goal = _goal(db_session)
    csrf = _login(client)
    res = client.post(
        f"/api/execution-envelopes/goals/{goal.id}/reauthorize", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert res.status_code == 400
    assert "no current active execution authorization" in res.json()["detail"]


# ---------------------------------------------------------------- approval policy


def test_approval_policy_correction_is_audited_and_rejects_unknown_names(client, db_session):
    goal = _goal(db_session)
    csrf = _login(client)

    bad = client.patch(
        f"/api/mainai/execution/goals/{goal.id}/approval-policy",
        json={"approval_policy": "whatever_i_want"},
        headers={"X-CSRF-Token": csrf},
    )
    assert bad.status_code == 422

    ok = client.patch(
        f"/api/mainai/execution/goals/{goal.id}/approval-policy",
        json={"approval_policy": "autonomous_development_work", "reason": "refusal named this policy"},
        headers={"X-CSRF-Token": csrf},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["approval_policy"] == "autonomous_development_work"

    _set_rls_user(db_session, FOUNDER_USER_ID)
    db_session.expire_all()
    assert db_session.get(MainAIGoal, goal.id).approval_policy == "autonomous_development_work"
    audit = db_session.execute(
        select(AuditLog).where(
            AuditLog.action == "mainai_goal_approval_policy_changed",
            AuditLog.entity_id == str(goal.id),
        )
    ).scalars().all()
    assert len(audit) == 1
    assert "standard_repo_work" in audit[0].detail
    assert "autonomous_development_work" in audit[0].detail


# ---------------------------------------------------------------- resume


def test_resume_wakes_a_park_from_an_older_envelope_and_is_idempotent(client, db_session):
    goal = _goal(db_session)
    old_envelope = _authorized_envelope(db_session, goal)
    task = _parked_task(db_session, goal, old_envelope)
    csrf = _login(client)

    res = client.post(
        f"/api/execution-envelopes/goals/{goal.id}/reauthorize", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert res.status_code == 201, res.text

    first = client.post(
        f"/api/mainai/execution/goals/{goal.id}/resume-parked-tasks", headers={"X-CSRF-Token": csrf}
    )
    assert first.status_code == 200, first.text
    assert first.json()["woken_task_ids"] == [str(task.id)]

    _set_rls_user(db_session, FOUNDER_USER_ID)
    db_session.expire_all()
    refreshed = db_session.get(MainAITask, task.id)
    assert refreshed.status == MainAITaskStatus.ready
    assert refreshed.blocker_reason is None
    assert refreshed.next_retry_at is None

    second = client.post(
        f"/api/mainai/execution/goals/{goal.id}/resume-parked-tasks", headers={"X-CSRF-Token": csrf}
    )
    assert second.status_code == 200
    assert second.json()["woken_task_ids"] == []


def test_resume_does_not_wake_a_park_under_the_still_active_envelope(client, db_session):
    goal = _goal(db_session)
    envelope = _authorized_envelope(db_session, goal)
    task = _parked_task(db_session, goal, envelope)
    csrf = _login(client)
    res = client.post(
        f"/api/mainai/execution/goals/{goal.id}/resume-parked-tasks", headers={"X-CSRF-Token": csrf}
    )
    assert res.status_code == 200
    assert res.json()["woken_task_ids"] == []
    _set_rls_user(db_session, FOUNDER_USER_ID)
    db_session.expire_all()
    assert db_session.get(MainAITask, task.id).status == MainAITaskStatus.blocked


def test_resume_without_an_active_envelope_is_a_clear_409(client, db_session):
    goal = _goal(db_session)
    _parked_task(db_session, goal, uuid.uuid4())
    csrf = _login(client)
    res = client.post(
        f"/api/mainai/execution/goals/{goal.id}/resume-parked-tasks", headers={"X-CSRF-Token": csrf}
    )
    assert res.status_code == 409


def test_the_whole_founder_recovery_chain_needs_no_direct_database_write(client, db_session):
    """Correct the configuration, re-authorize the SAME scope, resume -- the exact sequence the
    isolated installation had to perform with SQL on 2026-11-27."""
    goal = _goal(db_session, approval_policy="standard_repo_work")
    envelope = _authorized_envelope(db_session, goal)
    task = _parked_task(db_session, goal, envelope)
    csrf = _login(client)

    assert client.patch(
        f"/api/mainai/execution/goals/{goal.id}/approval-policy",
        json={"approval_policy": "autonomous_development_work"},
        headers={"X-CSRF-Token": csrf},
    ).status_code == 200
    reauthorized = client.post(
        f"/api/execution-envelopes/goals/{goal.id}/reauthorize",
        json={"reason": "policy corrected, same scope"},
        headers={"X-CSRF-Token": csrf},
    )
    assert reauthorized.status_code == 201, reauthorized.text
    assert reauthorized.json()["authorized_paths"] == PATHS

    resumed = client.post(
        f"/api/mainai/execution/goals/{goal.id}/resume-parked-tasks", headers={"X-CSRF-Token": csrf}
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["woken_task_ids"] == [str(task.id)]

    _set_rls_user(db_session, FOUNDER_USER_ID)
    db_session.expire_all()
    assert db_session.get(MainAITask, task.id).status == MainAITaskStatus.ready
    assert db_session.get(MainAIGoal, goal.id).approval_policy == "autonomous_development_work"
