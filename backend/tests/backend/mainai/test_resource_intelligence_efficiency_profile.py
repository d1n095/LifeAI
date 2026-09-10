"""MainAI Resource Intelligence Part 2 -- `app.resource_intelligence.efficiency_profile` --
proves the real N-observation, recency-weighted profile computation from REAL underlying data
(`AgentWorkAssignment` terminal outcomes, `AgentWorkAssignmentEvent` status_changed history,
`agent_resource_telemetry_samples`, and `cost_bridge.cost_per_accepted_commit()` passed through
verbatim), the `MIN_SAMPLE_SIZE_FOR_ESTABLISHED` provisional/established gate (mutation-style:
identical assignment shape, below vs at the threshold), the real rework-rate signal derived from
`changes_requested` history, and the "excluded, never fabricated zero" telemetry contract for
`context_efficiency` -- against a real Postgres database, never mocked.

See docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md §1.5 for the architecture this
module implements."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.agent_coordination.execution_control import start_execution_tracking
from app.agent_coordination.service import create_work_assignment, register_agent, transition_status
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.mainai_execution import MainAITask
from app.provider_spend import authorize_provider_spend, record_provider_spend_usage
from app.resource_intelligence.cost_bridge import cost_per_accepted_commit
from app.resource_intelligence.efficiency_profile import (
    MIN_SAMPLE_SIZE_FOR_ESTABLISHED,
    PROFILE_METRIC_KEYS,
    agent_efficiency_profile,
    is_provisional,
)
from app.resource_intelligence.telemetry import record_telemetry_sample
from app.resource_intelligence.types import ResourceIntelligenceError


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_plan_task(db, owner_id, *, instruction="Resource intelligence efficiency profile test."):
    goal = create_goal(db, owner_id=owner_id, title="RI efficiency profile test", original_instruction=instruction, created_by="founder", approval_policy="standard_repo_work")
    plan = create_plan(db, goal=goal, rationale="ri efficiency profile test", tasks=[PlannedTaskSpec(description="Task 0", task_type="repo_edit")], created_by="founder")
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).first()
    return goal, task


def _agent(db, key="agent"):
    return register_agent(
        db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli",
        execution_mode="cli_interactive", supports_read=True, supports_write=True, concurrency_limit=3,
    )


def _new_assignment(db, *, owner_id, goal, task, agent, role="builder"):
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=task.id if task else None, agent_id=agent.id,
        role=role, read_write_mode="read_write", repository_identity="lifeai",
        allowed_paths=["backend/app/resource_intelligence/**"], requested_by="test",
    )


def _run_to_completed(db, assignment):
    transition_status(db, assignment=assignment, new_status="ready")
    transition_status(db, assignment=assignment, new_status="running")
    transition_status(db, assignment=assignment, new_status="completed")


def _run_to_failed(db, assignment):
    transition_status(db, assignment=assignment, new_status="ready")
    transition_status(db, assignment=assignment, new_status="running")
    transition_status(db, assignment=assignment, new_status="failed")


def _run_to_completed_with_rework(db, assignment):
    transition_status(db, assignment=assignment, new_status="ready")
    transition_status(db, assignment=assignment, new_status="running")
    transition_status(db, assignment=assignment, new_status="ready_for_review")
    transition_status(db, assignment=assignment, new_status="reviewing")
    transition_status(db, assignment=assignment, new_status="changes_requested")
    transition_status(db, assignment=assignment, new_status="running")
    transition_status(db, assignment=assignment, new_status="completed")


def _run_to_completed_without_rework(db, assignment):
    transition_status(db, assignment=assignment, new_status="ready")
    transition_status(db, assignment=assignment, new_status="running")
    transition_status(db, assignment=assignment, new_status="ready_for_review")
    transition_status(db, assignment=assignment, new_status="reviewing")
    transition_status(db, assignment=assignment, new_status="completed")


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


# ============================================================================ zero data / structure


def test_profile_returns_exactly_the_documented_keys(superuser_db, owner_id):
    agent = _agent(superuser_db)
    superuser_db.commit()
    profile = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    assert set(profile.keys()) == set(PROFILE_METRIC_KEYS)


def test_profile_missing_data_for_agent_with_zero_assignments(superuser_db, owner_id):
    agent = _agent(superuser_db)
    superuser_db.commit()

    profile = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    assert profile["accepted_commit_rate"].missing_data is True
    assert profile["rework_rate"].missing_data is True
    assert profile["cost_per_accepted_commit"].missing_data is True
    assert profile["context_efficiency"].missing_data is True
    # Zero samples is always provisional -- every envelope must say so.
    for envelope in profile.values():
        assert "PROVISIONAL" in (envelope.uncertainty or "")


def test_invalid_task_type_raises_resource_intelligence_error(superuser_db, owner_id):
    agent = _agent(superuser_db)
    superuser_db.commit()
    with pytest.raises(ResourceIntelligenceError):
        agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id, task_type="not_a_real_role")


# ============================================================================ provisional/established gate (mutation-style)


def test_provisional_gate_changes_behavior_below_vs_at_minimum_sample_size(superuser_db, owner_id):
    """Identical real assignment history (all straightforward completions), scaled to
    MIN_SAMPLE_SIZE_FOR_ESTABLISHED - 1 vs MIN_SAMPLE_SIZE_FOR_ESTABLISHED -- the PROVISIONAL
    note must be present below the threshold and absent at/above it. This is the concrete
    mechanism enforcing ONE_RUN != LONG_TERM_PROFILE."""

    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)

    for _ in range(MIN_SAMPLE_SIZE_FOR_ESTABLISHED - 1):
        assignment = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
        _run_to_completed(superuser_db, assignment)
    superuser_db.commit()

    below = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    assert below["accepted_commit_rate"].sample_size == MIN_SAMPLE_SIZE_FOR_ESTABLISHED - 1
    assert is_provisional(below["accepted_commit_rate"].sample_size) is True
    for envelope in below.values():
        assert "PROVISIONAL" in (envelope.uncertainty or "")

    # One more identically-shaped completion crosses the threshold.
    assignment = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    _run_to_completed(superuser_db, assignment)
    superuser_db.commit()

    at_threshold = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    assert at_threshold["accepted_commit_rate"].sample_size == MIN_SAMPLE_SIZE_FOR_ESTABLISHED
    assert is_provisional(at_threshold["accepted_commit_rate"].sample_size) is False
    assert "PROVISIONAL" not in (at_threshold["accepted_commit_rate"].uncertainty or "")
    assert "PROVISIONAL" not in (at_threshold["rework_rate"].uncertainty or "")


# ============================================================================ accepted_commit_rate


def test_accepted_commit_rate_reflects_real_terminal_outcomes(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)

    for _ in range(3):
        a = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
        _run_to_completed(superuser_db, a)
    for _ in range(2):
        a = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
        _run_to_failed(superuser_db, a)
    superuser_db.commit()

    profile = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    envelope = profile["accepted_commit_rate"]
    assert envelope.missing_data is False
    assert envelope.sample_size == 5
    # All assignments complete "now" -- recency weights are all ~1.0, so the weighted rate
    # should closely track the raw 3/5 fraction.
    assert envelope.value == pytest.approx(0.6, abs=0.01)


# ============================================================================ rework_rate


def test_rework_rate_counts_only_assignments_that_passed_through_changes_requested(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)

    reworked = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    _run_to_completed_with_rework(superuser_db, reworked)

    clean = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    _run_to_completed_without_rework(superuser_db, clean)
    superuser_db.commit()

    profile = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    envelope = profile["rework_rate"]
    assert envelope.missing_data is False
    assert envelope.sample_size == 2
    assert envelope.value == pytest.approx(0.5, abs=0.01)


# ============================================================================ cost_per_accepted_commit passthrough


def test_cost_per_accepted_commit_is_cost_bridges_own_envelope_verbatim(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    _spend_authorization(superuser_db, owner_id=owner_id, goal=goal)
    superuser_db.flush()
    _settle_usage(superuser_db, owner_id=owner_id, goal=goal, task=task, cost_usd="0.90")
    superuser_db.flush()
    _run_to_completed(superuser_db, assignment)
    superuser_db.commit()

    direct = cost_per_accepted_commit(superuser_db, owner_id=owner_id, agent_id=agent.id)
    profile = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    via_profile = profile["cost_per_accepted_commit"]

    assert via_profile.missing_data is False
    assert via_profile.value == pytest.approx(direct.value)
    assert via_profile.method == direct.method  # same object's own real method string, not reimplemented


def test_cost_per_accepted_commit_notes_it_is_agent_wide_when_task_type_is_requested(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent, role="builder")
    _spend_authorization(superuser_db, owner_id=owner_id, goal=goal)
    superuser_db.flush()
    _settle_usage(superuser_db, owner_id=owner_id, goal=goal, task=task, cost_usd="0.40")
    superuser_db.flush()
    _run_to_completed(superuser_db, assignment)
    superuser_db.commit()

    profile = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id, task_type="builder")
    assert "task_type" in (profile["cost_per_accepted_commit"].uncertainty or "")


# ============================================================================ context_efficiency


def test_context_efficiency_sums_real_telemetry_and_excludes_assignments_without_samples(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)

    with_telemetry = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    attempt_id = uuid.uuid4()
    start_execution_tracking(superuser_db, assignment=with_telemetry, adapter_key="fake-cli", attempt_id=attempt_id)
    record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=with_telemetry.id, attempt_id=attempt_id, input_tokens=100, output_tokens=20)
    record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=with_telemetry.id, attempt_id=attempt_id, input_tokens=50, output_tokens=10)
    superuser_db.flush()
    _run_to_completed(superuser_db, with_telemetry)

    without_telemetry = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    _run_to_completed(superuser_db, without_telemetry)
    superuser_db.commit()

    profile = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    envelope = profile["context_efficiency"]
    assert envelope.missing_data is False
    assert envelope.value == pytest.approx(180.0)  # (100+20) + (50+10)
    assert envelope.sample_size == 1  # only the assignment WITH telemetry counts
    assert "zero telemetry" in (envelope.uncertainty or "") or "1 accepted assignment" in (envelope.uncertainty or "")


def test_context_efficiency_missing_data_when_no_accepted_assignment_has_telemetry(superuser_db, owner_id):
    goal, task = _goal_plan_task(superuser_db, owner_id)
    agent = _agent(superuser_db)
    assignment = _new_assignment(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    _run_to_completed(superuser_db, assignment)
    superuser_db.commit()

    profile = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    assert profile["context_efficiency"].missing_data is True
    assert profile["context_efficiency"].value is None
