"""Founder behavior requirements against real PostgreSQL and the production services.

These are not UI-rendering or mocked-API tests:

1. A founder decision with origin/authority is superseded by a newer decision, and a fresh
   database session (the durable state a restart sees) returns only the current decision.
2. A running agent with already-completed work is not cancelled, re-instructed, or given new
   work by reconciliation/idle-work inspection.
3. A founder correction becomes an active engineering lesson that changes the actual
   verification plan produced by planner.create_plan(), not just a helper return value.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from app.agent_coordination.adapters import AgentHealth, AgentObservation, AgentResult
from app.agent_coordination.dispatch import DispatchResult, apply_dispatch_result, dispatch_assignment
from app.agent_coordination.execution_control import (
    RECONCILE_PROCESS_ALIVE,
    mark_execution_running,
    reconcile_execution_state,
    start_execution_tracking,
)
from app.agent_coordination.routing import (
    OUTCOME_NO_FEASIBLE_ASSIGNMENT,
    idle_agents_with_next_assignment,
    next_feasible_assignment_for_agent,
)
from app.agent_coordination.service import (
    acquire_lease,
    create_work_assignment,
    register_agent,
)
from app.founder_memory import record_founder_memory
from app.mainai_execution import planner
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.lessons import record_lesson_from_founder_correction
from app.models.agent_coordination import (
    AgentDispatchExecution,
    AgentWorkAssignment,
    AgentWorkAssignmentEvent,
    AgentWorkAssignmentEventType,
    WorkAssignmentStatus,
)
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.models.lesson_plan_decisions import EngineeringLessonPlanDecision
from app.models.mainai_execution import MainAITask
from app.models.user import User
from app.project_entities import (
    get_project_entity,
    list_current_project_entities,
    mark_project_entity_superseded,
    promote_interpretation_proposal,
    record_interpretation_proposal,
)
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _owner(db) -> User:
    user = User(email=f"behavior-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    _set_rls_user(db, user.id)
    return user


def _claim(db, owner: User) -> KnowledgeClaim:
    document = Document(
        title="Founder behavior source",
        source=DocumentSource.upload,
        uploaded_by=owner.id,
        active_truth_status=ActiveTruthStatus.active,
    )
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(
        owner_id=owner.id,
        source_id=document.id,
        claim_text="Founder decision source claim.",
        extraction_version="behavior-v1",
    )
    db.add(claim)
    db.flush()
    return claim


def _goal_plan_task(db, owner_id, *, title: str, task_type: str = "repo_edit"):
    goal = planner.create_goal(
        db,
        owner_id=owner_id,
        title=title,
        original_instruction="Exercise the real founder behavior path.",
        created_by="founder",
        approval_policy="autonomous_development_work",
    )
    plan = planner.create_plan(
        db,
        goal=goal,
        rationale="founder behavior integration test",
        tasks=[planner.PlannedTaskSpec(description="Behavior task", task_type=task_type)],
        created_by="founder",
    )
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).one()
    return goal, plan, task


def _agent(db, key: str):
    return register_agent(
        db,
        agent_key=f"{key}-{uuid.uuid4().hex[:8]}",
        display_name=key,
        adapter_kind="cli",
        execution_mode="cli_interactive",
        supports_read=True,
        supports_write=True,
        concurrency_limit=3,
    )


def _assignment(db, *, owner_id, goal, task, agent, paths):
    return create_work_assignment(
        db,
        owner_id=owner_id,
        goal_id=goal.id,
        task_id=task.id,
        agent_id=agent.id,
        role="builder",
        read_write_mode="read_write",
        repository_identity="lifeai",
        allowed_paths=list(paths),
        requested_by="behavior-test",
    )


def _lease(db, *, assignment, agent, paths):
    return acquire_lease(
        db,
        assignment=assignment,
        agent_id=agent.id,
        branch=f"behavior/{uuid.uuid4().hex[:8]}",
        worktree_path=f"/tmp/behavior-{uuid.uuid4().hex[:8]}",
        allowed_paths=list(paths),
        mode="read_write",
    )


class _RunningAdapter:
    def __init__(self):
        self.started = []
        self.instructions = []
        self.cancelled = False

    async def health(self):
        return AgentHealth(healthy=True, detail="behavior test adapter")

    def capabilities(self):
        return ("repo_edit", "run_tests")

    async def start_assignment(self, assignment_id):
        self.started.append(assignment_id)

    async def send_instruction(self, assignment_id, instruction):
        self.instructions.append(instruction)

    async def observe(self, assignment_id):
        return AgentObservation(raw_status="running", summary="still working")

    async def cancel(self, assignment_id):
        self.cancelled = True

    async def resume(self, assignment_id):
        pass

    async def collect_result(self, assignment_id):
        return AgentResult(succeeded=True, summary="completed")


def test_decision_with_origin_and_supersession_survives_a_new_database_session(superuser_db):
    owner = _owner(superuser_db)
    claim = _claim(superuser_db, owner)
    superuser_db.commit()

    old_proposal = record_interpretation_proposal(
        superuser_db,
        owner_id=owner.id,
        source_claim_id=claim.id,
        proposed_entity_type="decision",
        idempotency_key=f"behavior-decision-old-{uuid.uuid4()}",
        classifier_strategy="behavior-test",
        classifier_confidence="certain",
    )
    superuser_db.commit()
    _, old_decision = promote_interpretation_proposal(
        superuser_db,
        owner_id=owner.id,
        proposal_id=old_proposal.id,
        entity_type="decision",
        title="Use PostgreSQL 15",
        summary="The original infrastructure decision.",
        authority="founder",
        basis="manual",
        decided_by="founder",
        decided_at=datetime.utcnow(),
        entity_idempotency_key=f"behavior-entity-old-{uuid.uuid4()}",
    )
    superuser_db.commit()

    new_proposal = record_interpretation_proposal(
        superuser_db,
        owner_id=owner.id,
        source_claim_id=claim.id,
        proposed_entity_type="decision",
        idempotency_key=f"behavior-decision-new-{uuid.uuid4()}",
        classifier_strategy="behavior-test",
        classifier_confidence="certain",
    )
    superuser_db.commit()
    _, new_decision = promote_interpretation_proposal(
        superuser_db,
        owner_id=owner.id,
        proposal_id=new_proposal.id,
        entity_type="decision",
        title="Use PostgreSQL 16",
        summary="The corrected, currently governing decision.",
        authority="founder",
        basis="manual",
        decided_by="founder",
        decided_at=datetime.utcnow(),
        supersedes_entity_id=old_decision.id,
        entity_idempotency_key=f"behavior-entity-new-{uuid.uuid4()}",
    )
    superuser_db.commit()
    mark_project_entity_superseded(
        superuser_db,
        owner_id=owner.id,
        entity_id=old_decision.id,
        superseded_by_entity_id=new_decision.id,
    )
    superuser_db.commit()

    # A fresh Session is the durable state a restarted process reads. No in-memory object
    # from the writes above is reused.
    RestartedSession = sessionmaker(bind=superuser_db.get_bind())
    restarted = RestartedSession()
    try:
        _set_rls_user(restarted, owner.id)
        current = list_current_project_entities(restarted, owner_id=owner.id, entity_type="decision")
        assert [entity.id for entity in current] == [new_decision.id]
        assert current[0].title == "Use PostgreSQL 16"
        assert current[0].authority == "founder"
        assert current[0].basis == "manual"
        assert current[0].decided_by == "founder"
        assert current[0].supersedes_entity_id == old_decision.id

        historical = get_project_entity(restarted, owner_id=owner.id, entity_id=old_decision.id)
        assert historical is not None
        assert historical.status == "superseded"
        assert historical.title == "Use PostgreSQL 15"
        assert historical.supersedes_entity_id is None
    finally:
        restarted.close()


@pytest.mark.asyncio
async def test_running_agent_and_completed_work_are_not_cancelled_or_redispatched(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()

    goal, _plan, task = _goal_plan_task(superuser_db, owner.id, title="Running behavior work")
    agent = _agent(superuser_db, "behavior-builder")
    running = _assignment(
        superuser_db,
        owner_id=owner.id,
        goal=goal,
        task=task,
        agent=agent,
        paths=["backend/app/behavior_running/**"],
    )
    _lease(superuser_db, assignment=running, agent=agent, paths=["backend/app/behavior_running/**"])
    grant_task_approval(superuser_db, task=task, approved_by="founder")
    superuser_db.commit()

    adapter = _RunningAdapter()
    dispatch = await dispatch_assignment(superuser_db, assignment=running, agent=agent, adapter=adapter)
    assert dispatch.outcome == "ASSIGNABLE"
    execution = start_execution_tracking(
        superuser_db,
        assignment=running,
        adapter_key="behavior-running",
        attempt_id=dispatch.attempt_id,
    )
    mark_execution_running(superuser_db, execution=execution)
    superuser_db.commit()

    completed_goal, _completed_plan, completed_task = _goal_plan_task(
        superuser_db, owner.id, title="Already completed behavior work"
    )
    completed = _assignment(
        superuser_db,
        owner_id=owner.id,
        goal=completed_goal,
        task=completed_task,
        agent=agent,
        paths=["docs/behavior_completed/**"],
    )
    _lease(superuser_db, assignment=completed, agent=agent, paths=["docs/behavior_completed/**"])
    grant_task_approval(superuser_db, task=completed_task, approved_by="founder")
    superuser_db.commit()

    completed_adapter = _RunningAdapter()
    completed_dispatch = await dispatch_assignment(
        superuser_db, assignment=completed, agent=agent, adapter=completed_adapter
    )
    assert completed_dispatch.outcome == "ASSIGNABLE"
    apply_dispatch_result(
        superuser_db,
        assignment=completed,
        agent=agent,
        result=DispatchResult(
            succeeded=True,
            adapter_key="behavior-completed",
            dispatch_attempt_id=completed_dispatch.attempt_id,
        ),
    )
    superuser_db.commit()
    assert completed.status == WorkAssignmentStatus.completed

    assignment_count = superuser_db.query(AgentWorkAssignment).filter_by(owner_id=owner.id).count()
    execution_count = superuser_db.query(AgentDispatchExecution).filter_by(owner_id=owner.id).count()

    decision = await reconcile_execution_state(
        superuser_db, execution=execution, assignment=running, adapter=adapter
    )
    superuser_db.commit()

    assert decision.outcome == RECONCILE_PROCESS_ALIVE
    assert adapter.cancelled is False
    assert adapter.instructions == []
    assert adapter.started == [running.id]
    superuser_db.refresh(running)
    superuser_db.refresh(completed)
    assert running.status == WorkAssignmentStatus.running
    assert completed.status == WorkAssignmentStatus.completed
    assert superuser_db.query(AgentWorkAssignment).filter_by(owner_id=owner.id).count() == assignment_count
    assert superuser_db.query(AgentDispatchExecution).filter_by(owner_id=owner.id).count() == execution_count

    events = (
        superuser_db.query(AgentWorkAssignmentEvent)
        .filter_by(assignment_id=running.id)
        .order_by(AgentWorkAssignmentEvent.created_at)
        .all()
    )
    assert sum(
        event.event_type == AgentWorkAssignmentEventType.status_changed
        and (event.detail or {}).get("to") == "running"
        for event in events
    ) == 1
    assert not any(
        event.event_type == AgentWorkAssignmentEventType.status_changed
        and (event.detail or {}).get("to") == "cancelled"
        for event in events
    )

    next_work = next_feasible_assignment_for_agent(superuser_db, agent_id=agent.id, owner_id=owner.id)
    assert next_work.outcome == OUTCOME_NO_FEASIBLE_ASSIGNMENT
    assert next_work.assignment_id is None

    idle = idle_agents_with_next_assignment(superuser_db, owner_id=owner.id, agent_ids=[agent.id])
    assert all(item.agent.id != agent.id for item in idle)


def test_corrected_decision_rule_changes_the_real_plan(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()

    task_type = "repo_edit"
    # Unique per run so the baseline cannot inherit a lesson left by an earlier run in the
    # shared test database; validate_targeted_tests_target() only requires a safe relative
    # path, not that the future regression file already exists.
    target = f"tests/backend/test_founder_behavior_rule_{uuid.uuid4().hex}.py::test_corrected_rule"

    baseline_goal, _baseline_plan, baseline_task = _goal_plan_task(
        superuser_db,
        owner.id,
        title="Before the correction",
        task_type=task_type,
    )
    baseline_targets = {
        step.get("target")
        for step in baseline_task.verification_plan
        if step.get("kind") == "targeted_tests"
    }
    assert target not in baseline_targets

    note = record_founder_memory(
        superuser_db,
        owner_id=owner.id,
        note_type="correction",
        content="We skipped the regression test for the corrected rule.",
        idempotency_key=f"behavior-correction-{uuid.uuid4()}",
        authority="founder",
        basis="manual",
    )
    superuser_db.commit()
    lesson = record_lesson_from_founder_correction(
        superuser_db,
        note=note,
        root_cause="The original rule was applied without a regression guard.",
        affected_component="behavior-test",
        general_rule="Always attach the approved correction's regression guard to the next plan.",
        applies_to=[task_type],
        created_by="founder",
        fix="Add the regression target to the verification plan.",
        regression_test=target,
    )
    superuser_db.commit()
    assert lesson.source_type == "founder_correction"
    assert lesson.source_ref == str(note.id)

    corrected_goal, _corrected_plan, corrected_task = _goal_plan_task(
        superuser_db,
        owner.id,
        title="After the approved correction",
        task_type=task_type,
    )
    corrected_targets = {
        step.get("target")
        for step in corrected_task.verification_plan
        if step.get("kind") == "targeted_tests"
    }
    assert target in corrected_targets

    decisions = (
        superuser_db.query(EngineeringLessonPlanDecision)
        .filter_by(task_id=corrected_task.id, lesson_id=lesson.id)
        .all()
    )
    assert len(decisions) == 1
    assert decisions[0].decision_type == "verification_step_added"
    assert decisions[0].affected_step == {"kind": "targeted_tests", "target": target}
