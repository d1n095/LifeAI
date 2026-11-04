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
        # Even an exact source_ref cannot override the structured mismatch.
        source_ref="wanted.capability",
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
        # Genuine subject linkage via capability_key -- NOT just "some passing test",
        # matching what real correct evidence must actually look like (see the
        # subject-relevance regression tests below for why a bare passed=True with no
        # subject field is never sufficient).
        payload={"passed": True, "capability_key": "test_execution.pytest_backend"},
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


def test_exact_structured_subject_can_verify(superuser_db):
    owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True, "subject": "test_execution.pytest_backend"},
        source_type="pytest",
        source_ref="opaque-run-reference",
        idempotency_key=f"ev-subject-{uuid.uuid4()}",
        deterministic=True,
    )
    support = evidence_supports_claim(
        superuser_db,
        owner_id=owner.id,
        subject_key="test_execution.pytest_backend",
        proposition="verified_available",
        evidence_id=evidence.id,
    )
    assert support.supports is True


def test_generic_proposition_without_subject_identity_rejected(superuser_db):
    owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True, "proposition": "verified_available"},
        source_type="pytest",
        source_ref="test_execution.pytest_backend",
        idempotency_key=f"ev-proposition-{uuid.uuid4()}",
        deterministic=True,
    )
    support = evidence_supports_claim(
        superuser_db,
        owner_id=owner.id,
        subject_key="test_execution.pytest_backend",
        proposition="verified_available",
        evidence_id=evidence.id,
    )
    assert support.supports is False
    assert "proposition_mismatch" in support.reasons


def test_evidence_from_wrong_owner_rejected(superuser_db):
    owner = _owner(superuser_db)
    other_owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, other_owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=other_owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True, "capability_key": "test_execution.pytest_backend"},
        source_type="pytest",
        source_ref="opaque-run-reference",
        idempotency_key=f"ev-owner-{uuid.uuid4()}",
        deterministic=True,
    )
    support = evidence_supports_claim(
        superuser_db,
        owner_id=owner.id,
        subject_key="test_execution.pytest_backend",
        proposition="verified_available",
        evidence_id=evidence.id,
    )
    assert support.supports is False
    assert support.reasons == ("evidence_not_found_or_wrong_owner",)


@pytest.mark.parametrize(
    "source_ref",
    [
        "test_execution.pytest_backend",
        "pytest_backend",
        "tests/backend/mainai/test_pytest_backend.py::test_runs",
        "not_pytest_backend",
        "pytest_backend_old",
        "other/test_pytest_backend_copy.py",
        "prefix_test_execution.pytest_backend",
        "test_execution.pytest_backend_suffix",
    ],
)
def test_source_ref_without_structured_subject_cannot_verify(superuser_db, source_ref):
    """Opaque provenance cannot establish capability identity, even on an exact string."""
    owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True},
        source_type="pytest",
        source_ref=source_ref,
        idempotency_key=f"ev-frag-{uuid.uuid4()}",
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


def test_old_success_new_failure_cannot_keep_verified(superuser_db):
    owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True, "capability_key": "cap.keep"},
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


# --- Subject-relevance regression tests (P0 evidence subject-relevance closeout) ---
#
# All of these previously passed a completely broken subject check: either a bare
# "test_run_result"+passed=True bypassed subject matching entirely whenever capability_key
# was absent from the payload (Bug A), or capability_key WAS present but matched via
# substring containment instead of exact equality (Bug B) -- "send_email" in "send_email_v2"
# and "file_operations" in "file_operations.delete" both incorrectly satisfied the old check.


def test_no_structured_subject_and_unrelated_source_ref_rejected(superuser_db):
    """Bug A: bare passed=True with no capability_key/subject in the payload, and a
    source_ref that names a COMPLETELY DIFFERENT capability, must not prove an unrelated
    subject -- this is the exact shape the original finding reproduced."""
    owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True},
        source_type="pytest",
        source_ref="tests/test_capX_completely_different.py",
        idempotency_key=f"ev-bugA-{uuid.uuid4()}",
        deterministic=True,
    )
    with pytest.raises(CapabilityEvidenceError):
        record_capability_observation(
            superuser_db,
            owner_id=owner.id,
            capability_key="capY_the_one_we_claim",
            domain="test",
            status="verified_available",
            authority="deterministic_source",
            success=True,
            verification_evidence_id=evidence.id,
        )


def test_substring_capability_key_collision_rejected(superuser_db):
    """Bug B: "send_email" must not be satisfied by evidence for "send_email_v2" merely
    because one string contains the other."""
    owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True, "capability_key": "send_email_v2"},
        source_type="pytest",
        source_ref="tests/test_send_email_v2.py",
        idempotency_key=f"ev-substr-{uuid.uuid4()}",
        deterministic=True,
    )
    with pytest.raises(CapabilityEvidenceError):
        record_capability_observation(
            superuser_db,
            owner_id=owner.id,
            capability_key="send_email",
            domain="test",
            status="verified_available",
            authority="deterministic_source",
            success=True,
            verification_evidence_id=evidence.id,
        )


def test_parent_child_capability_key_not_conflated(superuser_db):
    """A child capability ("file_operations.delete") must not be treated as proof for its
    parent ("file_operations") or vice versa, unless a hierarchy is explicitly modeled --
    it isn't, so both directions must reject."""
    owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True, "capability_key": "file_operations.delete"},
        source_type="pytest",
        source_ref="tests/test_file_delete.py",
        idempotency_key=f"ev-parentchild-{uuid.uuid4()}",
        deterministic=True,
    )
    with pytest.raises(CapabilityEvidenceError):
        record_capability_observation(
            superuser_db,
            owner_id=owner.id,
            capability_key="file_operations",
            domain="test",
            status="verified_available",
            authority="deterministic_source",
            success=True,
            verification_evidence_id=evidence.id,
        )


def test_same_domain_different_skill_rejected(superuser_db):
    """Two capabilities in the same general domain but genuinely distinct skills must not
    be conflated: evidence for read-permission must not prove write-permission."""
    owner = _owner(superuser_db)
    execution = _task_exec(superuser_db, owner.id)
    evidence = record_evidence(
        superuser_db,
        owner_id=owner.id,
        execution_id=execution.id,
        evidence_kind="test_run_result",
        payload={"passed": True, "capability_key": "permissions.read"},
        source_type="pytest",
        source_ref="tests/test_read_permission.py",
        idempotency_key=f"ev-domain-{uuid.uuid4()}",
        deterministic=True,
    )
    with pytest.raises(CapabilityEvidenceError):
        record_capability_observation(
            superuser_db,
            owner_id=owner.id,
            capability_key="permissions.write",
            domain="permissions",
            status="verified_available",
            authority="deterministic_source",
            success=True,
            verification_evidence_id=evidence.id,
        )
