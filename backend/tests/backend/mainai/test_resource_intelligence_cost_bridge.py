"""MainAI Resource Intelligence Part 1 -- `app.resource_intelligence.cost_bridge` -- proves
the read-only join from `AgentWorkAssignment` (`app.agent_coordination`) to real, SETTLED
`ProviderSpendUsageEvent` rows (`app.provider_spend`), the "UNKNOWN stays UNKNOWN" contract
when zero usage events match, `cost_per_accepted_commit()`'s never-divide-by-zero guarantee,
and that `populate_agent_outcome_cost_fields()` becomes the first real caller to actually fill
`app.agent_coordination.service.build_agent_outcome_payload()`'s own `cost_tokens`/`cost_usd`/
`duration_seconds` fields -- against a real Postgres database, never mocked.

See docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md §0's "confirmed gap: this
ledger ties to goal_id/task_id/job_id -- never to agent_id" for why this bridge exists."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.agent_coordination.execution_control import mark_execution_exited, start_execution_tracking
from app.agent_coordination.service import create_work_assignment, register_agent, transition_status
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.mainai_execution import MainAITask
from app.provider_spend import authorize_provider_spend, record_provider_spend_usage
from app.resource_intelligence.cost_bridge import (
    cost_for_assignment,
    cost_per_accepted_commit,
    populate_agent_outcome_cost_fields,
    tokens_for_assignment,
)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_plan_task(db, owner_id, *, instruction="Resource intelligence cost bridge test."):
    goal = create_goal(db, owner_id=owner_id, title="RI cost bridge test", original_instruction=instruction, created_by="founder", approval_policy="standard_repo_work")
    plan = create_plan(db, goal=goal, rationale="ri cost bridge test", tasks=[PlannedTaskSpec(description="Task 0", task_type="repo_edit")], created_by="founder")
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).first()
    return goal, task


def _agent(db, key="agent"):
    return register_agent(
        db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli",
        execution_mode="cli_interactive", supports_read=True, supports_write=True, concurrency_limit=3,
    )


def _assignment(db, *, owner_id, goal, task, agent, **kwargs):
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=task.id if task else None, agent_id=agent.id,
        role="builder", read_write_mode="read_write", repository_identity="lifeai",
        allowed_paths=["backend/app/resource_intelligence/**"], requested_by="test", **kwargs,
    )


def _spend_authorization(db, *, owner_id, goal, **overrides):
    proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal.id, idempotency_key=f"prop-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(
        db, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
        authorized_paths=["README.md"], authorized_capabilities=["read_file", "patch_file"],
        authorized_risk="low", envelope_idempotency_key=f"env-{uuid.uuid4()}",
    )
    kwargs = dict(
        owner_id=owner_id, goal_id=goal.id, execution_envelope_id=envelope.id, authorized_by="founder",
        max_cost_usd=Decimal("10.00"), max_requests=10, max_cost_per_request_usd=Decimal("1.00"),
        idempotency_key=f"spend-{uuid.uuid4()}", allowed_providers=["fake-local"], allowed_models=["planner-v2"],
    )
    kwargs.update(overrides)
    return authorize_provider_spend(db, **kwargs)


def _settle_usage(db, *, owner_id, goal, task=None, cost_usd="0.10", prompt_tokens=100, completion_tokens=50):
    return record_provider_spend_usage(
        db, owner_id=owner_id, goal_id=goal.id, source_ref=f"src-{uuid.uuid4()}", provider="fake-local", model="planner-v2",
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost_usd=Decimal(cost_usd),
        task_id=task.id if task else None,
    )


# ============================================================================ cost_for_assignment / tokens_for_assignment


def test_cost_for_assignment_missing_data_with_zero_provider_spend_events(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    superuser_db.commit()

    envelope = cost_for_assignment(superuser_db, owner_id=owner_id, assignment_id=assignment.id)
    assert envelope.missing_data is True
    assert envelope.value is None
    assert envelope.unit == "usd"


def test_tokens_for_assignment_missing_data_with_zero_provider_spend_events(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    superuser_db.commit()

    result = tokens_for_assignment(superuser_db, owner_id=owner_id, assignment_id=assignment.id)
    assert set(result.keys()) == {"prompt_tokens", "completion_tokens"}
    for envelope in result.values():
        assert envelope.missing_data is True
        assert envelope.value is None


def test_cost_for_assignment_missing_data_for_nonexistent_assignment(superuser_db, owner_id):
    envelope = cost_for_assignment(superuser_db, owner_id=owner_id, assignment_id=uuid.uuid4())
    assert envelope.missing_data is True


def test_cost_for_assignment_sums_real_settled_events_joined_on_goal_and_task(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    _spend_authorization(superuser_db, owner_id=owner_id, goal=goal)
    superuser_db.flush()

    _settle_usage(superuser_db, owner_id=owner_id, goal=goal, task=task, cost_usd="0.30", prompt_tokens=100, completion_tokens=40)
    _settle_usage(superuser_db, owner_id=owner_id, goal=goal, task=task, cost_usd="0.20", prompt_tokens=60, completion_tokens=10)
    superuser_db.commit()

    cost = cost_for_assignment(superuser_db, owner_id=owner_id, assignment_id=assignment.id)
    assert cost.missing_data is False
    assert cost.value == pytest.approx(0.50)
    assert cost.sample_size == 2

    tokens = tokens_for_assignment(superuser_db, owner_id=owner_id, assignment_id=assignment.id)
    assert tokens["prompt_tokens"].value == 160
    assert tokens["completion_tokens"].value == 50


def test_cost_for_assignment_never_attributes_another_tasks_settled_spend(superuser_db, owner_id):
    """Two tasks under the SAME goal -- spend settled against the OTHER task must never be
    counted for this assignment's own task-scoped cost."""

    goal = create_goal(superuser_db, owner_id=owner_id, title="two task goal", original_instruction="x", created_by="founder", approval_policy="standard_repo_work")
    from app.mainai_execution.planner import create_plan as _create_plan

    plan = _create_plan(superuser_db, goal=goal, rationale="two tasks", tasks=[
        PlannedTaskSpec(description="Task A", task_type="repo_edit"),
        PlannedTaskSpec(description="Task B", task_type="repo_edit"),
    ], created_by="founder")
    superuser_db.commit()
    tasks = superuser_db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).all()
    task_a, task_b = tasks[0], tasks[1]

    agent = _agent(superuser_db)
    assignment_a = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=agent)
    _spend_authorization(superuser_db, owner_id=owner_id, goal=goal)
    superuser_db.flush()

    _settle_usage(superuser_db, owner_id=owner_id, goal=goal, task=task_a, cost_usd="0.25")
    _settle_usage(superuser_db, owner_id=owner_id, goal=goal, task=task_b, cost_usd="0.99")
    superuser_db.commit()

    cost = cost_for_assignment(superuser_db, owner_id=owner_id, assignment_id=assignment_a.id)
    assert cost.value == pytest.approx(0.25)


