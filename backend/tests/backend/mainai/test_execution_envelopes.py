"""Life Execution Authorization Envelope -- proves PROPOSED_SCOPE != AUTHORIZED_SCOPE
structurally: propose_execution_scope() never touches execution_authorization_envelopes; only
authorize_execution_scope() can, and only with the caller's own explicit authorized_by/
authorized_paths/authorized_capabilities/authorized_risk, never the proposal's own suggested
values silently copied in. See migration 0057's own module docstring and
docs/LIFE_EXECUTION_AUTHORIZATION_ENVELOPE.md for the full architecture."""

import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from app.execution_envelopes import (
    ExecutionEnvelopeError,
    authorize_execution_scope,
    get_current_execution_envelope,
    get_execution_authorization_envelope,
    get_execution_scope_proposal,
    list_execution_authorization_envelopes,
    list_execution_scope_proposals,
    list_unreviewed_execution_scope_proposals,
    propose_execution_scope,
    reject_execution_scope,
)
from app.mainai_execution.planner import create_goal
from app.models.user import User


def _owner_with_goal(db, title="Byt databas till Postgres."):
    user = User(email=f"env-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    goal = create_goal(db, owner_id=user.id, title=title, original_instruction=title, created_by="test")
    db.flush()
    return user, goal


def test_propose_execution_scope_never_writes_to_authorization_envelopes(superuser_db):
    owner, goal = _owner_with_goal(superuser_db)
    superuser_db.commit()

    proposal = propose_execution_scope(
        superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="prop-1",
        proposed_capabilities=["repo_read", "repo_edit"], proposed_risk="low",
    )
    superuser_db.commit()

    assert proposal.status == "unreviewed"
    assert proposal.authorized_envelope_id is None
    # No authority-bearing vocabulary exists on this row at all -- structural, not just
    # untested. (authorized_by/authorized_paths/authorized_capabilities/authorized_risk live
    # ONLY on ExecutionAuthorizationEnvelope.)
    assert not hasattr(proposal, "authorized_by")
    assert not hasattr(proposal, "authorized_paths")


def test_propose_execution_scope_defaults_to_empty_paths_never_a_guess(superuser_db):
    owner, goal = _owner_with_goal(superuser_db)
    superuser_db.commit()
    proposal = propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="prop-empty")
    superuser_db.commit()
    assert proposal.proposed_paths == []


def test_propose_execution_scope_is_idempotent_and_rejects_a_reused_key_with_different_fields(superuser_db):
    owner, goal = _owner_with_goal(superuser_db)
    superuser_db.commit()
    first = propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="idem-prop", proposed_risk="low")
    superuser_db.commit()
    replay = propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="idem-prop", proposed_risk="low")
    assert replay.id == first.id

    with pytest.raises(ExecutionEnvelopeError):
        propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="idem-prop", proposed_risk="high")


def test_proposed_risk_rejects_arbitrary_values(superuser_db):
    owner, goal = _owner_with_goal(superuser_db)
    superuser_db.commit()
    with pytest.raises(DBAPIError):
        propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="bad-risk", proposed_risk="catastrophic")
    superuser_db.rollback()


def test_authorizing_requires_the_callers_own_explicit_values_never_the_proposals_suggestion(superuser_db):
    """The core structural proof: a proposed_risk='low' proposal does NOT imply
    authorized_risk='low' -- the caller must assert it themselves, even if they end up
    choosing the same value."""

    owner, goal = _owner_with_goal(superuser_db)
    superuser_db.commit()
    proposal = propose_execution_scope(
        superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="auth-1",
        proposed_paths=["backend/app/foo.py"], proposed_capabilities=["repo_read", "repo_edit"], proposed_risk="low",
    )
    superuser_db.commit()

    authorized_proposal, envelope = authorize_execution_scope(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["backend/app/foo.py"], authorized_capabilities=["repo_read", "repo_edit"], authorized_risk="low",
        envelope_idempotency_key="auth-envelope-1",
    )
    superuser_db.commit()

    assert authorized_proposal.status == "authorized"
    assert authorized_proposal.authorized_envelope_id == envelope.id
    assert envelope.authorized_by == "founder"
    assert envelope.status == "active"
    fetched = get_execution_authorization_envelope(superuser_db, owner_id=owner.id, envelope_id=envelope.id)
    assert fetched.provenance["authorized_from_proposal_id"] == str(proposal.id)


