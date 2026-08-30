"""Stage J — personal intent learning (wrong terminology must not overwrite canonical truth)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, text as sa_text

from app.models.founder_intent import FounderIntentBinding, FounderIntentCorrection
from app.models.project_entities import ProjectEntity
from app.models.user import User
from app.personal_intent import AmbiguityClass, correct_intent_binding, resolve_with_learned_intent
from app.request_context import current_user_id as current_user_id_var


def _owner(db):
    user = User(email=f"intent-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def test_filler_phrases_and_repeated_learning(superuser_db):
    owner = _owner(superuser_db)
    first = resolve_with_learned_intent(
        superuser_db,
        owner_id=owner.id,
        raw_expression="få med det här med short founder answers",
        idempotency_key="j1",
        context={"source": "chat"},
    )
    superuser_db.commit()
    assert first.authority_claimed is False
    assert "short founder answers" in first.interpreted_intent.lower()
    assert first.binding_id is not None

    second = resolve_with_learned_intent(
        superuser_db,
        owner_id=owner.id,
        raw_expression="få med det här med short founder answers",
        idempotency_key="j2",
    )
    superuser_db.commit()
    assert second.auto_resolved is True
    assert second.binding_id == first.binding_id
    binding = superuser_db.get(FounderIntentBinding, first.binding_id)
    assert binding.hit_count >= 2


def test_wrong_terminology_correction_preserves_history_not_entity_title(superuser_db):
    owner = _owner(superuser_db)
    # Seed a canonical entity with correct title
    from app.models.document import ActiveTruthStatus, Document, DocumentSource
    from app.models.knowledge_claim import KnowledgeClaim
    from app.project_entities import promote_interpretation_proposal, record_interpretation_proposal

    document = Document(title="s", source=DocumentSource.upload, uploaded_by=owner.id, active_truth_status=ActiveTruthStatus.active)
    superuser_db.add(document)
    superuser_db.flush()
    claim = KnowledgeClaim(owner_id=owner.id, source_id=document.id, claim_text="Postgres sessions", extraction_version="v1")
    superuser_db.add(claim)
    superuser_db.flush()
    proposal = record_interpretation_proposal(
        superuser_db, owner_id=owner.id, source_claim_id=claim.id, proposed_entity_type="idea", idempotency_key="j-prop"
    )
    entity_pair = promote_interpretation_proposal(
        superuser_db,
        owner_id=owner.id,
        proposal_id=proposal.id,
        entity_type="idea",
        title="Postgres sessions",
        authority="founder",
        basis="manual",
        entity_idempotency_key="j-ent",
    )
    entity = entity_pair[1]
    superuser_db.flush()
    canonical_title = entity.title

    res = resolve_with_learned_intent(
        superuser_db,
        owner_id=owner.id,
        raw_expression="den andra grejen med mongo sessions",
        idempotency_key="j-wrong",
    )
    # Point binding at canonical entity, then correct wrong terminology
    binding = superuser_db.get(FounderIntentBinding, res.binding_id)
    binding.canonical_entity_id = entity.id
    superuser_db.flush()
    correct_intent_binding(
        superuser_db,
        owner_id=owner.id,
        binding_id=res.binding_id,
        corrected_intent="Postgres sessions",
        wrong_terminology="mongo sessions",
        reason="founder_correction_wrong_component_name",
        canonical_entity_id=entity.id,
    )
    superuser_db.commit()

    # Entity title unchanged
    refreshed = superuser_db.get(ProjectEntity, entity.id)
    assert refreshed.title == canonical_title
    corrections = list(
        superuser_db.execute(
            select(FounderIntentCorrection).where(FounderIntentCorrection.binding_id == res.binding_id)
        ).scalars().all()
    )
    assert len(corrections) == 1
    assert corrections[0].prior_intent != corrections[0].corrected_intent
    assert corrections[0].wrong_terminology == "mongo sessions"

    again = resolve_with_learned_intent(
        superuser_db,
        owner_id=owner.id,
        raw_expression="den andra grejen med mongo sessions",
        idempotency_key="j-wrong-2",
    )
    assert again.auto_resolved is True
    assert again.interpreted_intent == "Postgres sessions"
    assert again.correction_history


def test_consequential_ambiguity_must_surface(superuser_db):
    owner = _owner(superuser_db)
    res = resolve_with_learned_intent(
        superuser_db,
        owner_id=owner.id,
        raw_expression="deploy till production den grejen",
        idempotency_key="j-cons",
        persist=True,
    )
    superuser_db.commit()
    assert res.ambiguity == AmbiguityClass.CONSEQUENTIAL
    assert res.must_surface is True
    assert res.authority_claimed is False


def test_hon_ska_kunna_and_unfinished_reference(superuser_db):
    owner = _owner(superuser_db)
    a = resolve_with_learned_intent(
        superuser_db,
        owner_id=owner.id,
        raw_expression="hon ska kunna göra temporal recap",
        idempotency_key="j-hon",
    )
    b = resolve_with_learned_intent(
        superuser_db,
        owner_id=owner.id,
        raw_expression="gör samma där",
        idempotency_key="j-unfinished",
    )
    superuser_db.commit()
    assert "temporal recap" in a.interpreted_intent.lower()
    assert b.ambiguity in {AmbiguityClass.LOW_RISK, AmbiguityClass.CONSEQUENTIAL, AmbiguityClass.NONE}
    assert b.authority_claimed is False
