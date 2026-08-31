"""Prove founder_add auto-links to work by default (Stage C park only)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.concept_reconciliation import reconcile_and_promote_idea
from app.inspectable_memory import founder_add_memory_note
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.project_entities import record_interpretation_proposal
from app.request_context import current_user_id as current_user_id_var
from app.work_candidates import list_work_candidates


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def test_founder_add_default_auto_parks_candidate(superuser_db):
    user = User(email=f"autolink-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(user)
    superuser_db.flush()
    _set_rls(superuser_db, user.id)
    document = Document(
        title="src",
        source=DocumentSource.upload,
        uploaded_by=user.id,
        active_truth_status=ActiveTruthStatus.active,
    )
    superuser_db.add(document)
    superuser_db.flush()
    claim = KnowledgeClaim(
        owner_id=user.id, source_id=document.id, claim_text="Use Postgres", extraction_version="v1"
    )
    superuser_db.add(claim)
    superuser_db.flush()
    proposal = record_interpretation_proposal(
        superuser_db,
        owner_id=user.id,
        source_claim_id=claim.id,
        proposed_entity_type="idea",
        idempotency_key=f"prop-{uuid.uuid4()}",
    )
    superuser_db.flush()
    reconcile_and_promote_idea(
        superuser_db,
        owner_id=user.id,
        proposal_id=proposal.id,
        title="Use Postgres",
        entity_idempotency_key=f"entity-{uuid.uuid4()}",
    )
    superuser_db.flush()

    note, _ = founder_add_memory_note(
        superuser_db,
        owner_id=user.id,
        content="Use Postgres for sessions",
        note_type="decision",
        idempotency_key=f"auto-{uuid.uuid4()}",
        # default link_to_work=True
    )
    superuser_db.flush()
    parked = [
        c
        for c in list_work_candidates(superuser_db, owner_id=user.id)
        if c.idempotency_key == f"memory-work:{note.id}"
    ]
    assert len(parked) == 1
    assert parked[0].status == "unreviewed"
    assert parked[0].provenance.get("memory_note_id") == str(note.id)
