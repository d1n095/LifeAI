"""Evidence semantics — failed/unrelated evidence cannot prove verified_available."""

from __future__ import annotations

import uuid

import pytest

from app.capability_reality import record_capability_observation
from app.capability_reality.service import CapabilityEvidenceError
from app.evidence_claim import evidence_supports_claim
from app.intelligence_governance.service import record_evidence, record_execution
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.user import User


def _owner(db):
    u = User(email=f"ev-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _task_exec(db, owner_id):
    goal = MainAIGoal(
        owner_id=owner_id,
        title="problem",
        original_instruction="solve",
        created_by="test",
        completed_at=None,
    )
    db.add(goal)
    db.flush()
    plan = MainAIPlan(
        owner_id=owner_id, goal_id=goal.id, version=1, rationale="test", created_by="test"
    )
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
    return record_execution(
        db,
        owner_id=owner_id,
        task_id=task.id,
        idempotency_key=f"e-{uuid.uuid4()}",
        provider="internal",
    )


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def test_failed_evidence_rejected_as_proof(superuser_db):
    owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": False},
        source_type="pytest",
        source_ref="tests/x.py::test_y",
        idempotency_key=f"ev-fail-{uuid.uuid4()}",
        deterministic=True,
    )
    with pytest.raises(CapabilityEvidenceError):
        record_capability_observation(
            superuser_db,
            owner_id=owner.id,
            capability_key="test_execution.pytest_backend",
            domain="test_execution",
            status="verified_available",
            authority="deterministic_source",
            success=True,
            verification_evidence_id=evidence.id,
        )


def test_unrelated_evidence_rejected(superuser_db):
    owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True, "capability_key": "other.thing"},
        source_type="pytest",
        source_ref="unrelated",
        idempotency_key=f"ev-unrel-{uuid.uuid4()}",
        deterministic=True,
    )
    support = evidence_supports_claim(
        superuser_db,
        owner_id=owner.id,
        subject_key="wanted.capability",
        proposition="verified_available",
        evidence_id=evidence.id,
    )
    assert support.supports is False


def test_passed_evidence_can_verify(superuser_db):
    owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True},
        source_type="pytest",
        source_ref="tests/backend/mainai/test_x.py::test_y",
        idempotency_key=f"ev-ok-{uuid.uuid4()}",
        deterministic=True,
    )
    record = record_capability_observation(
        superuser_db,
        owner_id=owner.id,
        capability_key="test_execution.pytest_backend",
        domain="test_execution",
        status="verified_available",
        authority="deterministic_source",
        success=True,
        verification_evidence_id=evidence.id,
    )
    assert record.status == "verified_available"


def test_old_success_new_failure_cannot_keep_verified(superuser_db):
    owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True},
        source_type="pytest",
        source_ref="tests/ok.py",
        idempotency_key=f"ev-ok2-{uuid.uuid4()}",
        deterministic=True,
    )
    record_capability_observation(
        superuser_db,
        owner_id=owner.id,
        capability_key="cap.keep",
        domain="test",
        status="verified_available",
        authority="deterministic_source",
        success=True,
        verification_evidence_id=evidence.id,
    )
    with pytest.raises(CapabilityEvidenceError):
        record_capability_observation(
            superuser_db,
            owner_id=owner.id,
            capability_key="cap.keep",
            domain="test",
            status="verified_available",
            authority="deterministic_source",
            success=False,
            verification_evidence_id=evidence.id,
        )
