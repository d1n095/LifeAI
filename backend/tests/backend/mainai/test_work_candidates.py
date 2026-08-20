"""Life Work Candidates -- proves DERIVED WORK CANDIDATE != AUTHORIZED WORK != EXECUTABLE WORK
structurally: record_work_candidate() never touches mainai_goals; only
authorize_work_candidate() can, and only with the caller's own explicit authorized_by, never
the candidate's own classifier confidence silently copied in. See migration 0055's own module
docstring and docs/LIFE_WORK_CANDIDATES.md for the full architecture."""

import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from app.mainai_execution.planner import get_goal
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal
from app.work_candidates import (
    WorkCandidateError,
    authorize_work_candidate,
    dismiss_work_candidate,
    get_work_candidate,
    list_unreviewed_work_candidates,
    list_work_candidates,
    record_work_candidate,
)


def _owner_with_entity(db, entity_type="vision_statement", title="Byt databas till Postgres."):
    # entity_type defaults to "vision_statement" -- deliberately NOT one of
    # app.project_entities.service._ACTIONABLE_ENTITY_TYPES -- so this fixture helper's own
    # promote_interpretation_proposal() call does not ALSO trigger the live work-candidate
    # wiring (proven separately in test_project_entity_work_candidate_capture.py) and pollute
    # these tests' own explicit record_work_candidate() call counts.
    user = User(email=f"wc-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    document = Document(title="Källa", source=DocumentSource.upload, uploaded_by=user.id, active_truth_status=ActiveTruthStatus.active)
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(owner_id=user.id, source_id=document.id, claim_text=title, extraction_version="v1")
    db.add(claim)
    db.flush()
    proposal = record_interpretation_proposal(db, owner_id=user.id, source_claim_id=claim.id, proposed_entity_type=entity_type, idempotency_key=f"wc-prop-{uuid.uuid4()}")
    db.flush()
    _, entity = promote_interpretation_proposal(
        db, owner_id=user.id, proposal_id=proposal.id, entity_type=entity_type, title=title,
        authority="founder", basis="manual", entity_idempotency_key=f"wc-entity-{uuid.uuid4()}",
    )
    return user, entity


def test_record_work_candidate_never_writes_to_mainai_goals(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()

    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="Migrate to Postgres",
        idempotency_key="wc-1", classifier_strategy="project_entity_promotion_v1", classifier_confidence=0.9,
    )
    superuser_db.commit()

    assert candidate.status == "unreviewed"
    assert candidate.authorized_goal_id is None


def test_record_work_candidate_is_idempotent_and_rejects_a_reused_key_with_different_fields(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    first = record_work_candidate(superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="x", idempotency_key="idem-wc")
    superuser_db.commit()
    replay = record_work_candidate(superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="x", idempotency_key="idem-wc")
    assert replay.id == first.id

    with pytest.raises(WorkCandidateError):
        record_work_candidate(superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="y", idempotency_key="idem-wc")


def test_priority_rejects_arbitrary_values(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    with pytest.raises(DBAPIError):
        record_work_candidate(superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="x", idempotency_key="bad-priority", priority="whenever")
    superuser_db.rollback()


def test_authorizing_a_candidate_requires_the_callers_own_explicit_authorized_by_never_the_classifiers_confidence(superuser_db):
    """The core structural proof: a classifier_confidence=0.95 candidate does NOT imply
    authorization -- the caller must assert authorized_by themselves, and the resulting
    MainAIGoal's created_by is exactly that assertion."""

    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="Migrate to Postgres",
        idempotency_key="auth-1", classifier_confidence=0.95,
    )
    superuser_db.commit()

    authorized_candidate, goal = authorize_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id, authorized_by="founder")
    superuser_db.commit()

    assert authorized_candidate.status == "authorized"
    assert authorized_candidate.authorized_goal_id == goal.id
    assert goal.created_by == "founder"
    fetched_goal = get_goal(superuser_db, goal.id)
    assert fetched_goal.id == goal.id


def test_cannot_authorize_an_already_reviewed_candidate_twice(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    candidate = record_work_candidate(superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="x", idempotency_key="twice-1")
    superuser_db.commit()
    authorize_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id, authorized_by="founder")
    superuser_db.commit()

    with pytest.raises(WorkCandidateError):
        authorize_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id, authorized_by="founder")


def test_dismissing_a_candidate_never_deletes_it_and_records_a_reason(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    candidate = record_work_candidate(superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="x", idempotency_key="dismiss-1")
    superuser_db.commit()

    dismissed = dismiss_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id, reason="Not actually actionable, just background context.")
    superuser_db.commit()
    assert dismissed.status == "dismissed"
    assert "background" in dismissed.dismissed_reason

    fetched = get_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id)
    assert fetched is not None


def test_list_unreviewed_work_candidates_excludes_authorized_and_dismissed(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    unreviewed = record_work_candidate(superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="a", idempotency_key="list-1")
    authorized = record_work_candidate(superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="b", idempotency_key="list-2")
    dismissed = record_work_candidate(superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="c", idempotency_key="list-3")
    superuser_db.commit()
    authorize_work_candidate(superuser_db, owner_id=owner.id, candidate_id=authorized.id, authorized_by="founder")
    dismiss_work_candidate(superuser_db, owner_id=owner.id, candidate_id=dismissed.id, reason="noise")
    superuser_db.commit()

    unreviewed_ids = {c.id for c in list_unreviewed_work_candidates(superuser_db, owner_id=owner.id)}
    assert unreviewed_ids == {unreviewed.id}
    all_ids = {c.id for c in list_work_candidates(superuser_db, owner_id=owner.id)}
    assert all_ids == {unreviewed.id, authorized.id, dismissed.id}


def test_authorizing_or_dismissing_a_candidate_belonging_to_another_owner_fails_closed(superuser_db):
    owner_a, entity_a = _owner_with_entity(superuser_db)
    owner_b, _ = _owner_with_entity(superuser_db)
    superuser_db.commit()
    candidate = record_work_candidate(superuser_db, owner_id=owner_a.id, source_entity_id=entity_a.id, title="x", idempotency_key="cross-1")
    superuser_db.commit()

    with pytest.raises(WorkCandidateError):
        dismiss_work_candidate(superuser_db, owner_id=owner_b.id, candidate_id=candidate.id, reason="not mine")
    with pytest.raises(WorkCandidateError):
        authorize_work_candidate(superuser_db, owner_id=owner_b.id, candidate_id=candidate.id, authorized_by="founder")
