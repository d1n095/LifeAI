"""Row-Level Security, exercised directly at the database layer through the restricted
runtime role (mainai_app), for `execution_scope_proposals`/`execution_authorization_
envelopes` (migration 0057). Mirrors tests/security/test_rls_isolation_work_candidates.py's
own established pattern exactly -- applying the discipline docs/LIFE_COGNITION_FOUNDATION_
REVIEW_2026-08-18.md's Finding 3 established: every new owner-scoped table gets a real
behavioral proof, not just a structural one, from the moment it is added."""

from sqlalchemy import text

from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import create_goal
from app.models.execution_envelope import ExecutionAuthorizationEnvelope, ExecutionScopeProposal


def _set_rls_user(session, user_id) -> None:
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


def _goal_for(session, owner_id):
    return create_goal(session, owner_id=owner_id, title="rls test", original_instruction="rls test", created_by="test")


def test_user_never_reads_another_users_execution_scope_proposals(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    goal_a = _goal_for(db_session, user_a.id)
    db_session.commit()
    _set_rls_user(db_session, user_a.id)
    propose_execution_scope(db_session, owner_id=user_a.id, goal_id=goal_a.id, idempotency_key="rls-esp-a")
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    goal_b = _goal_for(db_session, user_b.id)
    db_session.commit()
    _set_rls_user(db_session, user_b.id)
    propose_execution_scope(db_session, owner_id=user_b.id, goal_id=goal_b.id, idempotency_key="rls-esp-b")
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(ExecutionScopeProposal).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(ExecutionScopeProposal).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_an_execution_scope_proposal_for_another_user(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    goal_a = _goal_for(db_session, user_a.id)
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    db_session.add(ExecutionScopeProposal(owner_id=user_b.id, goal_id=goal_a.id, idempotency_key="rls-esp-cross"))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by execution_scope_proposals_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()


def test_cannot_reference_another_owners_goal_from_an_execution_scope_proposal(db_session, make_verified_user):
    """Distinct from the cross-owner ROW test above: owner_id=A (matches session, passes WITH
    CHECK) but goal_id points at owner B's goal -- proves the composite owner-anchored FK is
    the real backstop, not just RLS on the child row's own owner_id."""

    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_b.id)
    goal_b = _goal_for(db_session, user_b.id)
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    db_session.add(ExecutionScopeProposal(owner_id=user_a.id, goal_id=goal_b.id, idempotency_key="rls-xref-esp"))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by the composite owner-anchored FK (migration 0057)"
    except Exception:
        db_session.rollback()


def test_user_never_reads_another_users_execution_authorization_envelopes(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    goal_a = _goal_for(db_session, user_a.id)
    db_session.commit()
    _set_rls_user(db_session, user_a.id)
    proposal_a = propose_execution_scope(db_session, owner_id=user_a.id, goal_id=goal_a.id, idempotency_key="rls-env-src-a")
    authorize_execution_scope(
        db_session, owner_id=user_a.id, proposal_id=proposal_a.id, authorized_by="founder",
        authorized_paths=[], authorized_capabilities=[], authorized_risk="low", envelope_idempotency_key="rls-env-a",
    )
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    goal_b = _goal_for(db_session, user_b.id)
    db_session.commit()
    _set_rls_user(db_session, user_b.id)
    proposal_b = propose_execution_scope(db_session, owner_id=user_b.id, goal_id=goal_b.id, idempotency_key="rls-env-src-b")
    authorize_execution_scope(
        db_session, owner_id=user_b.id, proposal_id=proposal_b.id, authorized_by="founder",
        authorized_paths=[], authorized_capabilities=[], authorized_risk="low", envelope_idempotency_key="rls-env-b",
    )
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(ExecutionAuthorizationEnvelope).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(ExecutionAuthorizationEnvelope).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_an_execution_authorization_envelope_for_another_user(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    goal_a = _goal_for(db_session, user_a.id)
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    db_session.add(ExecutionAuthorizationEnvelope(
        owner_id=user_b.id, goal_id=goal_a.id, authorized_risk="low", authorized_by="founder", idempotency_key="rls-env-cross",
    ))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by execution_authorization_envelopes_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()
