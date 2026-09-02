"""Sibling concurrency: idempotent record_* recover without IntegrityError."""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.user import User
from app.project_entities import record_interpretation_proposal
from app.work_candidates.service import record_work_candidate


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _owner(db):
    u = User(email=f"sib-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _claim(db, owner_id):
    doc = Document(
        title="sib",
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=ActiveTruthStatus.active,
    )
    db.add(doc)
    db.flush()
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=doc.id, claim_text="sibling concurrency", extraction_version="v1"
    )
    db.add(claim)
    db.flush()
    return claim


def test_two_sessions_record_interpretation_proposal_converge(superuser_db):
    owner = _owner(superuser_db)
    claim = _claim(superuser_db, owner.id)
    key = f"prop-race-{uuid.uuid4().hex}"
    owner_id, claim_id = owner.id, claim.id
    superuser_db.commit()

    url = get_settings().database_url
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def worker():
        eng = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
        db = sessionmaker(bind=eng)()
        try:
            barrier.wait(timeout=10)
            row = record_interpretation_proposal(
                db,
                owner_id=owner_id,
                source_claim_id=claim_id,
                proposed_entity_type="idea",
                idempotency_key=key,
            )
            db.commit()
            results.append(row.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            db.rollback()
        finally:
            db.close()
            eng.dispose()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(30)
    t2.join(30)
    assert not errors, errors
    assert len(results) == 2
    assert results[0] == results[1]


def test_two_sessions_record_work_candidate_converge(superuser_db):
    from app.concept_reconciliation import reconcile_and_promote_idea

    owner = _owner(superuser_db)
    claim = _claim(superuser_db, owner.id)
    prop = record_interpretation_proposal(
        superuser_db,
        owner_id=owner.id,
        source_claim_id=claim.id,
        proposed_entity_type="idea",
        idempotency_key=f"p-{uuid.uuid4().hex}",
    )
    result = reconcile_and_promote_idea(
        superuser_db,
        owner_id=owner.id,
        proposal_id=prop.id,
        title="Sibling race concept",
        entity_idempotency_key=f"e-{uuid.uuid4().hex}",
    )
    entity_id = result.canonical_entity_id
    key = f"wc-race-{uuid.uuid4().hex}"
    owner_id = owner.id
    superuser_db.commit()

    url = get_settings().database_url
    barrier = threading.Barrier(2)
    results: list = []
    errors: list = []

    def worker():
        eng = create_engine(url, poolclass=NullPool, pool_pre_ping=True)
        db = sessionmaker(bind=eng)()
        try:
            barrier.wait(timeout=10)
            row = record_work_candidate(
                db,
                owner_id=owner_id,
                source_entity_id=entity_id,
                title="sibling wc",
                idempotency_key=key,
            )
            db.commit()
            results.append(row.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
            db.rollback()
        finally:
            db.close()
            eng.dispose()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    t2.start()
    t1.join(30)
    t2.join(30)
    assert not errors, errors
    assert len(results) == 2
    assert results[0] == results[1]
