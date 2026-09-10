"""MainAI Resource Intelligence Part 1 -- `app.resource_intelligence.telemetry` -- proves the
new `agent_resource_telemetry_samples` table (migration 0071) records real per-attempt
observations, the ownership check `record_telemetry_sample()` performs before inserting, the
`MetricEnvelope` "UNKNOWN stays UNKNOWN" contract for `context_utilization()`/
`estimated_time_to_context_limit()`, and the DERIVED (never stored)
`idle_productive_blocked_time()` classifier against real `AgentDispatchExecution`/
`AgentWorkAssignmentEvent` history -- against a real Postgres database, never mocked.

See docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md for the architecture this
package implements."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.agent_coordination.execution_control import start_execution_tracking
from app.agent_coordination.service import create_work_assignment, register_agent, transition_status
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.agent_coordination import AgentWorkAssignmentEvent
from app.models.mainai_execution import MainAITask
from app.resource_intelligence.telemetry import (
    context_utilization,
    estimated_time_to_context_limit,
    idle_productive_blocked_time,
    list_telemetry_samples,
    record_telemetry_sample,
)
from app.resource_intelligence.types import ResourceIntelligenceError


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_plan_task(db, owner_id, *, instruction="Resource intelligence telemetry test."):
    goal = create_goal(db, owner_id=owner_id, title="RI telemetry test", original_instruction=instruction, created_by="founder", approval_policy="standard_repo_work")
    plan = create_plan(db, goal=goal, rationale="ri telemetry test", tasks=[PlannedTaskSpec(description="Task 0", task_type="repo_edit")], created_by="founder")
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).first()
    return goal, task


def _agent(db, key="agent"):
    return register_agent(
        db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli",
        execution_mode="cli_interactive", supports_read=True, supports_write=True, concurrency_limit=3,
    )


def _assignment(db, *, owner_id, goal, task, agent):
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=task.id, agent_id=agent.id,
        role="builder", read_write_mode="read_write", repository_identity="lifeai",
        allowed_paths=["backend/app/resource_intelligence/**"], requested_by="test",
    )


# ============================================================================ record_telemetry_sample


def test_record_telemetry_sample_stores_only_what_the_caller_reports(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    attempt_id = uuid.uuid4()

    sample = record_telemetry_sample(
        superuser_db, owner_id=owner_id, assignment_id=assignment.id, attempt_id=attempt_id,
        context_used_tokens=1000, context_window_tokens=200000,
    )
    superuser_db.commit()

    assert sample.context_used_tokens == 1000
    assert sample.context_window_tokens == 200000
    # Every field the caller did not report stays None -- never fabricated/defaulted to 0.
    assert sample.input_tokens is None
    assert sample.output_tokens is None
    assert sample.cached_tokens is None
    assert sample.tool_calls is None
    assert sample.provenance == {}

    samples = list_telemetry_samples(superuser_db, owner_id=owner_id, attempt_id=attempt_id)
    assert len(samples) == 1
    assert samples[0].id == sample.id


def test_record_telemetry_sample_rejects_assignment_belonging_to_a_different_owner(superuser_db, owner_id, make_verified_user):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    superuser_db.commit()

    other_user, _pw = make_verified_user()
    with pytest.raises(ResourceIntelligenceError):
        record_telemetry_sample(
            superuser_db, owner_id=other_user.id, assignment_id=assignment.id, attempt_id=uuid.uuid4(),
            context_used_tokens=1, context_window_tokens=2,
        )


def test_record_telemetry_sample_rejects_nonexistent_assignment(superuser_db, owner_id):
    with pytest.raises(ResourceIntelligenceError):
        record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=uuid.uuid4(), attempt_id=uuid.uuid4())


# ============================================================================ context_utilization


def test_context_utilization_missing_data_with_zero_samples(superuser_db, owner_id):
    envelope = context_utilization(superuser_db, owner_id=owner_id, attempt_id=uuid.uuid4())
    assert envelope.missing_data is True
    assert envelope.value is None
    assert envelope.unit == "percent"
    assert envelope.source
    assert envelope.definition
    assert envelope.method


def test_context_utilization_missing_data_when_window_unknown(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    attempt_id = uuid.uuid4()
    record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=assignment.id, attempt_id=attempt_id, context_used_tokens=500)
    superuser_db.commit()

    envelope = context_utilization(superuser_db, owner_id=owner_id, attempt_id=attempt_id)
    assert envelope.missing_data is True
    assert envelope.value is None


def test_context_utilization_computes_real_percent_from_latest_sample(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    attempt_id = uuid.uuid4()
    record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=assignment.id, attempt_id=attempt_id, context_used_tokens=10000, context_window_tokens=200000)
    record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=assignment.id, attempt_id=attempt_id, context_used_tokens=50000, context_window_tokens=200000)
    superuser_db.commit()

    envelope = context_utilization(superuser_db, owner_id=owner_id, attempt_id=attempt_id)
    assert envelope.missing_data is False
    assert envelope.value == pytest.approx(25.0)
    assert envelope.sample_size == 1
    assert envelope.trend == "increasing"


# ============================================================================ estimated_time_to_context_limit


def test_estimated_time_to_context_limit_missing_data_with_fewer_than_two_samples(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    attempt_id = uuid.uuid4()
    record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=assignment.id, attempt_id=attempt_id, context_used_tokens=1000, context_window_tokens=200000)
    superuser_db.commit()

    envelope = estimated_time_to_context_limit(superuser_db, owner_id=owner_id, attempt_id=attempt_id)
    assert envelope.missing_data is True
    assert envelope.value is None


def test_estimated_time_to_context_limit_projects_from_real_burn_rate(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    attempt_id = uuid.uuid4()

    s1 = record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=assignment.id, attempt_id=attempt_id, context_used_tokens=10000, context_window_tokens=110000)
    superuser_db.flush()
    s2 = record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=assignment.id, attempt_id=attempt_id, context_used_tokens=20000, context_window_tokens=110000)
    # Force a deterministic, known elapsed window instead of relying on real wall-clock speed
    # between two consecutive Python statements.
    s1.sampled_at = datetime.utcnow() - timedelta(seconds=100)
    s2.sampled_at = datetime.utcnow()
    superuser_db.flush()
    superuser_db.commit()

    envelope = estimated_time_to_context_limit(superuser_db, owner_id=owner_id, attempt_id=attempt_id)
    assert envelope.missing_data is False
    # rate = 10000 tokens / 100s = 100 tokens/s; remaining = 110000-20000=90000 -> eta=900s
    assert envelope.value == pytest.approx(900.0, rel=0.05)
    assert envelope.trend == "increasing"
    assert "rate" in envelope.method


def test_estimated_time_to_context_limit_missing_data_on_non_positive_burn_rate(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    attempt_id = uuid.uuid4()
    s1 = record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=assignment.id, attempt_id=attempt_id, context_used_tokens=50000, context_window_tokens=110000)
    superuser_db.flush()
    # A compaction/reset dropped usage -- burn rate goes negative, never a fabricated ETA.
    s2 = record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=assignment.id, attempt_id=attempt_id, context_used_tokens=5000, context_window_tokens=110000)
    s1.sampled_at = datetime.utcnow() - timedelta(seconds=100)
    s2.sampled_at = datetime.utcnow()
    superuser_db.flush()
    superuser_db.commit()

    envelope = estimated_time_to_context_limit(superuser_db, owner_id=owner_id, attempt_id=attempt_id)
    assert envelope.missing_data is True
    assert envelope.value is None


# ============================================================================ idle_productive_blocked_time


def test_idle_productive_blocked_time_missing_data_with_zero_status_events(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    superuser_db.commit()

    result = idle_productive_blocked_time(superuser_db, owner_id=owner_id, assignment_id=assignment.id)
    assert set(result.keys()) == {"idle_seconds", "productive_seconds", "blocked_seconds"}
    for envelope in result.values():
        assert envelope.missing_data is True
        assert envelope.value is None


def test_idle_productive_blocked_time_missing_data_for_unowned_assignment(superuser_db, owner_id):
    result = idle_productive_blocked_time(superuser_db, owner_id=owner_id, assignment_id=uuid.uuid4())
    for envelope in result.values():
        assert envelope.missing_data is True


def test_idle_productive_blocked_time_derives_real_buckets_from_status_and_execution_history(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    superuser_db.commit()

    # planned -> ready (idle) -> running (productive, corroborated by a real dispatch
    # execution) -> blocked (blocked) -> ready (idle) -> completed (terminal, closes timeline).
    transition_status(superuser_db, assignment=assignment, new_status="ready")
    transition_status(superuser_db, assignment=assignment, new_status="running")
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="fake-cli", attempt_id=uuid.uuid4())
    transition_status(superuser_db, assignment=assignment, new_status="blocked", detail={"reason": "waiting_for_founder"})
    transition_status(superuser_db, assignment=assignment, new_status="ready")
    transition_status(superuser_db, assignment=assignment, new_status="running")
    transition_status(superuser_db, assignment=assignment, new_status="completed")
    superuser_db.flush()

    # Pin the dispatch execution's real wall-clock span to fully cover the FIRST running
    # interval, so productive_seconds has a deterministic, real, non-zero value to assert on.
    events = list(superuser_db.execute(
        select(AgentWorkAssignmentEvent)
        .where(AgentWorkAssignmentEvent.assignment_id == assignment.id)
        .order_by(AgentWorkAssignmentEvent.created_at)
    ).scalars())
    running_events = [e for e in events if (e.detail or {}).get("to") == "running"]
    assert len(running_events) == 2
    first_running_start = running_events[0].created_at
    first_running_end = [e.created_at for e in events if e.created_at > first_running_start][0]
    execution.started_at = first_running_start
    execution.ended_at = first_running_end
    superuser_db.flush()
    superuser_db.commit()

    result = idle_productive_blocked_time(superuser_db, owner_id=owner_id, assignment_id=assignment.id)
    assert result["idle_seconds"].missing_data is False
    assert result["productive_seconds"].missing_data is False
    assert result["blocked_seconds"].missing_data is False
    assert result["productive_seconds"].value > 0
    assert result["blocked_seconds"].value > 0
    assert result["idle_seconds"].value > 0
    assert result["productive_seconds"].sample_size == result["idle_seconds"].sample_size == result["blocked_seconds"].sample_size
