"""Independent adversarial pass over the whole "Resource Intelligence + Context Lifecycle +
Cost/Quota + Agent Efficiency" program (docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_
RECONCILIATION.md).

BUILDER != FINAL EXAMINER: Part 1 (types/telemetry/cost_bridge/session_checkpoint) and Part 2
(efficiency_profile/decision/scheduler) were each built and unit-tested by the agent that wrote
them. This file is a SEPARATE pass, composing the real modules end-to-end against real
Postgres rows and the REAL `app.agent_coordination` state machine (`transition_status()`'s own
`ALLOWED_TRANSITIONS` table), not the isolated unit-level fixtures each builder already used."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest

from app.agent_coordination.execution_control import start_execution_tracking, mark_execution_exited
from app.agent_coordination.service import (
    create_work_assignment,
    register_agent,
    transition_status,
)
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.mainai_execution import MainAITask
from app.resource_intelligence.decision import propose_resource_action
from app.resource_intelligence.efficiency_profile import MIN_SAMPLE_SIZE_FOR_ESTABLISHED, agent_efficiency_profile
from app.resource_intelligence.scheduler import next_best_resource_allocation
from app.resource_intelligence.session_checkpoint import (
    AgentSessionCheckpoint,
    load_agent_session_checkpoint,
    save_agent_session_checkpoint,
)
from app.resource_intelligence.telemetry import (
    context_utilization,
    estimated_time_to_context_limit,
    idle_productive_blocked_time,
    record_telemetry_sample,
)
from app.resource_intelligence.types import ContextLifecycleAction


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_task(db, owner_id, *, instruction="Resource intelligence longitudinal scenario."):
    goal = create_goal(
        db, owner_id=owner_id, title="RI scenario", original_instruction=instruction,
        created_by="founder", approval_policy="autonomous_development_work",
    )
    plan = create_plan(
        db, goal=goal, rationale="RI scenario",
        tasks=[PlannedTaskSpec(description="Task", task_type="repo_edit")],
        created_by="founder",
    )
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).first()
    return goal, task


def _agent(db, key, **kwargs):
    defaults = dict(display_name=key, adapter_kind="cli", execution_mode="cli_interactive", supports_read=True, supports_write=True, concurrency_limit=3)
    defaults.update(kwargs)
    return register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", **defaults)


def _assign(db, *, owner_id, goal, task, agent, role="builder", mode="read_write"):
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=task.id if task else None, agent_id=agent.id,
        role=role, read_write_mode=mode, repository_identity="lifeai", allowed_paths=["backend/**"], requested_by="test",
    )


def _run_to_terminal(db, assignment, *, terminal="completed", via_review=False, reworked=False):
    """Drives a real assignment through the REAL ALLOWED_TRANSITIONS state machine to a
    terminal status, exactly like a real builder/reviewer flow would."""
    transition_status(db, assignment=assignment, new_status="ready")
    transition_status(db, assignment=assignment, new_status="running")
    if reworked:
        transition_status(db, assignment=assignment, new_status="ready_for_review")
        transition_status(db, assignment=assignment, new_status="reviewing")
        transition_status(db, assignment=assignment, new_status="changes_requested")
        transition_status(db, assignment=assignment, new_status="running")
    if via_review:
        transition_status(db, assignment=assignment, new_status="ready_for_review")
        transition_status(db, assignment=assignment, new_status="reviewing")
        transition_status(db, assignment=assignment, new_status=terminal)
    else:
        transition_status(db, assignment=assignment, new_status=terminal)
    db.commit()
    return assignment


def _backdate_completion(db, assignment, *, days_ago):
    assignment.completed_at = datetime.utcnow() - timedelta(days=days_ago)
    db.commit()


# --- A. Real end-to-end context-lifecycle chain: telemetry -> metrics -> decision. -----------


def test_scenario_a_real_telemetry_chain_drives_checkpoint_recommendation(superuser_db, owner_id):
    agent = _agent(superuser_db, "claude-code")
    goal, task = _goal_task(superuser_db, owner_id)
    assignment = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    superuser_db.commit()
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="cli", attempt_id=uuid.uuid4())
    superuser_db.commit()

    first = record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=assignment.id, attempt_id=execution.attempt_id, context_used_tokens=50_000, context_window_tokens=200_000)
    first.sampled_at = datetime.utcnow() - timedelta(seconds=60)
    second = record_telemetry_sample(superuser_db, owner_id=owner_id, assignment_id=assignment.id, attempt_id=execution.attempt_id, context_used_tokens=185_000, context_window_tokens=200_000)
    second.sampled_at = datetime.utcnow()
    superuser_db.commit()

    util = context_utilization(superuser_db, owner_id=owner_id, attempt_id=execution.attempt_id)
    assert util.value >= 90.0
    ttl = estimated_time_to_context_limit(superuser_db, owner_id=owner_id, attempt_id=execution.attempt_id)

    recommendation = propose_resource_action(
        context_utilization=util, time_to_limit=ttl, critical_unsummarized_state=True,
    )
    assert recommendation.action == ContextLifecycleAction.CHECKPOINT
    assert recommendation.authorized is False

    # Following the recommendation for real: the checkpoint mechanism actually persists.
    checkpoint = AgentSessionCheckpoint(
        agent_id=agent.id, attempt_id=execution.attempt_id, current_objective="finish scenario A",
        current_status="context nearly exhausted, checkpointing per recommendation", exact_sha="deadbeef",
        current_branch="claude/scenario-a", current_worktree="/tmp/scenario-a", open_p0=[], open_p1=[],
        what_was_tried=["recorded telemetry"], what_failed=[], what_passed=["checkpoint recommendation fired"],
        important_findings=["context utilization crossed 90%"], current_test_evidence=[], next_action="reset and resume",
        do_not_repeat=[], authority_boundaries=["advisory only"], unresolved_questions=[], critical_session_only_facts=["scenario A marker"],
    )
    save_agent_session_checkpoint(superuser_db, owner_id=owner_id, checkpoint=checkpoint)
    superuser_db.commit()
    loaded = load_agent_session_checkpoint(superuser_db, owner_id=owner_id, agent_id=agent.id, attempt_id=execution.attempt_id)
    assert loaded == checkpoint


# --- B. WIP-at-limit biases DEFER through the real scheduler composition, not just the pure fn.


def test_scenario_b_wip_at_limit_biases_defer_through_real_scheduler(superuser_db, owner_id):
    agent = _agent(superuser_db, "cursor-agent", concurrency_limit=1)
    goal, task = _goal_task(superuser_db, owner_id)
    assignment = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    transition_status(superuser_db, assignment=assignment, new_status="ready")
    transition_status(superuser_db, assignment=assignment, new_status="running")
    superuser_db.commit()

    rows = next_best_resource_allocation(superuser_db, owner_id=owner_id)
    my_row = next(r for r in rows if r["assignment_id"] == str(assignment.id))
    assert my_row["wip_at_limit"] is True
    assert my_row["recommendation"]["action"] == ContextLifecycleAction.DEFER.value
    assert my_row["recommendation"]["authorized"] is False


# --- C. Established poor efficiency profile -> HANDOFF, built from REAL failure history. -----


def test_scenario_c_real_failure_history_produces_established_handoff_recommendation(superuser_db, owner_id):
    agent = _agent(superuser_db, "struggling-agent")
    goal, _task = _goal_task(superuser_db, owner_id)

    # MIN_SAMPLE_SIZE_FOR_ESTABLISHED real assignments, all FAILED -- a genuinely poor,
    # established track record, not a synthetic dict.
    for i in range(MIN_SAMPLE_SIZE_FOR_ESTABLISHED):
        _, task_i = _goal_task(superuser_db, owner_id, instruction=f"failing task {i}")
        a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_i, agent=agent)
        transition_status(superuser_db, assignment=a, new_status="ready")
        transition_status(superuser_db, assignment=a, new_status="running")
        transition_status(superuser_db, assignment=a, new_status="failed")
        superuser_db.commit()
        _backdate_completion(superuser_db, a, days_ago=1)

    profile = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    assert profile["accepted_commit_rate"].missing_data is False
    assert profile["accepted_commit_rate"].value == 0.0
    assert profile["accepted_commit_rate"].sample_size == MIN_SAMPLE_SIZE_FOR_ESTABLISHED
    assert "PROVISIONAL" not in (profile["accepted_commit_rate"].uncertainty or "")

    recommendation = propose_resource_action(
        context_utilization=context_utilization(superuser_db, owner_id=owner_id, attempt_id=uuid.uuid4()),
        time_to_limit=estimated_time_to_context_limit(superuser_db, owner_id=owner_id, attempt_id=uuid.uuid4()),
        efficiency_profile=profile,
    )
    assert recommendation.action == ContextLifecycleAction.HANDOFF


def test_scenario_c2_below_threshold_same_history_stays_provisional_and_never_recommends_handoff(superuser_db, owner_id):
    """Mutation-style proof: the IDENTICAL failure pattern, one observation short of the real
    threshold, must NOT yet produce a confident HANDOFF -- ONE_RUN != LONG_TERM_PROFILE."""
    agent = _agent(superuser_db, "almost-enough-data-agent")
    goal, _task = _goal_task(superuser_db, owner_id)

    for i in range(MIN_SAMPLE_SIZE_FOR_ESTABLISHED - 1):
        _, task_i = _goal_task(superuser_db, owner_id, instruction=f"failing task {i}")
        a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_i, agent=agent)
        transition_status(superuser_db, assignment=a, new_status="ready")
        transition_status(superuser_db, assignment=a, new_status="running")
        transition_status(superuser_db, assignment=a, new_status="failed")
        superuser_db.commit()

    profile = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    assert profile["accepted_commit_rate"].value == 0.0
    assert "PROVISIONAL" in (profile["accepted_commit_rate"].uncertainty or "")

    recommendation = propose_resource_action(
        context_utilization=context_utilization(superuser_db, owner_id=owner_id, attempt_id=uuid.uuid4()),
        time_to_limit=estimated_time_to_context_limit(superuser_db, owner_id=owner_id, attempt_id=uuid.uuid4()),
        efficiency_profile=profile,
    )
    assert recommendation.action != ContextLifecycleAction.HANDOFF


# --- D. Rework-heavy but eventually-successful history -> established CHANGE_MODEL. ----------


def test_scenario_d_high_rework_rate_with_acceptable_completion_rate_recommends_change_model(superuser_db, owner_id):
    agent = _agent(superuser_db, "rework-heavy-agent")
    goal, _task = _goal_task(superuser_db, owner_id)

    for i in range(MIN_SAMPLE_SIZE_FOR_ESTABLISHED):
        _, task_i = _goal_task(superuser_db, owner_id, instruction=f"reworked task {i}")
        a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_i, agent=agent)
        _run_to_terminal(superuser_db, a, terminal="completed", via_review=True, reworked=True)
        _backdate_completion(superuser_db, a, days_ago=1)

    profile = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    assert profile["accepted_commit_rate"].value == 1.0
    assert profile["rework_rate"].value == 1.0

    recommendation = propose_resource_action(
        context_utilization=context_utilization(superuser_db, owner_id=owner_id, attempt_id=uuid.uuid4()),
        time_to_limit=estimated_time_to_context_limit(superuser_db, owner_id=owner_id, attempt_id=uuid.uuid4()),
        efficiency_profile=profile,
    )
    assert recommendation.action == ContextLifecycleAction.CHANGE_MODEL


# --- E. Recency weighting: a long-past bad run does not permanently dominate a recent good one.


def test_scenario_e_recency_weighting_lets_recent_good_history_outweigh_old_bad_history(superuser_db, owner_id):
    agent = _agent(superuser_db, "improved-agent")
    goal, _task = _goal_task(superuser_db, owner_id)

    for i in range(MIN_SAMPLE_SIZE_FOR_ESTABLISHED):
        _, task_i = _goal_task(superuser_db, owner_id, instruction=f"old failing task {i}")
        a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_i, agent=agent)
        transition_status(superuser_db, assignment=a, new_status="ready")
        transition_status(superuser_db, assignment=a, new_status="running")
        transition_status(superuser_db, assignment=a, new_status="failed")
        superuser_db.commit()
        _backdate_completion(superuser_db, a, days_ago=400)  # far beyond the 30-day half-life

    for i in range(MIN_SAMPLE_SIZE_FOR_ESTABLISHED):
        _, task_i = _goal_task(superuser_db, owner_id, instruction=f"recent good task {i}")
        a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_i, agent=agent)
        _run_to_terminal(superuser_db, a, terminal="completed")
        _backdate_completion(superuser_db, a, days_ago=1)

    profile = agent_efficiency_profile(superuser_db, owner_id=owner_id, agent_id=agent.id)
    # The old failures are NOT discarded (never 1.0), but recency weighting means the recent,
    # far-more-heavily-weighted successes dominate the rate.
    assert profile["accepted_commit_rate"].value > 0.9

    recommendation = propose_resource_action(
        context_utilization=context_utilization(superuser_db, owner_id=owner_id, attempt_id=uuid.uuid4()),
        time_to_limit=estimated_time_to_context_limit(superuser_db, owner_id=owner_id, attempt_id=uuid.uuid4()),
        efficiency_profile=profile,
    )
    assert recommendation.action != ContextLifecycleAction.HANDOFF


# --- F. Idle/productive/blocked derivation over a REAL multi-transition history. --------------


def test_scenario_f_idle_productive_blocked_derived_from_real_transition_and_dispatch_history(superuser_db, owner_id):
    agent = _agent(superuser_db, "timeline-agent")
    goal, task = _goal_task(superuser_db, owner_id)
    assignment = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=agent)
    superuser_db.commit()

    transition_status(superuser_db, assignment=assignment, new_status="ready")
    superuser_db.commit()
    execution = start_execution_tracking(superuser_db, assignment=assignment, adapter_key="cli", attempt_id=uuid.uuid4())
    superuser_db.commit()
    transition_status(superuser_db, assignment=assignment, new_status="running")
    superuser_db.commit()
    mark_execution_exited(superuser_db, execution=execution)
    transition_status(superuser_db, assignment=assignment, new_status="waiting_review")
    superuser_db.commit()
    transition_status(superuser_db, assignment=assignment, new_status="reviewing")
    superuser_db.commit()
    transition_status(superuser_db, assignment=assignment, new_status="completed")
    superuser_db.commit()

    buckets = idle_productive_blocked_time(superuser_db, owner_id=owner_id, assignment_id=assignment.id)
    assert buckets["productive_seconds"].missing_data is False
    assert buckets["productive_seconds"].value >= 0.0
    # waiting_review is a BLOCKED bucket per telemetry.py's own real classification.
    assert buckets["blocked_seconds"].value > 0.0 or buckets["idle_seconds"].value > 0.0


# --- G. Cross-owner isolation across the full composed system. -------------------------------


def test_scenario_g_cross_owner_isolation_across_telemetry_cost_and_efficiency_profile(superuser_db, make_verified_user):
    alice, _ = make_verified_user()
    bob, _ = make_verified_user()

    agent = _agent(superuser_db, "shared-name-agent")
    goal_a, task_a = _goal_task(superuser_db, alice.id, instruction="alice's task")
    assignment_a = _assign(superuser_db, owner_id=alice.id, goal=goal_a, task=task_a, agent=agent)
    _run_to_terminal(superuser_db, assignment_a, terminal="completed")

    # Bob has NO assignments for this agent at all.
    profile_bob = agent_efficiency_profile(superuser_db, owner_id=bob.id, agent_id=agent.id)
    assert profile_bob["accepted_commit_rate"].missing_data is True

    from app.resource_intelligence.types import ResourceIntelligenceError
    with pytest.raises(ResourceIntelligenceError):
        record_telemetry_sample(superuser_db, owner_id=bob.id, assignment_id=assignment_a.id, attempt_id=uuid.uuid4(), context_used_tokens=1)
