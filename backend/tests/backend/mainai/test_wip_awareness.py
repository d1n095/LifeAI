"""app.mainai_executive.wip_awareness -- real workload queries against the LIVE
WorkforceAssignment (app.workforce) and MainAITask (app.mainai_execution) status columns.
MORE PARALLELISM != MORE PROGRESS: proves should_defer_new_work() reflects REAL current load,
not a guess, and never mutates anything itself."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from app.mainai_executive.wip_awareness import (
    current_wip_load,
    default_wip_limit,
    should_defer_new_work,
)
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask, MainAITaskStatus
from app.models.user import User
from app.workforce import TaskScopedAuthority, cancel_assignment, register_workforce_agent, resolve_delegation, submit_delegation_request


def _owner(db) -> User:
    user = User(email=f"wip-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _goal_plan(db, owner_id):
    goal = MainAIGoal(owner_id=owner_id, title="g", original_instruction="s", created_by="test")
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="r", created_by="test")
    db.add(plan)
    db.flush()
    return goal, plan


_TERMINAL_TASK_STATUSES = {MainAITaskStatus.completed, MainAITaskStatus.failed, MainAITaskStatus.cancelled}


def _task(db, owner_id, goal, plan, *, status: MainAITaskStatus):
    task = MainAITask(
        owner_id=owner_id, goal_id=goal.id, plan_id=plan.id, description="d",
        task_type="repo_edit", status=status,
        completed_at=datetime.utcnow() if status in _TERMINAL_TASK_STATUSES else None,
    )
    db.add(task)
    db.flush()
    return task


def _real_assignment(db, owner_id, *, capability: str | None = None):
    """A genuine WorkforceAssignment created through the real broker path
    (register_workforce_agent -> submit_delegation_request -> resolve_delegation), so its FK
    columns (delegation_request_id/profile_id) point at real rows -- never a hand-built row
    with random UUIDs, which would violate WorkforceAssignment's own composite FKs."""
    capability = capability or f"wip-test-{uuid.uuid4().hex[:8]}"
    register_workforce_agent(
        db, owner_id=owner_id, agent_key=f"wip-builder-{uuid.uuid4().hex[:8]}", name="WIP Test Builder",
        role="specialist", agent_type="LOCAL_MODEL", provider_type="local", trust_zone="LOCAL_INTERNAL",
        capability_tags=[capability], allowed_tool_classes=["read_excerpt"], cost_class="low", status="active",
    )
    verifier = register_workforce_agent(
        db, owner_id=owner_id, agent_key=f"wip-verifier-{uuid.uuid4().hex[:8]}", name="WIP Test Verifier",
        role="verifier", agent_type="VERIFIER", provider_type="local", trust_zone="LOCAL_INTERNAL",
        capability_tags=["verification"], allowed_tool_classes=["read_excerpt"], cost_class="low", status="active",
    )
    request = submit_delegation_request(
        db, owner_id=owner_id, goal_text="wip awareness test delegation", required_capability=capability,
        risk="low", data_sensitivity="internal", cost_ceiling_usd=0.0,
    )
    return resolve_delegation(
        db, owner_id=owner_id, request=request, context_items=[],
        authority=TaskScopedAuthority(
            allowed_read_paths=("notes/**",), allowed_write_paths=(), allowed_tool_classes=("read_excerpt",),
            allow_execution_effects=False, spend_ceiling_usd=0.0, expires_at=datetime.utcnow() + timedelta(hours=1),
        ),
        prefer_local_only=True,
        verifier_profile_id=verifier.id,
    )


def test_default_wip_limit_is_a_small_positive_constant():
    limit = default_wip_limit()
    assert isinstance(limit, int)
    assert 1 <= limit <= 20


