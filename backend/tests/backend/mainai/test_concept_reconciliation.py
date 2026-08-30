"""Stage B — differently-worded ideas collapse to one canonical concept / work candidate."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.concept_reconciliation import (
    ConceptReconciliationError,
    classify_against_corpus,
    normalize_concept_text,
    reconcile_and_promote_idea,
    relate_concepts,
)
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.project_entities import ProjectEntity, ProjectEntityAlias
from app.models.user import User
from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal
from app.project_entities.service import list_current_project_entities
from app.work_candidates import list_work_candidates


def _owner_claim_proposal(db, *, text: str, entity_type: str = "idea"):
    user = User(email=f"recon-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    document = Document(title="src", source=DocumentSource.upload, uploaded_by=user.id, active_truth_status=ActiveTruthStatus.active)
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(owner_id=user.id, source_id=document.id, claim_text=text, extraction_version="v1")
    db.add(claim)
    db.flush()
    proposal = record_interpretation_proposal(
        db,
        owner_id=user.id,
        source_claim_id=claim.id,
        proposed_entity_type=entity_type,
        idempotency_key=f"prop-{uuid.uuid4()}",
    )
    db.flush()
    return user, claim, proposal


def _second_proposal(db, *, owner, text: str, entity_type: str = "idea"):
    document = Document(title="src2", source=DocumentSource.upload, uploaded_by=owner.id, active_truth_status=ActiveTruthStatus.active)
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(owner_id=owner.id, source_id=document.id, claim_text=text, extraction_version="v1")
    db.add(claim)
    db.flush()
    proposal = record_interpretation_proposal(
        db,
        owner_id=owner.id,
        source_claim_id=claim.id,
        proposed_entity_type=entity_type,
        idempotency_key=f"prop-{uuid.uuid4()}",
    )
    db.flush()
    return claim, proposal


def test_normalize_collapses_punctuation_and_case():
    assert normalize_concept_text("Use  Postgres for MainAI memory storage!") == normalize_concept_text(
        "use postgres for mainai memory storage"
    )


def test_differently_worded_same_idea_resolves_to_one_canonical_entity(superuser_db):
    a = "Use Postgres for MainAI memory storage"
    b = "use  postgres  for mainai memory storage!"
    owner, _claim, proposal_a = _owner_claim_proposal(superuser_db, text=a)
    result_a = reconcile_and_promote_idea(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal_a.id,
        title=a,
        entity_idempotency_key="entity-a",
    )
    superuser_db.commit()
    assert result_a.created_entity is True

    _claim_b, proposal_b = _second_proposal(superuser_db, owner=owner, text=b)
    result_b = reconcile_and_promote_idea(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal_b.id,
        title=b,
        entity_idempotency_key="entity-b",
    )
    superuser_db.commit()

    assert result_b.created_entity is False
    assert result_b.outcome == "reused_same"
    assert result_b.canonical_entity_id == result_a.canonical_entity_id
    entities = list_current_project_entities(superuser_db, owner_id=owner.id, entity_type="idea")
    assert len(entities) == 1
    aliases = superuser_db.query(ProjectEntityAlias).filter_by(owner_id=owner.id).all() if hasattr(superuser_db, "query") else []
    from sqlalchemy import select

    aliases = list(
        superuser_db.execute(select(ProjectEntityAlias).where(ProjectEntityAlias.owner_id == owner.id)).scalars().all()
    )
    assert len(aliases) == 1
    assert aliases[0].entity_id == result_a.canonical_entity_id


def test_same_concept_does_not_spawn_duplicate_work_candidates(superuser_db):
    title_a = "Add durable inspectable memory foundation"
    title_b = "add durable inspectable memory foundation."
    owner, _, proposal_a = _owner_claim_proposal(superuser_db, text=title_a)
    result_a = reconcile_and_promote_idea(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal_a.id,
        title=title_a,
        entity_idempotency_key="wc-entity-a",
    )
    superuser_db.commit()
    assert result_a.created_work_candidate is True

    _, proposal_b = _second_proposal(superuser_db, owner=owner, text=title_b)
    result_b = reconcile_and_promote_idea(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal_b.id,
        title=title_b,
        entity_idempotency_key="wc-entity-b",
    )
    superuser_db.commit()
    assert result_b.created_work_candidate is False
    wcs = [c for c in list_work_candidates(superuser_db, owner_id=owner.id) if c.source_entity_id == result_a.canonical_entity_id]
    assert len(wcs) == 1


def test_partial_overlap_does_not_collapse_to_same(superuser_db):
    a = "Use Postgres for MainAI memory storage"
    b = "Use Postgres for billing ledger storage"
    owner, _, proposal_a = _owner_claim_proposal(superuser_db, text=a)
    reconcile_and_promote_idea(
        superuser_db, owner_id=owner.id, proposal_id=proposal_a.id, title=a, entity_idempotency_key="po-a"
    )
    superuser_db.commit()
    hits = classify_against_corpus(superuser_db, owner_id=owner.id, title=b, entity_type="idea")
    assert any(h.relationship_type == "partial_overlap" for h in hits)
    assert not any(h.relationship_type == "same" for h in hits)

    _, proposal_b = _second_proposal(superuser_db, owner=owner, text=b)
    result_b = reconcile_and_promote_idea(
        superuser_db, owner_id=owner.id, proposal_id=proposal_b.id, title=b, entity_idempotency_key="po-b"
    )
    superuser_db.commit()
    assert result_b.created_entity is True
    entities = list_current_project_entities(superuser_db, owner_id=owner.id, entity_type="idea")
    assert len(entities) == 2


def test_contradicts_never_collapses(superuser_db):
    a = "Prefer MongoDB for MainAI storage"
    b = "Prefer Postgres for MainAI storage"
    owner, _, proposal_a = _owner_claim_proposal(superuser_db, text=a)
    result_a = reconcile_and_promote_idea(
        superuser_db, owner_id=owner.id, proposal_id=proposal_a.id, title=a, entity_idempotency_key="c-a"
    )
    _, proposal_b = _second_proposal(superuser_db, owner=owner, text=b)
    result_b = reconcile_and_promote_idea(
        superuser_db, owner_id=owner.id, proposal_id=proposal_b.id, title=b, entity_idempotency_key="c-b"
    )
    superuser_db.commit()
    assert result_a.canonical_entity_id != result_b.canonical_entity_id
    edge = relate_concepts(
        superuser_db,
        owner_id=owner.id,
        from_entity_id=result_a.canonical_entity_id,
        to_entity_id=result_b.canonical_entity_id,
        relationship_type="contradicts",
    )
    superuser_db.commit()
    assert edge.relationship_type == "contradicts"
    with pytest.raises(ConceptReconciliationError):
        relate_concepts(
            superuser_db,
            owner_id=owner.id,
            from_entity_id=result_a.canonical_entity_id,
            to_entity_id=result_b.canonical_entity_id,
            relationship_type="same",
        )


def test_unique_fingerprint_enforced_at_db(superuser_db):
    title = "Canonical fingerprint idea"
    owner, claim, proposal = _owner_claim_proposal(superuser_db, text=title)
    promote_interpretation_proposal(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal.id,
        entity_type="idea",
        title=title,
        authority="founder",
        basis="manual",
        entity_idempotency_key="fp-1",
    )
    superuser_db.commit()
    # Direct insert attempting same fingerprint must fail.
    dup = ProjectEntity(
        owner_id=owner.id,
        entity_type="idea",
        title=title,
        title_normalized=normalize_concept_text(title),
        derived_from_claim_id=claim.id,
        idempotency_key="fp-2",
        authority="founder",
        basis="manual",
        status="proposed",
    )
    superuser_db.add(dup)
    with pytest.raises(IntegrityError):
        superuser_db.flush()
    superuser_db.rollback()


def test_promote_path_same_collapse_without_reconcile_helper(superuser_db):
    """Production promote_interpretation_proposal itself collapses SAME."""
    title_a = "Ship concept reconciliation stage B"
    title_b = "ship concept reconciliation stage b!!!"
    owner, _, proposal_a = _owner_claim_proposal(superuser_db, text=title_a)
    _, entity_a = promote_interpretation_proposal(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal_a.id,
        entity_type="idea",
        title=title_a,
        authority="founder",
        basis="manual",
        entity_idempotency_key="direct-a",
    )
    superuser_db.commit()
    _, proposal_b = _second_proposal(superuser_db, owner=owner, text=title_b)
    _, entity_b = promote_interpretation_proposal(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal_b.id,
        entity_type="idea",
        title=title_b,
        authority="founder",
        basis="manual",
        entity_idempotency_key="direct-b",
    )
    superuser_db.commit()
    assert entity_a.id == entity_b.id
    assert len(list_current_project_entities(superuser_db, owner_id=owner.id, entity_type="idea")) == 1
