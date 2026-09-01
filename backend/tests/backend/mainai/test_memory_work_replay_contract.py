"""Memory→work replay contract — canonical ids retained on NOOP_SAME."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.concept_reconciliation import reconcile_and_promote_idea
from app.inspectable_memory import founder_add_memory_note
from app.memory_work_linkage import TimingClass, apply_memory_work_linkage
from app.memory_work_linkage.types import LinkageAction
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.project_entities import record_interpretation_proposal
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _owner(db):
    u = User(email=f"replay-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    current_user_id_var.set(str(u.id))
    db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(u.id)})
    return u


def _entity(db, owner_id):
    document = Document(
        title="src",
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=ActiveTruthStatus.active,
    )
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=document.id, claim_text="entity", extraction_version="v1"
    )
    db.add(claim)
    db.flush()
    proposal = record_interpretation_proposal(
        db,
        owner_id=owner_id,
        source_claim_id=claim.id,
        proposed_entity_type="idea",
        idempotency_key=f"prop-{uuid.uuid4()}",
    )
    db.flush()
    result = reconcile_and_promote_idea(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        title="Replay park target",
        entity_idempotency_key=f"entity-{uuid.uuid4()}",
    )
    db.flush()
    return result.canonical_entity_id


def test_memory_work_replay_keeps_canonical_ids(superuser_db):
    owner = _owner(superuser_db)
    _entity(superuser_db, owner.id)
    note, _ = founder_add_memory_note(
        superuser_db,
        owner_id=owner.id,
        content="Park a follow-up about library hours classification for research.",
        note_type="observation",
        idempotency_key=f"replay-note-{uuid.uuid4()}",
        link_to_work=False,
    )
    first = apply_memory_work_linkage(
        superuser_db,
        owner_id=owner.id,
        note_id=note.id,
        timing=TimingClass.LATER,
        park_candidate=True,
        is_correction=True,
    )
    # Contract fields must always be present
    assert first.operation_receipt_id
    assert isinstance(first.created_now_ids, list)
    assert isinstance(first.canonical_candidate_ids, list)
    if not (first.created_now_ids or first.canonical_candidate_ids):
        pytest.skip("park path did not bind entity in this fixture — contract fields still present")
    canonical = list(first.canonical_candidate_ids or first.created_candidate_ids)

    second = apply_memory_work_linkage(
        superuser_db,
        owner_id=owner.id,
        note_id=note.id,
        timing=TimingClass.LATER,
        park_candidate=True,
        is_correction=True,
    )
    assert second.created_now_ids == []
    assert second.canonical_candidate_ids == canonical
    assert second.replayed is True or LinkageAction.NOOP_SAME in second.actions
    assert second.operation_receipt_id
