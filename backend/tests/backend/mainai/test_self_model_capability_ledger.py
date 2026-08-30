"""Stage E — evidence-backed self-model / capability ledger."""

from __future__ import annotations

import uuid

import pytest

from app.capability_reality import CapabilityRealityError, record_capability_observation
from app.intelligence_governance.service import record_evidence, record_execution
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
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


def _evidence(db, owner_id, *, key: str = "ev"):
    goal = MainAIGoal(owner_id=owner_id, title="problem", original_instruction="solve", created_by="test", completed_at=None)
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    db.add(plan)
    db.flush()
    task = MainAITask(
        owner_id=owner_id,
        goal_id=goal.id,
        plan_id=plan.id,
        task_type="repo_edit",
        description="t",
        status="pending",
        risk_level="low",
    )
    db.add(task)
    db.flush()
    execution = record_execution(
        db, owner_id=owner_id, task_id=task.id, idempotency_key=f"{key}-exec", provider="internal"
    )
    return record_evidence(
        db,
        owner_id=owner_id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True},
        source_type="pytest",
        source_ref=f"tests::{key}.py::test_y",
        idempotency_key=f"{key}-evidence",
        deterministic=True,
    )


def test_confidence_alone_does_not_prove_capability(superuser_db):
    owner = _owner(superuser_db)
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


def test_proven_requires_durable_evidence(superuser_db):
    owner = _owner(superuser_db)
    evidence = _evidence(superuser_db, owner.id, key="proven")
    record_proven_capability(
        superuser_db,
        owner_id=owner.id,
        capability_key="demo.proven",
        domain="test",
        verification_evidence_id=evidence.id,
        status_reason="integration_test_passed",
    )
    superuser_db.commit()
    snap = build_self_model(superuser_db, owner_id=owner.id)
    assert "demo.proven" in snap.proven
    assert "demo.proven" in snap.improved
    entry = next(e for e in snap.entries if e.capability_key == "demo.proven")
    assert entry.success_count >= 1
    assert entry.status == "verified_available"
    assert entry.last_proof_evidence_id == evidence.id
    assert entry.last_proof_at is not None


def test_proven_without_evidence_is_rejected(superuser_db):
    owner = _owner(superuser_db)
    with pytest.raises(TypeError):
        record_proven_capability(  # type: ignore[call-arg]
            superuser_db,
            owner_id=owner.id,
            capability_key="demo.no-ev",
            domain="test",
        )
    with pytest.raises(CapabilityRealityError):
        record_capability_observation(
            superuser_db,
            owner_id=owner.id,
            capability_key="demo.no-ev2",
            domain="test",
            status="verified_available",
            success=True,
        )
    superuser_db.commit()
    snap = build_self_model(superuser_db, owner_id=owner.id)
    assert "demo.no-ev" not in snap.proven
    assert "demo.no-ev2" not in snap.proven


def test_wrong_owner_evidence_is_rejected(superuser_db):
    owner = _owner(superuser_db)
    other = _owner(superuser_db)
    evidence = _evidence(superuser_db, other.id, key="wrong-owner")
    with pytest.raises(CapabilityRealityError):
        record_proven_capability(
            superuser_db,
            owner_id=owner.id,
            capability_key="demo.stolen",
            domain="test",
            verification_evidence_id=evidence.id,
        )


def test_repeated_failures_and_regression(superuser_db):
    owner = _owner(superuser_db)
    evidence = _evidence(superuser_db, owner.id, key="regress")
    record_proven_capability(
        superuser_db,
        owner_id=owner.id,
        capability_key="demo.regress",
        domain="test",
        verification_evidence_id=evidence.id,
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
