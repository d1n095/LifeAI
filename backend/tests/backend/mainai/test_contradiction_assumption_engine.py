"""Stage K — contradiction + assumption engine tests."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from app.contradiction_engine import invalidate_assumption, list_claims, record_structured_claim
from app.models.structured_claim import StructuredClaimEvent
from app.models.user import User


def _owner(db):
    user = User(email=f"claim-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def test_record_assumption_fact_and_supersede(superuser_db):
    owner = _owner(superuser_db)
    a = record_structured_claim(
        superuser_db,
        owner_id=owner.id,
        kind="ASSUMPTION",
        statement="Single-region deploy is enough for v1",
        confidence=0.6,
        source="founder_chat",
        dependent_refs=[{"kind": "mainai_goal", "id": str(uuid.uuid4())}],
        revalidation_trigger="multi_region_signal",
        idempotency_key="k-a1",
    )
    fact = record_structured_claim(
        superuser_db,
        owner_id=owner.id,
        kind="FACT",
        statement="Postgres is the durable store",
        confidence=0.95,
        source="decision_note",
        idempotency_key="k-f1",
    )
    superseded = record_structured_claim(
        superuser_db,
        owner_id=owner.id,
        kind="SUPERSEDED",
        statement="Multi-region is required for v1",
        confidence=0.8,
        source="founder_correction",
        supersedes_claim_id=a.id,
        idempotency_key="k-s1",
    )
    superuser_db.commit()
    assert fact.last_validated_at is not None
    assert a.status == "superseded"
    assert superseded.status == "active"
    assert list_claims(superuser_db, owner_id=owner.id, kind="ASSUMPTION", status="superseded")


def test_invalidate_finds_affected_and_preserves_events(superuser_db):
    owner = _owner(superuser_db)
    claim = record_structured_claim(
        superuser_db,
        owner_id=owner.id,
        kind="assumption",
        statement="Lease takeover never needed in soak",
        dependent_refs=[{"kind": "work_candidate", "id": str(uuid.uuid4())}],
        idempotency_key="k-inv",
    )
    superuser_db.commit()
    result = invalidate_assumption(
        superuser_db,
        owner_id=owner.id,
        claim_id=claim.id,
        evidence_note="soak run required takeover twice",
    )
    superuser_db.commit()
    assert result.new_status == "invalidated"
    assert any(r["kind"] == "work_candidate" for r in result.affected_work)
    events = list(
        superuser_db.execute(
            select(StructuredClaimEvent).where(StructuredClaimEvent.claim_id == claim.id)
        ).scalars().all()
    )
    types = {e.event_type for e in events}
    assert "created" in types and "invalidated" in types


def test_context_specific_claim(superuser_db):
    owner = _owner(superuser_db)
    row = record_structured_claim(
        superuser_db,
        owner_id=owner.id,
        kind="CONTEXT-SPECIFIC",
        statement="SSH egress only on disposable mirror",
        source="stage4_bound",
        idempotency_key="k-ctx",
    )
    superuser_db.commit()
    assert row.kind == "context_specific"