# ============================================================================ cost_per_accepted_commit


def test_cost_per_accepted_commit_never_divides_by_zero(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    _spend_authorization(superuser_db, owner_id=owner_id, goal=goal)
    superuser_db.flush()
    _settle_usage(superuser_db, owner_id=owner_id, goal=goal, task=task, cost_usd="0.50")
    superuser_db.commit()

    # Real cost exists, but zero assignments ever reached completed/verified.
    envelope = cost_per_accepted_commit(superuser_db, owner_id=owner_id, agent_id=agent.id)
    assert envelope.missing_data is True
    assert envelope.value is None


def test_cost_per_accepted_commit_missing_data_for_agent_with_no_assignments(superuser_db, owner_id):
    agent = _agent(superuser_db)
    superuser_db.commit()
    envelope = cost_per_accepted_commit(superuser_db, owner_id=owner_id, agent_id=agent.id)
    assert envelope.missing_data is True


def test_cost_per_accepted_commit_divides_real_cost_by_real_completions(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    _spend_authorization(superuser_db, owner_id=owner_id, goal=goal)
    superuser_db.flush()
    _settle_usage(superuser_db, owner_id=owner_id, goal=goal, task=task, cost_usd="1.00")
    superuser_db.flush()

    transition_status(superuser_db, assignment=assignment, new_status="ready")
    transition_status(superuser_db, assignment=assignment, new_status="running")
    transition_status(superuser_db, assignment=assignment, new_status="completed")
    superuser_db.commit()

    envelope = cost_per_accepted_commit(superuser_db, owner_id=owner_id, agent_id=agent.id)
    assert envelope.missing_data is False
    assert envelope.value == pytest.approx(1.00)
    assert envelope.sample_size == 1


# ============================================================================ populate_agent_outcome_cost_fields


def test_populate_agent_outcome_cost_fields_calls_the_real_payload_builder(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    _spend_authorization(superuser_db, owner_id=owner_id, goal=goal)
    superuser_db.flush()
    _settle_usage(superuser_db, owner_id=owner_id, goal=goal, task=task, cost_usd="0.75", prompt_tokens=200, completion_tokens=80)
    superuser_db.flush()

    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="fake-cli", attempt_id=uuid.uuid4())
    execution.started_at = datetime.utcnow() - timedelta(seconds=42)
    mark_execution_exited(superuser_db, execution=execution)
    superuser_db.commit()

    payload = populate_agent_outcome_cost_fields(superuser_db, owner_id=owner_id, assignment_id=assignment.id)
    assert payload["cost_usd"] == pytest.approx(0.75)
    assert payload["cost_tokens"] == 280
    assert payload["duration_seconds"] is not None
    assert payload["duration_seconds"] > 0
    # build_agent_outcome_payload() drops every field this caller did not supply.
    assert "tests_passed" not in payload
    assert "ci_conclusion" not in payload


def test_populate_agent_outcome_cost_fields_omits_unknown_fields_never_fabricates_zero(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    superuser_db.commit()

    payload = populate_agent_outcome_cost_fields(superuser_db, owner_id=owner_id, assignment_id=assignment.id)
    # No provider spend, no dispatch execution -- every cost-related field must be ABSENT
    # (build_agent_outcome_payload drops None values), never a fabricated 0.
    assert "cost_usd" not in payload
    assert "cost_tokens" not in payload
    assert "duration_seconds" not in payload
