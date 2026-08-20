"""Proves migration 0051 actually closes the gap it was written for: `capability_record` is
now a real, working entry in `app.active_context.service`'s central object-reference registry
and in `memory_threads`' own `member_kind` vocabulary -- the SAME mechanism `founder_memory_
note` (migration 0049) and `diagnosis_record` (migration 0050) already use, not a new one.
See docs/LIFE_COGNITION_FOUNDATION_REVIEW_2026-08-18.md for why this was missing."""

import uuid

from app.active_context.service import SUPPORTED_TYPES, create_context_set
from app.capability_reality.service import record_capability_observation
from app.intelligence_governance import record_evidence, record_execution
from app.memory_threads.service import add_member, create_thread
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.user import User


def _owner(db):
    user = User(email=f"cap-link-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _task(db, owner_id):
    goal = MainAIGoal(owner_id=owner_id, title="cap link test", original_instruction="x", created_by="test")
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    db.add(plan)
    db.flush()
    task = MainAITask(owner_id=owner_id, goal_id=goal.id, plan_id=plan.id, description="t", task_type="repo_edit", status="pending")
    db.add(task)
    db.flush()
    return task


def test_capability_record_is_a_recognized_linkable_type_in_the_central_registry():
    assert "capability_record" in SUPPORTED_TYPES


def test_a_capability_gap_can_be_anchored_as_an_active_context_set(superuser_db):
    owner = _owner(superuser_db)
    capability = record_capability_observation(
        superuser_db, owner_id=owner.id, capability_key=f"agent_dispatch.{uuid.uuid4()}", domain="test",
        status="configured_unavailable", status_reason="missing provider key",
    )
    superuser_db.commit()

    context = create_context_set(
        superuser_db, owner_id=owner.id, anchor_type="capability_record", anchor_ref=str(capability.id), idempotency_key="cap-anchor-1",
    )
    superuser_db.commit()
    assert context.anchor_type == "capability_record"
    assert context.anchor_ref == str(capability.id)


def test_a_capability_record_can_be_linked_to_the_task_that_discovered_the_gap(superuser_db):
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    capability = record_capability_observation(
        superuser_db, owner_id=owner.id, capability_key=f"document_ingestion.{uuid.uuid4()}", domain="test", status="configured_unavailable",
    )
    superuser_db.commit()

    thread = create_thread(superuser_db, owner_id=owner.id, idempotency_key="cap-thread-1", manual_label="Missing capability discovered mid-task")
    add_member(superuser_db, owner_id=owner.id, thread_id=thread.id, member_kind="mainai_task", member_ref_id=task.id, membership_basis="deterministic_relationship")
    add_member(superuser_db, owner_id=owner.id, thread_id=thread.id, member_kind="capability_record", member_ref_id=capability.id, membership_basis="deterministic_relationship")
    superuser_db.commit()

    # Linked, but each remains its own row in its own table -- the task was never rewritten
    # to carry capability metadata, and the capability record was never rewritten to carry
    # task-specific detail.
    superuser_db.refresh(capability)
    assert capability.domain == "test"
    assert capability.status == "configured_unavailable"


def test_capability_record_edges_expose_its_verification_evidence(superuser_db):
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    execution = record_execution(superuser_db, owner_id=owner.id, task_id=task.id, idempotency_key="cap-edge-exec", provider="internal")
    evidence = record_evidence(
        superuser_db, owner_id=owner.id, execution_id=execution.id, evidence_kind="capability_probe", review_kind="deterministic_tool",
        deterministic=True, payload={"probed": True}, source_type="ci_log", source_ref="cap-probe", idempotency_key="cap-edge-ev",
    )
    capability = record_capability_observation(
        superuser_db, owner_id=owner.id, capability_key=f"probe.{uuid.uuid4()}", domain="test", status="verified_available",
        verification_evidence_id=evidence.id, success=True,
    )
    superuser_db.commit()

    from app.active_context.service import _edges, _Ref

    edges = _edges(superuser_db, owner.id, _Ref("capability_record", capability.id), capability)
    assert any(e.relation == "verified_by" and e.target.object_id == evidence.id for e in edges)