def test_current_wip_load_counts_real_rows_by_status(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    goal, plan = _goal_plan(superuser_db, owner.id)
    _task(superuser_db, owner.id, goal, plan, status=MainAITaskStatus.running)
    _task(superuser_db, owner.id, goal, plan, status=MainAITaskStatus.ready)
    _task(superuser_db, owner.id, goal, plan, status=MainAITaskStatus.completed)
    _real_assignment(superuser_db, owner.id)
    superuser_db.commit()

    load = current_wip_load(superuser_db, owner_id=owner.id)
    assert load["tasks_in_flight"] == 2  # running + ready, not completed
    assert load["tasks_running"] == 1
    assert load["task_counts_by_status"]["completed"] == 1
    assert load["assignments_in_flight"] == 1  # the one real 'assigned' WorkforceAssignment
    assert load["assignment_counts_by_status"]["assigned"] == 1
    assert load["authority_impact"] == "NONE"


def test_current_wip_load_excludes_revoked_assignment(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    assignment = _real_assignment(superuser_db, owner.id)
    superuser_db.commit()
    load_before = current_wip_load(superuser_db, owner_id=owner.id)
    assert load_before["assignments_in_flight"] == 1

    cancel_assignment(superuser_db, owner_id=owner.id, assignment=assignment, reason="wip test cancel")
    superuser_db.commit()
    load_after = current_wip_load(superuser_db, owner_id=owner.id)
    assert load_after["assignments_in_flight"] == 0
    assert load_after["assignment_counts_by_status"]["revoked"] == 1


def test_current_wip_load_only_counts_this_owner(superuser_db):
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    superuser_db.commit()
    goal_a, plan_a = _goal_plan(superuser_db, owner_a.id)
    _task(superuser_db, owner_a.id, goal_a, plan_a, status=MainAITaskStatus.running)
    goal_b, plan_b = _goal_plan(superuser_db, owner_b.id)
    _task(superuser_db, owner_b.id, goal_b, plan_b, status=MainAITaskStatus.running)
    _task(superuser_db, owner_b.id, goal_b, plan_b, status=MainAITaskStatus.running)
    superuser_db.commit()

    load_a = current_wip_load(superuser_db, owner_id=owner_a.id)
    load_b = current_wip_load(superuser_db, owner_id=owner_b.id)
    assert load_a["tasks_in_flight"] == 1
    assert load_b["tasks_in_flight"] == 2


def test_should_defer_new_work_reflects_real_load_not_a_guess(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    goal, plan = _goal_plan(superuser_db, owner.id)
    for _ in range(3):
        _task(superuser_db, owner.id, goal, plan, status=MainAITaskStatus.running)
    superuser_db.commit()

    should_defer, reason = should_defer_new_work(superuser_db, owner_id=owner.id, wip_limit=3)
    assert should_defer is True
    assert "3" in reason

    should_defer, reason = should_defer_new_work(superuser_db, owner_id=owner.id, wip_limit=10)
    assert should_defer is False


def test_should_defer_new_work_uses_task_status_not_stale_assignment_status(superuser_db):
    """An assignment can still read 'assigned' while its task already completed in a race --
    the real dispatched-work signal (MainAITask.status) must win, not the assignment count."""
    owner = _owner(superuser_db)
    superuser_db.commit()
    goal, plan = _goal_plan(superuser_db, owner.id)
    _task(superuser_db, owner.id, goal, plan, status=MainAITaskStatus.completed)
    _real_assignment(superuser_db, owner.id)
    superuser_db.commit()

    should_defer, _reason = should_defer_new_work(superuser_db, owner_id=owner.id, wip_limit=1)
    assert should_defer is False  # zero tasks in flight, even though one assignment is 'assigned'


def test_should_defer_new_work_rejects_nonpositive_limit(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    with pytest.raises(ValueError):
        should_defer_new_work(superuser_db, owner_id=owner.id, wip_limit=0)


def test_should_defer_new_work_never_mutates(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    goal, plan = _goal_plan(superuser_db, owner.id)
    _task(superuser_db, owner.id, goal, plan, status=MainAITaskStatus.running)
    superuser_db.commit()
    should_defer_new_work(superuser_db, owner_id=owner.id)
    assert superuser_db.new == set() or len(superuser_db.new) == 0
    assert superuser_db.dirty == set() or len(superuser_db.dirty) == 0
