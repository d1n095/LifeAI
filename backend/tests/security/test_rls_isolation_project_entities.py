"""Row-Level Security, exercised directly at the database layer through the restricted
runtime role (mainai_app), for `interpretation_proposals`/`project_entities` (migration 0054).
Mirrors tests/security/test_rls_isolation_candidate_learning_signals.py's own established
pattern exactly -- applying the discipline docs/LIFE_COGNITION_FOUNDATION_REVIEW_2026-08-18.md's
Finding 3 established: every new owner-scoped table gets a real behavioral proof, not just a
structural one, from the moment it is added."""

from sqlalchemy import text

from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.project_entities import InterpretationProposal, ProjectEntity
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal


def _set_rls_user(session, user_id) -> None:
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


def _claim_for(session, owner_id):
    document = Document(title="Källa", source=DocumentSource.upload, uploaded_by=owner_id, active_truth_status=ActiveTruthStatus.active)
    session.add(document)
    session.flush()
    claim = KnowledgeClaim(owner_id=owner_id, source_id=document.id, claim_text="Vi bör byta databas.", extraction_version="v1")
    session.add(claim)
    session.flush()
    return claim


def test_user_never_reads_another_users_interpretation_proposals(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    claim_a = _claim_for(db_session, user_a.id)
    db_session.commit()
    _set_rls_user(db_session, user_a.id)
    record_interpretation_proposal(db_session, owner_id=user_a.id, source_claim_id=claim_a.id, proposed_entity_type="decision", idempotency_key="rls-ip-a")
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    claim_b = _claim_for(db_session, user_b.id)
    db_session.commit()
    _set_rls_user(db_session, user_b.id)
    record_interpretation_proposal(db_session, owner_id=user_b.id, source_claim_id=claim_b.id, proposed_entity_type="decision", idempotency_key="rls-ip-b")
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(InterpretationProposal).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(InterpretationProposal).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_an_interpretation_proposal_for_another_user(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    claim_a = _claim_for(db_session, user_a.id)
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    db_session.add(InterpretationProposal(owner_id=user_b.id, source_claim_id=claim_a.id, proposed_entity_type="decision", idempotency_key="rls-ip-cross"))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by interpretation_proposals_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()


def test_user_never_reads_another_users_project_entities(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    claim_a = _claim_for(db_session, user_a.id)
    db_session.commit()
    _set_rls_user(db_session, user_a.id)
    proposal_a = record_interpretation_proposal(db_session, owner_id=user_a.id, source_claim_id=claim_a.id, proposed_entity_type="decision", idempotency_key="rls-pe-src-a")
    promote_interpretation_proposal(
        db_session, owner_id=user_a.id, proposal_id=proposal_a.id, entity_type="decision", title="Byt databas",
        entity_idempotency_key="rls-pe-a", authority="founder", basis="manual",
    )
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    claim_b = _claim_for(db_session, user_b.id)
    db_session.commit()
    _set_rls_user(db_session, user_b.id)
    proposal_b = record_interpretation_proposal(db_session, owner_id=user_b.id, source_claim_id=claim_b.id, proposed_entity_type="decision", idempotency_key="rls-pe-src-b")
    promote_interpretation_proposal(
        db_session, owner_id=user_b.id, proposal_id=proposal_b.id, entity_type="decision", title="Byt databas B",
        entity_idempotency_key="rls-pe-b", authority="founder", basis="manual",
    )
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(ProjectEntity).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(ProjectEntity).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_a_project_entity_for_another_user(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    claim_a = _claim_for(db_session, user_a.id)
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    db_session.add(ProjectEntity(
        owner_id=user_b.id, entity_type="decision", title="x", derived_from_claim_id=claim_a.id, idempotency_key="rls-pe-cross",
    ))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by project_entities_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()


# --- adversarial: owner-anchored reference integrity (migration 0056) ----------------------
#
# Distinct from the cross-owner ROW tests above (which prove RLS rejects owner_id=B), these
# prove the database itself rejects owner_id=A rows that REFERENCE another owner's object by
# UUID -- a bare FK only proves the referenced row exists, RLS's WITH CHECK only validates the
# child row's own owner_id, neither alone proves the referenced UUID belongs to the SAME
# owner. Constructed directly against the restricted role, bypassing the service layer's own
# fail-closed checks, to prove the database is the real backstop.


def test_cannot_reference_another_owners_claim_from_an_interpretation_proposal(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_b.id)
    claim_b = _claim_for(db_session, user_b.id)
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    db_session.add(InterpretationProposal(owner_id=user_a.id, source_claim_id=claim_b.id, proposed_entity_type="decision", idempotency_key="rls-xref-ip"))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by the composite owner-anchored FK (migration 0056)"
    except Exception:
        db_session.rollback()


def test_cannot_reference_another_owners_claim_from_a_project_entity(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_b.id)
    claim_b = _claim_for(db_session, user_b.id)
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    db_session.add(ProjectEntity(owner_id=user_a.id, entity_type="decision", title="x", derived_from_claim_id=claim_b.id, idempotency_key="rls-xref-pe"))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by the composite owner-anchored FK (migration 0056)"
    except Exception:
        db_session.rollback()


def test_cannot_reference_another_owners_entity_from_a_relationship(db_session, make_verified_user):
    from app.models.project_entities import ProjectEntityRelationship

    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    claim_a = _claim_for(db_session, user_a.id)
    db_session.commit()
    _set_rls_user(db_session, user_a.id)
    proposal_a = record_interpretation_proposal(db_session, owner_id=user_a.id, source_claim_id=claim_a.id, proposed_entity_type="idea", idempotency_key="rls-xref-rel-a")
    _, entity_a = promote_interpretation_proposal(db_session, owner_id=user_a.id, proposal_id=proposal_a.id, entity_type="idea", title="a", authority="founder", basis="manual", entity_idempotency_key="rls-xref-rel-entity-a")
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    claim_b = _claim_for(db_session, user_b.id)
    db_session.commit()
    _set_rls_user(db_session, user_b.id)
    proposal_b = record_interpretation_proposal(db_session, owner_id=user_b.id, source_claim_id=claim_b.id, proposed_entity_type="idea", idempotency_key="rls-xref-rel-b")
    _, entity_b = promote_interpretation_proposal(db_session, owner_id=user_b.id, proposal_id=proposal_b.id, entity_type="idea", title="b", authority="founder", basis="manual", entity_idempotency_key="rls-xref-rel-entity-b")
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    db_session.add(ProjectEntityRelationship(owner_id=user_b.id, from_entity_id=entity_b.id, to_entity_id=entity_a.id, relationship_type="relates_to"))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by the composite owner-anchored FK (migration 0056)"
    except Exception:
        db_session.rollback()
