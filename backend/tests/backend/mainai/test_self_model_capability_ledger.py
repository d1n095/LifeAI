"""Stage E — evidence-backed self-model / capability ledger."""

from __future__ import annotations

import uuid

import pytest

from app.models.user import User
from app.self_model import (
    build_self_model,
    record_failed_capability,
    record_founder_intervention,
    record_proven_capability,
)


def _owner(db):
    user = User(email=f"self-model-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def test_confidence_alone_does_not_prove_capability(superuser_db):
    owner = _owner(superuser_db)
    from app.capability_reality import record_capability_observation

    record_capability_observation(
        superuser_db,
        owner_id=owner.id,
        capability_key="demo.unproven",
        domain="test",
        status="unknown",
        confidence=0.99,
        authority="ai_interpretation",
    )
    superuser_db.commit()
    snap = build_self_model(superuser_db, owner_id=owner.id)
    assert "demo.unproven" not in snap.proven
    entry = next(e for e in snap.entries if e.capability_key == "demo.unproven")
    assert entry.confidence_is_not_evidence is True
    assert entry.last_proof_at is None
    assert entry.weak is True


def test_proven_requires_real_success_observation(superuser_db):
    owner = _owner(superuser_db)
    record_proven_capability(
        superuser_db,
        owner_id=owner.id,
        capability_key="demo.proven",
        domain="test",
        status_reason="integration_test_passed",
    )
    superuser_db.commit()
    snap = build_self_model(superuser_db, owner_id=owner.id)
    assert "demo.proven" in snap.proven
    assert "demo.proven" in snap.improved
    entry = next(e for e in snap.entries if e.capability_key == "demo.proven")
    assert entry.success_count >= 1
    assert entry.status == "verified_available"
    assert entry.last_proof_at is not None


def test_repeated_failures_and_regression(superuser_db):
    owner = _owner(superuser_db)
    record_proven_capability(
        superuser_db,
        owner_id=owner.id,
        capability_key="demo.regress",
        domain="test",
    )
    record_failed_capability(
        superuser_db,
        owner_id=owner.id,
        capability_key="demo.regress",
        domain="test",
        reason="timeout_after_lease_expiry",
    )
    record_failed_capability(
        superuser_db,
        owner_id=owner.id,
        capability_key="demo.regress",
        domain="test",
        reason="timeout_after_lease_expiry",
    )
    superuser_db.commit()
    snap = build_self_model(superuser_db, owner_id=owner.id)
    assert "demo.regress" in snap.failed
    assert "demo.regress" in snap.repeatedly_failing
    assert "demo.regress" in snap.regressed
    entry = next(e for e in snap.entries if e.capability_key == "demo.regress")
    assert entry.failure_count >= 2
    assert any("timeout_after_lease_expiry" in p for p in entry.failure_patterns)
    assert entry.regression_history
    assert entry.next_improvement_candidate


def test_founder_intervention_is_counted(superuser_db):
    owner = _owner(superuser_db)
    record_founder_intervention(
        superuser_db,
        owner_id=owner.id,
        capability_key="demo.founder",
        domain="test",
        reason="manual_scope_narrowing",
    )
    superuser_db.commit()
    snap = build_self_model(superuser_db, owner_id=owner.id)
    entry = next(e for e in snap.entries if e.capability_key == "demo.founder")
    assert entry.founder_interventions >= 1
    assert "manual_scope_narrowing" in entry.corrections
