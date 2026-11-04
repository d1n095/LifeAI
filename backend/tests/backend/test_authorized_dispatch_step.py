"""Authorized dispatch step — real WAITING_FOR_PROVIDER + fail-closed (needs independent review).

Real Postgres + RLS. NO injected/fake adapter and NO simulated external delivery: in this
environment no supported agent (claude-code/cursor-agent/codex) is founder-enabled or installed,
so the honest, verified outcome is WAITING_FOR_PROVIDER. The "enabled but incomplete config"
case uses REAL environment flags pointing at a REAL on-PATH binary and still fails closed to
`blocked` (REAL_PROVIDER_NOT_CONFIGURED) via the real NotConfiguredAdapter — proving a success
is never fabricated even when a provider is "enabled".
"""

from __future__ import annotations

import pytest
from sqlalchemy import text as sa_text

from app.agent_coordination.authorized_dispatch_step import (
    dispatch_or_wait,
    resume_waiting_for_provider,
)
from app.agent_coordination.dispatch import OUTCOME_ADAPTER_DISABLED, OUTCOME_REAL_PROVIDER_NOT_CONFIGURED
from app.agent_coordination.service import DuplicateWorkError, create_work_assignment, register_agent
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.agent_coordination import WorkAssignmentStatus
from app.models.mainai_execution import MainAITask
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls(session, owner_id):
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _goal_task(db, owner_id, *, files):
    goal = create_goal(
        db, owner_id=owner_id, title="dispatch", original_instruction="review open PR",
        created_by="founder", approval_policy="autonomous_development_work",
    )
    plan = create_plan(
        db, goal=goal, rationale="dispatch slice",
        tasks=[PlannedTaskSpec(description="review", task_type="read_only_audit", target_files=files)],
        created_by="founder",
    )
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).one()
    return goal, task


def _agent(db, key="claude-code"):
    return register_agent(
        db, agent_key=key, display_name=key, adapter_kind="cli",
        capabilities=["review"], supports_write=False,
    )


def _assignment(db, owner_id, *, paths, key="claude-code"):
    goal, task = _goal_task(db, owner_id, files=paths)
    agent = _agent(db, key)
    a = create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=task.id, agent_id=agent.id,
        role="reviewer", read_write_mode="read_only", repository_identity="lifeai",
        allowed_paths=paths, requested_by="founder",
    )
    db.commit()
    return a, agent, goal, task


def test_unavailable_provider_waits_durably_and_resumes(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls(db_session, user.id)
    a, agent, _g, _t = _assignment(db_session, user.id, paths=["docs/a/**"])

    res = dispatch_or_wait(db_session, assignment=a, agent=agent)
    db_session.commit()
    assert res.outcome == "waiting_for_provider"
    assert res.provider_enabled is False  # not founder-enabled
    assert res.missing_capability == OUTCOME_ADAPTER_DISABLED
    assert res.delivery_claimed is False and res.first_cycle_go is False

    db_session.refresh(a)
    assert a.status == WorkAssignmentStatus.blocked  # durably preserved as WAITING_FOR_PROVIDER
    # Resume once a provider becomes available → back to ready for a later tick.
    assert resume_waiting_for_provider(db_session, assignment=a) is True
    db_session.commit()
    db_session.refresh(a)
    assert a.status == WorkAssignmentStatus.ready


def test_unrelated_authorized_work_is_not_blocked_by_a_waiting_assignment(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls(db_session, user.id)
    a1, agent1, _g1, _t1 = _assignment(db_session, user.id, paths=["docs/x/**"], key="claude-code")
    a2, agent2, _g2, _t2 = _assignment(db_session, user.id, paths=["docs/y/**"], key="cursor-agent")

    r1 = dispatch_or_wait(db_session, assignment=a1, agent=agent1)
    r2 = dispatch_or_wait(db_session, assignment=a2, agent=agent2)
    db_session.commit()
    # Each independently reaches WAITING_FOR_PROVIDER; one does not block the other.
    assert r1.outcome == "waiting_for_provider"
    assert r2.outcome == "waiting_for_provider"
    db_session.refresh(a1)
    db_session.refresh(a2)
    assert a1.status == WorkAssignmentStatus.blocked
    assert a2.status == WorkAssignmentStatus.blocked


def test_duplicate_conflicting_assignment_is_prevented(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls(db_session, user.id)
    goal, task = _goal_task(db_session, user.id, files=["docs/dup/**"])
    agent = _agent(db_session)
    kw = dict(
        owner_id=user.id, goal_id=goal.id, task_id=task.id, agent_id=agent.id, role="reviewer",
        read_write_mode="read_only", repository_identity="lifeai", allowed_paths=["docs/dup/**"], requested_by="founder",
    )
    create_work_assignment(db_session, **kw)
    db_session.commit()
    with pytest.raises(DuplicateWorkError):
        create_work_assignment(db_session, **kw)  # same canonical work → rejected


def test_enabled_but_incomplete_config_never_fabricates_success(db_session, make_verified_user, monkeypatch):
    user, _ = make_verified_user()
    _set_rls(db_session, user.id)
    a, agent, _g, _t = _assignment(db_session, user.id, paths=["docs/z/**"])

    # REAL founder env config: enable claude-code and point it at a REAL on-PATH binary (echo),
    # but DO NOT supply an argv template. adapter_availability now reports enabled+found, so the
    # gate's adapter check passes — yet get_real_adapter returns NotConfiguredAdapter (no argv),
    # and dispatch fails closed to blocked. No injected adapter; no simulated delivery.
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_ENABLED__CLAUDE_CODE", "true")
    monkeypatch.setenv("LIFE_AGENT_ADAPTER_COMMAND__CLAUDE_CODE", "echo")

    res = dispatch_or_wait(db_session, assignment=a, agent=agent)
    db_session.commit()
    assert res.provider_enabled is True and res.executable_found is True  # genuinely "enabled"
    assert res.outcome == "gate_blocked"
    assert res.dispatch_decision == OUTCOME_REAL_PROVIDER_NOT_CONFIGURED  # fail-closed, not success
    assert res.delivery_claimed is False
    db_session.refresh(a)
    assert a.status == WorkAssignmentStatus.blocked