def test_founder_can_narrow_the_proposed_scope(superuser_db):
    owner, goal = _owner_with_goal(superuser_db)
    superuser_db.commit()
    proposal = propose_execution_scope(
        superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="narrow-1",
        proposed_paths=["backend/app/foo.py", "backend/app/bar.py"], proposed_capabilities=["repo_read", "repo_edit", "run_tests"], proposed_risk="medium",
    )
    superuser_db.commit()

    _, envelope = authorize_execution_scope(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["backend/app/foo.py"], authorized_capabilities=["repo_read"], authorized_risk="low",
        envelope_idempotency_key="narrow-envelope-1",
    )
    superuser_db.commit()

    assert envelope.authorized_paths == ["backend/app/foo.py"]
    assert envelope.authorized_capabilities == ["repo_read"]
    assert envelope.authorized_risk == "low"


def test_founder_can_expand_beyond_the_proposed_scope(superuser_db):
    owner, goal = _owner_with_goal(superuser_db)
    superuser_db.commit()
    proposal = propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="expand-1", proposed_capabilities=["repo_read"])
    superuser_db.commit()

    _, envelope = authorize_execution_scope(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["backend/app/**"], authorized_capabilities=["repo_read", "repo_edit", "run_tests"], authorized_risk="medium",
        envelope_idempotency_key="expand-envelope-1",
    )
    superuser_db.commit()

    assert envelope.authorized_capabilities == ["repo_read", "repo_edit", "run_tests"]


def test_cannot_authorize_an_already_reviewed_proposal_twice(superuser_db):
    owner, goal = _owner_with_goal(superuser_db)
    superuser_db.commit()
    proposal = propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="twice-1")
    superuser_db.commit()
    authorize_execution_scope(
        superuser_db, owner_id=owner.id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=[], authorized_capabilities=[], authorized_risk="low", envelope_idempotency_key="twice-env-1",
    )
    superuser_db.commit()

    with pytest.raises(ExecutionEnvelopeError):
        authorize_execution_scope(
            superuser_db, owner_id=owner.id, proposal_id=proposal.id, authorized_by="founder",
            authorized_paths=[], authorized_capabilities=[], authorized_risk="low", envelope_idempotency_key="twice-env-2",
        )


def test_dismissing_rejects_a_proposal_never_deletes_it(superuser_db):
    owner, goal = _owner_with_goal(superuser_db)
    superuser_db.commit()
    proposal = propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="reject-1")
    superuser_db.commit()

    rejected = reject_execution_scope(superuser_db, owner_id=owner.id, proposal_id=proposal.id, reason="Founder doesn't want this goal to touch the repo at all.")
    superuser_db.commit()
    assert rejected.status == "rejected"
    assert "repo at all" in rejected.rejected_reason

    fetched = get_execution_scope_proposal(superuser_db, owner_id=owner.id, proposal_id=proposal.id)
    assert fetched is not None  # still durably queryable, not deleted


