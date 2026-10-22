"""Integration: CO-4b/CO-5 persist on existing assignment rows, events, AuditLog, Founder snapshot."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.agent_coordination.durable import (
    DurableCoordinationService,
    founder_coordination_snapshot,
    persist_to_assignment,
)
from app.agent_coordination.service import create_work_assignment, register_agent
from app.audit import record_audit
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.mainai_founder_ops.snapshot import build_founder_ops_snapshot
from app.models.agent_coordination import AgentWorkAssignmentEvent, AgentWorkAssignmentEventType
from app.models.audit import AuditLog
from app.models.mainai_execution import MainAITask
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_task(db, owner_id):
    goal = create_goal(
        db,
        owner_id=owner_id,
        title="Durable coordination",
        original_instruction="Record a cross-agent obligation without inferring execution.",
        created_by="founder",
        approval_policy="autonomous_development_work",
    )
    plan = create_plan(
        db,
        goal=goal,
        rationale="coordination integration",
        tasks=[PlannedTaskSpec(description="Persist ledger", task_type="repo_edit", target_files=["docs/DURABLE_AGENT_COORDINATION.md"])],
        created_by="founder",
    )
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).one()
    return goal, task


def test_ledger_persists_on_assignment_and_reloads_into_founder_snapshot(db_session, owner_id):
    _set_rls_user(db_session, owner_id)
    goal, task = _goal_task(db_session, owner_id)
    agent = register_agent(
        db_session,
        agent_key="cursor-agent",
        display_name="Cursor",
        adapter_kind="cli",
        supports_write=True,
    )
    assignment = create_work_assignment(
        db_session,
        owner_id=owner_id,
        goal_id=goal.id,
        task_id=task.id,
        agent_id=agent.id,
        role="builder",
        read_write_mode="read_write",
        repository_identity="lifeai",
        allowed_paths=["docs/**"],
        requested_by="founder",
    )
    db_session.commit()

    service = DurableCoordinationService()
    created = service.assign(
        agent_identity="cursor",
        workstream="durable-handoff",
        owner_id=str(owner_id),
        instruction_id="int-1",
        backing_assignment_id=str(assignment.id),
        actor="founder",
    )
    service.acknowledge(assignment_id=created.assignment_id, agent_identity="cursor")
    persist_to_assignment(
        db_session,
        assignment=assignment,
        service=service,
        obligation_id=created.assignment_id,
        actor="mainai",
    )
    db_session.commit()

    events = (
        db_session.query(AgentWorkAssignmentEvent)
        .filter_by(assignment_id=assignment.id, event_type=AgentWorkAssignmentEventType.evidence_recorded)
        .all()
    )
    assert any((event.detail or {}).get("durable_coordination") for event in events)
    audits = db_session.query(AuditLog).filter_by(action="durable_coordination.persist").all()
    assert audits

    db_session.refresh(assignment)
    payload = (assignment.provenance or {}).get("durable_coordination")
    assert payload["obligation"]["status"] == "acknowledged"
    assert payload["obligation"]["backing_assignment_id"] == str(assignment.id)

    reloaded = founder_coordination_snapshot(db_session, owner_id=owner_id)
    assert reloaded["first_cycle_go"] is False
    assert reloaded["blocks_first_cycle"] is False
    assert reloaded["queue_empty"] is False
    assert reloaded["owners"][created.assignment_id] == "cursor"
    assert reloaded["active"][0]["status"] == "acknowledged"

    snap = build_founder_ops_snapshot(db_session, owner_id=owner_id, health={"status": "ok"})
    assert snap["first_cycle_go"] is False
    assert snap["durable_coordination"]["active"][0]["assignment_id"] == created.assignment_id
    assert snap["durable_coordination"]["blocks_first_cycle"] is False


def test_blocked_handoff_survives_reload(db_session, owner_id):
    _set_rls_user(db_session, owner_id)
    goal, task = _goal_task(db_session, owner_id)
    agent = register_agent(
        db_session,
        agent_key=f"cursor-{uuid.uuid4().hex[:8]}",
        display_name="Cursor",
        adapter_kind="cli",
    )
    assignment = create_work_assignment(
        db_session,
        owner_id=owner_id,
        goal_id=goal.id,
        task_id=task.id,
        agent_id=agent.id,
        role="builder",
        read_write_mode="read_write",
        repository_identity="lifeai",
        allowed_paths=["docs/**"],
        requested_by="founder",
    )
    service = DurableCoordinationService()
    created = service.assign(
        agent_identity="cursor",
        workstream="blocked-transfer",
        owner_id=str(owner_id),
        instruction_id="int-block",
        backing_assignment_id=str(assignment.id),
        actor="founder",
    )
    payload = b"secret-artifact"
    from app.agent_coordination.durable import artifact_sha256

    registered = service.register_artifact(
        producer_identity="cursor",
        consumer_identity="claude",
        sha256=artifact_sha256(payload),
        byte_length=len(payload),
        assignment_id=created.assignment_id,
        payload=payload,
    )
    blocked = service.transfer_artifact(
        handoff_id=registered.detail["handoff_id"],
        actor="cursor",
        transport="github_publication",
    )
    assert blocked.outcome == "blocked"
    persist_to_assignment(
        db_session,
        assignment=assignment,
        service=service,
        obligation_id=created.assignment_id,
    )
    db_session.commit()
    view = founder_coordination_snapshot(db_session, owner_id=owner_id)
    assert view["blocked"][0]["missing_capability"] == "codex_github_publication_authority"
    assert view["handoffs"][0]["status"] == "blocked"
    record_audit(
        db_session,
        user_id=owner_id,
        action="durable_coordination.blocked_transfer",
        entity_type="artifact_handoff",
        entity_id=registered.detail["handoff_id"],
        detail="missing=codex_github_publication_authority",
        commit=False,
    )
    db_session.commit()
