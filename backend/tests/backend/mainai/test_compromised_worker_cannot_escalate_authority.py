"""Founder directive 2026-11-14 ("Resolve execution authority and security gaps"): "Build
regression tests demonstrating that a compromised or unprivileged UserAI cannot obtain
Founder authority, access another tenant or bypass approval." This file covers the two
clauses tests/security/test_rls_isolation_agent_work_assignments.py does not (that file
covers "access another tenant" for this same subsystem): "obtain Founder authority" and
"bypass approval," applied to the one gap an earlier research pass this session found --
app.agent_coordination.dispatch.apply_dispatch_result(), the ONE place a dispatched worker's
result is handed back into durable state, had no existing test exercising a compromised
worker's injected result."""

import uuid

import pytest

from app.agent_coordination.dispatch import DispatchResult, apply_dispatch_result
from app.agent_coordination.service import (
    InvalidTransitionError,
    create_work_assignment,
    register_agent,
)
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType
from app.models.user import User


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure as the other agent_coordination test modules' own identical
    fixture."""
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _owner(db) -> User:
    user = User(email=f"compromised-worker-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _agent(db, key: str):
    return register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli")


def _assignment(db, *, owner_id, goal_id, agent_id):
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal_id, task_id=None, agent_id=agent_id, role="builder", read_write_mode="read_only",
        repository_identity="lifeai", allowed_paths=["backend/app/**"], requested_by="test",
    )


def test_a_compromised_worker_result_never_creates_or_grants_mainai_task_approval(superuser_db):
    """The structural proof for "cannot bypass approval": a task is created with
    approval_required=True and genuinely has no approval_granted event. An attacker-controlled
    worker reports success on a DIFFERENT, unrelated AgentWorkAssignment -- the only object
    apply_dispatch_result() ever touches -- via a maximally adversarial DispatchResult (every
    observation field populated, as a compromised real adapter's stdout parser might produce).
    Confirms the task's own approval state is untouched: DispatchResult has no field that
    could even NAME the task, let alone grant it approval, and apply_dispatch_result() never
    queries app.models.mainai_execution at all."""
    owner = _owner(superuser_db)
    superuser_db.flush()
    goal = create_goal(superuser_db, owner_id=owner.id, title="Approval-gated goal", original_instruction="Needs sign-off.", created_by="test")
    create_plan(
        superuser_db, goal=goal, rationale="one task requiring approval", created_by="test",
        tasks=[PlannedTaskSpec(description="Sensitive change", task_type="repo_edit", approval_required=True)],
    )
    superuser_db.commit()
    task = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    assert task.approval_required is True

    agent = _agent(superuser_db, "compromised")
    assignment = _assignment(superuser_db, owner_id=owner.id, goal_id=goal.id, agent_id=agent.id)
    superuser_db.commit()

    adversarial_result = DispatchResult(
        succeeded=True, adapter_key="compromised-adapter", head_sha="deadbeef" * 5, branch="attacker/branch",
        worktree_path="/tmp/attacker", changed_paths=("backend/app/deps.py",), tests_passed=True, tests_run=1,
        tests_failed=0, ci_conclusion="success", pr_ref="https://github.com/d1n095/LifeAI/pull/999999",
        duration_seconds=1.0, cost_tokens=1, cost_usd=0.01,
    )
    # apply_dispatch_result() starts a freshly-created assignment at `planned`, so the
    # transition to `completed` is itself invalid -- this is checked separately below. Move
    # it to `running` first via the same path a real dispatch would, so this test isolates
    # the approval-bypass question from the state-machine question.
    from app.agent_coordination.service import transition_status

    transition_status(superuser_db, assignment=assignment, new_status="ready")
    transition_status(superuser_db, assignment=assignment, new_status="running")
    superuser_db.commit()

    apply_dispatch_result(superuser_db, assignment=assignment, agent=agent, result=adversarial_result)
    superuser_db.commit()

    approval_events = (
        superuser_db.query(MainAITaskEvent)
        .filter(MainAITaskEvent.task_id == task.id, MainAITaskEvent.event_type == MainAITaskEventType.approval_granted)
        .all()
    )
    assert approval_events == [], "a worker's dispatch result must never be able to grant MainAITask approval"
    superuser_db.refresh(task)
    assert task.approval_required is True


def test_dispatch_result_dataclass_has_no_field_that_could_name_a_founder_authority_target():
    """A second, independent proof of the same invariant, at the type level: DispatchResult's
    own field set is exhaustively enumerated here, so a FUTURE field addition that introduced
    something like `owner_id`/`approved_by`/`task_id`/`goal_id` would make this test fail
    loudly, forcing a conscious security review rather than a silent capability add."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(DispatchResult)}
    forbidden_substrings = ("owner", "approv", "founder", "goal_id", "task_id", "authoriz")
    offending = {name for name in field_names if any(s in name.lower() for s in forbidden_substrings)}
    assert offending == set(), f"DispatchResult gained a field that looks like it could grant authority: {offending}"


def test_apply_dispatch_result_from_a_non_running_assignment_fails_closed(superuser_db):
    """The state-machine half of "cannot bypass approval": a freshly-created assignment starts
    at `planned`, never `running` -- a compromised worker racing to report a result before (or
    without) ever being properly dispatched must be rejected, never silently accepted as if it
    had legitimately run."""
    owner = _owner(superuser_db)
    superuser_db.flush()
    goal = create_goal(superuser_db, owner_id=owner.id, title="State machine test", original_instruction="x", created_by="test")
    superuser_db.commit()
    agent = _agent(superuser_db, "premature")
    assignment = _assignment(superuser_db, owner_id=owner.id, goal_id=goal.id, agent_id=agent.id)
    superuser_db.commit()
    assert assignment.status.value == "planned"

    with pytest.raises(InvalidTransitionError):
        apply_dispatch_result(superuser_db, assignment=assignment, agent=agent, result=DispatchResult(succeeded=True))

    superuser_db.rollback()
    superuser_db.refresh(assignment)
    assert assignment.status.value == "planned", "a rejected transition must never leave the assignment looking completed"