def test_reauthorizing_a_goal_supersedes_the_prior_envelope_never_mutates_it(superuser_db):
    owner, goal = _owner_with_goal(superuser_db)
    superuser_db.commit()
    proposal1 = propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="resup-1", proposed_capabilities=["repo_read"])
    superuser_db.commit()
    _, old_envelope = authorize_execution_scope(
        superuser_db, owner_id=owner.id, proposal_id=proposal1.id, authorized_by="founder",
        authorized_paths=[], authorized_capabilities=["repo_read"], authorized_risk="low", envelope_idempotency_key="resup-env-1",
    )
    superuser_db.commit()
    assert old_envelope.status == "active"

    proposal2 = propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="resup-2", proposed_capabilities=["repo_read", "repo_edit"])
    superuser_db.commit()
    _, new_envelope = authorize_execution_scope(
        superuser_db, owner_id=owner.id, proposal_id=proposal2.id, authorized_by="founder",
        authorized_paths=[], authorized_capabilities=["repo_read", "repo_edit"], authorized_risk="low", envelope_idempotency_key="resup-env-2",
    )
    superuser_db.commit()

    assert new_envelope.supersedes_envelope_id == old_envelope.id
    refetched_old = get_execution_authorization_envelope(superuser_db, owner_id=owner.id, envelope_id=old_envelope.id)
    assert refetched_old.status == "superseded"
    assert refetched_old.authorized_capabilities == ["repo_read"]  # content untouched, never rewritten

    current = get_current_execution_envelope(superuser_db, owner_id=owner.id, goal_id=goal.id)
    assert current.id == new_envelope.id

    history = list_execution_authorization_envelopes(superuser_db, owner_id=owner.id, goal_id=goal.id)
    assert [e.id for e in history] == [old_envelope.id, new_envelope.id]


def test_get_current_execution_envelope_is_none_when_never_authorized(superuser_db):
    owner, goal = _owner_with_goal(superuser_db)
    superuser_db.commit()
    propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="never-authorized")
    superuser_db.commit()

    assert get_current_execution_envelope(superuser_db, owner_id=owner.id, goal_id=goal.id) is None


def test_list_unreviewed_execution_scope_proposals_excludes_authorized_and_rejected(superuser_db):
    owner, goal = _owner_with_goal(superuser_db)
    superuser_db.commit()
    unreviewed = propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="list-1")
    authorized = propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="list-2")
    rejected = propose_execution_scope(superuser_db, owner_id=owner.id, goal_id=goal.id, idempotency_key="list-3")
    superuser_db.commit()
    authorize_execution_scope(
        superuser_db, owner_id=owner.id, proposal_id=authorized.id, authorized_by="founder",
        authorized_paths=[], authorized_capabilities=[], authorized_risk="low", envelope_idempotency_key="list-env-1",
    )
    reject_execution_scope(superuser_db, owner_id=owner.id, proposal_id=rejected.id, reason="noise")
    superuser_db.commit()

    unreviewed_ids = {p.id for p in list_unreviewed_execution_scope_proposals(superuser_db, owner_id=owner.id)}
    assert unreviewed_ids == {unreviewed.id}
    all_ids = {p.id for p in list_execution_scope_proposals(superuser_db, owner_id=owner.id)}
    assert all_ids == {unreviewed.id, authorized.id, rejected.id}


def test_authorizing_or_rejecting_a_proposal_belonging_to_another_owner_fails_closed(superuser_db):
    owner_a, goal_a = _owner_with_goal(superuser_db)
    owner_b, _ = _owner_with_goal(superuser_db)
    superuser_db.commit()
    proposal = propose_execution_scope(superuser_db, owner_id=owner_a.id, goal_id=goal_a.id, idempotency_key="cross-1")
    superuser_db.commit()

    with pytest.raises(ExecutionEnvelopeError):
        reject_execution_scope(superuser_db, owner_id=owner_b.id, proposal_id=proposal.id, reason="not mine")
    with pytest.raises(ExecutionEnvelopeError):
        authorize_execution_scope(
            superuser_db, owner_id=owner_b.id, proposal_id=proposal.id, authorized_by="founder",
            authorized_paths=[], authorized_capabilities=[], authorized_risk="low", envelope_idempotency_key="cross-env-1",
        )


def test_proposing_scope_for_another_owners_goal_fails_closed(superuser_db):
    owner_a, goal_a = _owner_with_goal(superuser_db)
    owner_b, _ = _owner_with_goal(superuser_db)
    superuser_db.commit()

    with pytest.raises(ExecutionEnvelopeError):
        propose_execution_scope(superuser_db, owner_id=owner_b.id, goal_id=goal_a.id, idempotency_key="xowner-goal")
