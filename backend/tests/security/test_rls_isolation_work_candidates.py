"""Row-Level Security, exercised directly at the database layer through the restricted
runtime role (mainai_app), for `work_candidates` (migration 0055). Mirrors
tests/security/test_rls_isolation_project_entities.py's own established pattern exactly --
applying the discipline docs/LIFE_COGNITION_FOUNDATION_REVIEW_2026-08-18.md's Finding 3
established: every new owner-scoped table gets a real behavioral proof, not just a structural
one, from the moment it is added."""

from sqlalchemy import text

from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.work_candidate import WorkCandidate
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal
from app.work_candidates import record_work_candidate


def _set_rls_user(session, user_id) -> None:
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


def _entity_for(session, owner_id):
    # entity_type "vision_statement" -- deliberately NOT one of
    # app.project_entities.service._ACTIONABLE_ENTITY_TYPES -- so this helper's own
    # promotion does not ALSO trigger the live work-candidate wiring (proven separately in
    # test_project_entity_work_candidate_capture.py) and pollute this file's own RLS-focused
    # row counts.
    document = Document(title="Källa", source=DocumentSource.upload, uploaded_by=owner_id, active_truth_status=ActiveTruthStatus.active)
    session.add(document)
    session.flush()
    claim = KnowledgeClaim(owner_id=owner_id, source_id=document.id, claim_text="Vi vill bygga en bättre produkt.", extraction_version="v1")
    session.add(claim)
    session.flush()
    proposal = record_interpretation_proposal(session, owner_id=owner_id, source_claim_id=claim.id, proposed_entity_type="vision_statement", idempotency_key=f"rls-wc-prop-{claim.id}")
    session.flush()
    _, entity = promote_interpretation_proposal(
        session, owner_id=owner_id, proposal_id=proposal.id, entity_type="vision_statement", title="Bygg bättre produkt",
        authority="founder", basis="manual", entity_idempotency_key=f"rls-wc-entity-{claim.id}",
    )
    return entity


def test_user_never_reads_another_users_work_candidates(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    entity_a = _entity_for(db_session, user_a.id)
    db_session.commit()
    _set_rls_user(db_session, user_a.id)
    record_work_candidate(db_session, owner_id=user_a.id, source_entity_id=entity_a.id, title="x", idempotency_key="rls-wc-a")
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    entity_b = _entity_for(db_session, user_b.id)
    db_session.commit()
    _set_rls_user(db_session, user_b.id)
    record_work_candidate(db_session, owner_id=user_b.id, source_entity_id=entity_b.id, title="x", idempotency_key="rls-wc-b")
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(WorkCandidate).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(WorkCandidate).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_a_work_candidate_for_another_user(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    entity_a = _entity_for(db_session, user_a.id)
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    db_session.add(WorkCandidate(owner_id=user_b.id, source_entity_id=entity_a.id, title="x", idempotency_key="rls-wc-cross"))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by work_candidates_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()
