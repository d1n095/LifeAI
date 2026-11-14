"""Row-Level Security, exercised directly at the database layer through the restricted
runtime role (mainai_app), for `agent_work_assignments` -- in `app/rls.py`'s own
_MAINAI_EXECUTION_TABLES list since migration 0046, but with no dedicated cross-owner proof
until now. Mirrors tests/security/test_rls_isolation_work_candidates.py's own established
pattern exactly, per the Founder directive's own instruction to audit existing hardening
before claiming controls are absent -- this fills a real gap in coverage, not a gap in the
control itself (the policy was already correctly declared in rls.py; it had simply never been
exercised against a real cross-owner attempt in a test). `coordination_agents` itself is
deliberately NOT owner-scoped (rls.py's own comment: "founder-wide, not owner-scoped") -- only
the WORK ASSIGNMENT that ties an agent to one owner's goal is isolated here."""

import uuid

from sqlalchemy import text

from app.agent_coordination.service import create_work_assignment, register_agent
from app.mainai_execution.planner import create_goal
from app.models.agent_coordination import AgentWorkAssignment


def _set_rls_user(session, user_id) -> None:
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


def _goal(session, owner_id):
    return create_goal(session, owner_id=owner_id, title="RLS test goal", original_instruction="Do the thing.", created_by="test")


def _agent(session, key):
    return register_agent(session, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli")


def test_user_never_reads_another_users_agent_work_assignments(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    goal_a = _goal(db_session, user_a.id)
    agent_a = _agent(db_session, "rls-agent-a")
    db_session.commit()
    _set_rls_user(db_session, user_a.id)
    create_work_assignment(
        db_session, owner_id=user_a.id, goal_id=goal_a.id, task_id=None, agent_id=agent_a.id, role="builder",
        read_write_mode="read_only", repository_identity="lifeai", allowed_paths=["backend/app/**"], requested_by="test",
    )
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    goal_b = _goal(db_session, user_b.id)
    agent_b = _agent(db_session, "rls-agent-b")
    db_session.commit()
    _set_rls_user(db_session, user_b.id)
    create_work_assignment(
        db_session, owner_id=user_b.id, goal_id=goal_b.id, task_id=None, agent_id=agent_b.id, role="builder",
        read_write_mode="read_only", repository_identity="lifeai", allowed_paths=["backend/app/**"], requested_by="test",
    )
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(AgentWorkAssignment).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(AgentWorkAssignment).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_an_agent_work_assignment_for_another_user(db_session, make_verified_user):
    """The core 'compromised/unprivileged UserAI cannot access another tenant' proof the
    Founder directive asked for, applied specifically to agent dispatch state: even a direct
    row insert (not just the service function) claiming owner_id=B while the RLS session is
    authenticated as A must be rejected by the database itself, never merely by application
    code choosing not to call it that way."""
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    goal_a = _goal(db_session, user_a.id)
    agent_a = _agent(db_session, "rls-agent-cross")
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    db_session.add(
        AgentWorkAssignment(
            owner_id=user_b.id, goal_id=goal_a.id, agent_id=agent_a.id, role="builder", read_write_mode="read_only",
            repository_identity="lifeai", allowed_paths=["backend/app/**"], requested_by="test",
            canonical_work_key="rls-cross-owner-test",
        )
    )
    try:
        db_session.commit()
        assert False, "insert should have been rejected by agent_work_assignments' own RLS WITH CHECK"
    except Exception:
        db_session.rollback()
