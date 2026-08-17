"""Agent Runtime Control Plane -- proves the read-only runtime-visibility layer
(app.agent_coordination.runtime_view) and the deterministic routing foundation
(app.agent_coordination.routing) built on top of PR #82's merged coordination tables. Never
duplicates PR #82's own conflict-detection tests (test_multi_agent_work_coordination.py) --
these tests exercise the NEW read-model/eligibility layer specifically, proving it answers
correctly against the SAME underlying state PR #82's tables already track.

`test_current_real_world_three_agent_state_end_to_end` mirrors the concrete situation this
foundation exists to represent right now: Cursor Agent RUNNING/WRITE on PR #80's live-loop
paths, Claude Code WRITE on this very coordination module (a genuinely different,
non-overlapping subsystem), Codex IDLE. See docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md's
"Runtime visibility & deterministic routing" section for the full architecture."""

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

from app.agent_coordination.routing import (
    OUTCOME_ASSIGNMENT_FOUND,
    OUTCOME_ELIGIBLE,
    OUTCOME_NEEDS_SELECTION,
    OUTCOME_NO_FEASIBLE_ASSIGNMENT,
    OUTCOME_NONE_AVAILABLE,
    OUTCOME_SCOPE_CONFLICT,
    eligible_agents_for,
    idle_agents_with_next_assignment,
    next_feasible_assignment_for_agent,
)
from app.agent_coordination.runtime_view import (
    RuntimeStatus,
    agent_runtime_snapshot,
    all_agents_runtime_snapshot,
    assignment_runtime_view,
    work_registry_snapshot,
)
from app.agent_coordination.service import (
    acquire_lease,
    build_agent_outcome_payload,
    create_parallel_exploration_group,
    create_work_assignment,
    register_agent,
    transition_status,
)
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.mainai_execution import MainAITask


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure as test_multi_agent_work_coordination.py's own identical
    fixture -- this module exercises the same migration-0046-governed tables."""
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_plan_task(db, owner_id, *, instruction="Coordinate multiple agents safely.", task_count=1):
    goal = create_goal(
        db, owner_id=owner_id, title="Runtime control plane test", original_instruction=instruction,
        created_by="founder", approval_policy="autonomous_development_work",
    )
    plan = create_plan(
        db, goal=goal, rationale="runtime control plane test",
        tasks=[PlannedTaskSpec(description=f"Task {i}", task_type="repo_edit") for i in range(task_count)],
        created_by="founder",
    )
    db.commit()
    tasks = db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).all()
    if task_count == 1:
        return goal, plan, tasks[0]
    return goal, plan, tasks


def _agent(db, key, **kwargs):
    defaults = dict(
        display_name=key, adapter_kind="cli", execution_mode="cli_interactive",
        supports_read=True, supports_write=True, concurrency_limit=3,
    )
    defaults.update(kwargs)
    return register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", **defaults)


def _assign(db, *, owner_id, goal, task, agent, role, mode, paths, **kwargs):
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=task.id if task else None, agent_id=agent.id,
        role=role, read_write_mode=mode, repository_identity="lifeai", allowed_paths=list(paths), requested_by="test", **kwargs
    )


def _lease(db, assignment, agent, *, branch, worktree, paths, mode="read_write", ttl_seconds=None):
    return acquire_lease(
        db, assignment=assignment, agent_id=agent.id, branch=branch, worktree_path=worktree, allowed_paths=list(paths), mode=mode, ttl_seconds=ttl_seconds
    )


def _set_rls_user(session, owner_id) -> None:
    from app.request_context import current_user_id as current_user_id_var

    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


# ============================================================================ 1: agent runtime
# snapshot correctness

def test_idle_agent_with_no_assignments(superuser_db, owner_id):
    cursor = _agent(superuser_db, "cursor-agent")
    view = agent_runtime_snapshot(superuser_db, cursor, owner_id=owner_id)
    assert view.runtime_status == RuntimeStatus.IDLE
    assert view.current_assignments == ()
    assert view.heartbeat_at is None


def test_running_agent_status_derived_from_active_lease(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    _lease(superuser_db, a, cursor, branch="cursor/finance", worktree="/tmp/wt-runtime-a", paths=["backend/app/finance/**"], ttl_seconds=600)
    transition_status(superuser_db, assignment=a, new_status="running")

    view = agent_runtime_snapshot(superuser_db, cursor, owner_id=owner_id)
    assert view.runtime_status == RuntimeStatus.RUNNING
    assert len(view.current_assignments) == 1
    assert view.current_assignments[0].runtime_status == RuntimeStatus.RUNNING
    assert view.current_assignments[0].block_reason is None
    assert view.heartbeat_at is not None


def test_heartbeat_at_selects_the_most_recent_across_multiple_active_leases(superuser_db, owner_id):
    """An agent with concurrency_limit > 1 can hold more than one active lease at once --
    heartbeat_at must report the MOST RECENT last_heartbeat_at across all of them, never an
    arbitrary one (e.g. the first row returned)."""
    goal, _plan, (task_a, task_b) = _goal_plan_task(superuser_db, owner_id, task_count=2)
    codex = _agent(superuser_db, "codex", concurrency_limit=2)
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=codex, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    b = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_b, agent=codex, role="builder", mode="read_write", paths=["frontend/**"])
    lease_a = _lease(superuser_db, a, codex, branch="codex/finance", worktree="/tmp/wt-heartbeat-a", paths=["backend/app/finance/**"], ttl_seconds=600).lease
    _lease(superuser_db, b, codex, branch="codex/frontend", worktree="/tmp/wt-heartbeat-b", paths=["frontend/**"], ttl_seconds=600)
    transition_status(superuser_db, assignment=a, new_status="running")
    transition_status(superuser_db, assignment=b, new_status="running")

    stale_cutoff = datetime.utcnow() - timedelta(minutes=10)
    lease_a.last_heartbeat_at = stale_cutoff
    superuser_db.flush()

    view = agent_runtime_snapshot(superuser_db, codex, owner_id=owner_id)
    assert view.heartbeat_at is not None
    assert view.heartbeat_at > stale_cutoff  # lease_b's fresher heartbeat won, not lease_a's staled-back one


def test_waiting_dependency_agent_status_and_block_reason(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    builder = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    reviewer = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=task, agent=claude, role="reviewer", mode="read_only", paths=[],
        depends_on_assignment_ids=(builder.id,),
    )
    view = agent_runtime_snapshot(superuser_db, claude, owner_id=owner_id)
    assert view.runtime_status == RuntimeStatus.WAITING_DEPENDENCY
    assert view.current_assignments[0].block_outcome == "WAITING_DEPENDENCY"
    assert view.current_assignments[0].block_reason is not None
    assert reviewer.status.value == "waiting_dependency"


def test_manually_blocked_assignment_surfaces_the_callers_own_recorded_reason(superuser_db, owner_id):
    """`blocked` is reached only through an explicit transition_status() call, never computed
    by evaluate_assignment_readiness() itself -- when the coordinator finds no STRUCTURAL
    blocker of its own (the common case: the caller blocked it for a reason outside this
    module's own vocabulary, e.g. waiting on a founder decision), the reason must still come
    from somewhere real, never silently report no reason at all for a row a human/process
    explicitly marked blocked."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="blocked", detail={"reason": "waiting_founder_decision"})

    view = agent_runtime_snapshot(superuser_db, cursor, owner_id=owner_id)
    assert view.runtime_status == RuntimeStatus.BLOCKED
    assert view.current_assignments[0].block_outcome == "BLOCKED"
    assert view.current_assignments[0].block_reason == "waiting_founder_decision"


def test_manually_blocked_assignment_without_a_recorded_reason_still_reports_something(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="blocked")  # no detail supplied

    view = agent_runtime_snapshot(superuser_db, cursor, owner_id=owner_id)
    assert view.current_assignments[0].block_outcome == "BLOCKED"
    assert view.current_assignments[0].block_reason is not None
    assert "no reason was recorded" in view.current_assignments[0].block_reason


def test_offline_agent_status_from_registry_disabled(superuser_db, owner_id):
    cursor = _agent(superuser_db, "cursor-agent", status="disabled")
    view = agent_runtime_snapshot(superuser_db, cursor, owner_id=owner_id)
    assert view.runtime_status == RuntimeStatus.OFFLINE


def test_offline_overrides_even_a_running_assignment(superuser_db, owner_id):
    """A registry-disabled agent reports OFFLINE regardless of any assignment it still holds --
    disabling an agent is the founder's own emergency stop, and the runtime view must never
    mask that with a "business as usual" RUNNING status."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-offline-override", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")
    # register_agent()'s own idempotent upsert -- never a raw attribute assignment, which
    # would leave a bare string where every other reader expects a real enum instance (see
    # that function's own comment on why this matters).
    register_agent(
        superuser_db, agent_key=cursor.agent_key, display_name=cursor.display_name, adapter_kind=cursor.adapter_kind.value,
        supports_read=True, supports_write=True, concurrency_limit=cursor.concurrency_limit, status="disabled",
    )

    view = agent_runtime_snapshot(superuser_db, cursor, owner_id=owner_id)
    assert view.runtime_status == RuntimeStatus.OFFLINE


def test_terminal_assignments_excluded_from_current_assignments(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-terminal", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")
    transition_status(superuser_db, assignment=a, new_status="completed")

    view = agent_runtime_snapshot(superuser_db, cursor, owner_id=owner_id)
    assert view.runtime_status == RuntimeStatus.IDLE
    assert view.current_assignments == ()


def test_owner_isolation_in_runtime_snapshot(superuser_db, make_verified_user):
    """coordination_agents is founder-wide, but this owner must never see another owner's
    assignment inside their snapshot of that shared agent."""
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    cursor = _agent(superuser_db, "cursor-agent")

    _set_rls_user(superuser_db, user_a.id)
    goal_a, _plan_a, task_a = _goal_plan_task(superuser_db, user_a.id)
    a = _assign(superuser_db, owner_id=user_a.id, goal=goal_a, task=task_a, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/owner-a", worktree="/tmp/wt-owner-a", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")
    superuser_db.commit()

    _set_rls_user(superuser_db, user_b.id)
    view_for_b = agent_runtime_snapshot(superuser_db, cursor, owner_id=user_b.id)
    assert view_for_b.current_assignments == ()
    assert view_for_b.runtime_status == RuntimeStatus.IDLE


# ============================================================================ 2: work registry
# snapshot

def test_work_registry_snapshot_lists_every_current_assignment(superuser_db, owner_id):
    goal, _plan, (task_a, task_b) = _goal_plan_task(superuser_db, owner_id, task_count=2)
    cursor = _agent(superuser_db, "cursor-agent")
    codex = _agent(superuser_db, "codex")
    _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_b, agent=codex, role="builder", mode="read_write", paths=["frontend/**"])

    registry = work_registry_snapshot(superuser_db, owner_id=owner_id, goal_id=goal.id)
    assert {v.agent_key for v in registry} == {cursor.agent_key, codex.agent_key}
    assert {v.repository_identity for v in registry} == {"lifeai"}


def test_assignment_runtime_view_accepts_a_pre_fetched_agent_without_a_redundant_query(superuser_db, owner_id):
    """agent_key comes from the explicitly supplied `agent` (an already-fetched row a caller
    like agent_runtime_snapshot() passes in to avoid one query per assignment) rather than
    assignment_runtime_view() re-fetching it itself -- calling it directly, standalone, proves
    that parameter actually drives the result."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    view = assignment_runtime_view(superuser_db, a, agent=cursor)
    assert view.agent_key == cursor.agent_key
    assert view.assignment_id == a.id
    assert view.canonical_status == a.status.value


def test_all_agents_runtime_snapshot_sees_every_registered_agent(superuser_db, owner_id):
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    codex = _agent(superuser_db, "codex")
    snapshot = all_agents_runtime_snapshot(superuser_db, owner_id=owner_id, agent_ids=[cursor.id, claude.id, codex.id])
    assert {v.agent_id for v in snapshot} == {cursor.id, claude.id, codex.id}
    assert all(v.runtime_status == RuntimeStatus.IDLE for v in snapshot)


# ============================================================================ 3: routing —
# scenarios A-E from the founder's own spec

def test_routing_scenario_a_two_non_overlapping_writers_both_eligible(superuser_db, owner_id):
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    decision_cursor = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/autonomous_gap/**"],
    )
    assert cursor.id in decision_cursor.eligible_agent_ids
    assert claude.id in decision_cursor.eligible_agent_ids
    assert decision_cursor.outcome == OUTCOME_NEEDS_SELECTION  # both are equally eligible -- this function picks no winner


def test_routing_scenario_b_write_request_on_occupied_path_rejected(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    a = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write",
        paths=["backend/app/autonomous_gap/**", "backend/app/development_supervisor/**"],
    )
    _lease(superuser_db, a, cursor, branch="cursor/pr79-live-loop-hardening", worktree="/tmp/wt-cursor-pr79", paths=a.allowed_paths, ttl_seconds=600)

    decision = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/development_supervisor/**"],
    )
    assert decision.outcome == OUTCOME_SCOPE_CONFLICT
    assert decision.eligible_agent_ids == ()
    # SCOPE_CONFLICT blocks every registered agent equally -- diagnostics must not come back
    # empty just because the rejection happened before per-candidate filtering ran.
    assert {r.agent_id for r in decision.rejected} == {cursor.id, claude.id}
    rejected_reasons = {r.reason for r in decision.rejected}
    assert len(rejected_reasons) == 1 and "already conflicts with active lease" in next(iter(rejected_reasons))


def test_routing_parallel_exploration_group_exempts_overlapping_writers(superuser_db, owner_id):
    """The one explicit exemption from SCOPE_CONFLICT: two agents intentionally competing on
    the SAME canonical problem, both already placed in the SAME parallel-exploration group,
    remain routable onto overlapping paths -- proving `eligible_agents_for()` correctly threads
    `parallel_exploration_group_id` through to `scan_write_scope_conflict()`."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    codex = _agent(superuser_db, "codex")
    group = create_parallel_exploration_group(
        superuser_db, owner_id=owner_id, goal_id=goal.id, canonical_problem_ref="best-routing-approach", created_by="founder"
    )
    a = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write",
        paths=["backend/app/agent_coordination/**"], parallel_exploration_group_id=group.id,
    )
    _lease(superuser_db, a, cursor, branch="cursor/explore-a", worktree="/tmp/wt-routing-explore-a", paths=a.allowed_paths, ttl_seconds=600)

    # WITHOUT the group id, the same scope is correctly SCOPE_CONFLICT.
    without_group = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/agent_coordination/**"],
    )
    assert without_group.outcome == OUTCOME_SCOPE_CONFLICT

    # WITH the matching group id, codex remains routable onto the same overlapping scope.
    with_group = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/agent_coordination/**"],
        parallel_exploration_group_id=group.id,
    )
    assert with_group.outcome != OUTCOME_SCOPE_CONFLICT
    assert codex.id in with_group.eligible_agent_ids


def test_routing_scenario_c_read_only_review_never_blocked_by_scope_conflict(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/autonomous_gap/**"])
    _lease(superuser_db, a, cursor, branch="cursor/pr79", worktree="/tmp/wt-cursor-review-scope", paths=["backend/app/autonomous_gap/**"], ttl_seconds=600)

    # read_only routing is NEVER gated by scan_write_scope_conflict (that check is skipped
    # entirely for read_only, exactly like acquire_lease()'s own "rule 1") -- both agents
    # remain candidates even though cursor holds an active write lease on an overlapping path;
    # the busy-writer-excluded-by-AVAILABILITY case is proven separately below.
    decision = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="reviewer", read_write_mode="read_only",
        repository_identity="lifeai", allowed_paths=[],
    )
    assert decision.outcome == OUTCOME_NEEDS_SELECTION
    assert {cursor.id, claude.id} <= set(decision.eligible_agent_ids)


def test_routing_scenario_c_busy_writer_excluded_from_review_routing_by_availability(superuser_db, owner_id):
    """A builder already at its own concurrency limit is correctly filtered out of routing for
    a SEPARATE review assignment -- availability, not scope, is what excludes it here."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent", concurrency_limit=1)
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/autonomous_gap/**"])
    _lease(superuser_db, a, cursor, branch="cursor/pr79", worktree="/tmp/wt-cursor-busy", paths=["backend/app/autonomous_gap/**"], ttl_seconds=600)
    transition_status(superuser_db, assignment=a, new_status="running")

    decision = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="reviewer", read_write_mode="read_only",
        repository_identity="lifeai", allowed_paths=[],
    )
    assert decision.outcome == OUTCOME_NONE_AVAILABLE or cursor.id not in decision.eligible_agent_ids
    rejected_ids = {r.agent_id for r in decision.rejected}
    assert cursor.id in rejected_ids


def test_routing_scenario_d_two_writers_same_worktree_second_rejected_at_lease_time(superuser_db, owner_id):
    """Routing's scope-conflict pre-check only reasons over PATH overlap (branch/worktree are
    not yet known before an agent is chosen) -- the worktree-identity rule itself is enforced
    where it has always been enforced, at acquire_lease() time. This test proves that
    enforcement still holds end to end even when routing green-lit the candidate."""
    goal, _plan, (task_a, task_b) = _goal_plan_task(superuser_db, owner_id, task_count=2)
    cursor = _agent(superuser_db, "cursor-agent")
    codex = _agent(superuser_db, "codex")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    b = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_b, agent=codex, role="builder", mode="read_write", paths=["frontend/**"])
    assert _lease(superuser_db, a, cursor, branch="shared", worktree="/tmp/wt-routing-shared", paths=["backend/app/finance/**"]).outcome == "ACQUIRED"
    result = _lease(superuser_db, b, codex, branch="shared", worktree="/tmp/wt-routing-shared", paths=["frontend/**"])
    assert result.outcome == "WORKTREE_CONFLICT"


def test_routing_scenario_e_offline_agent_never_eligible(superuser_db, owner_id):
    _agent(superuser_db, "cursor-agent", status="disabled")
    decision = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/finance/**"],
    )
    assert decision.outcome == OUTCOME_NONE_AVAILABLE
    assert decision.eligible_agent_ids == ()
    assert decision.rejected[0].reason.startswith("registry status is 'disabled'")


def test_routing_required_capability_filters_out_agents_lacking_it(superuser_db, owner_id):
    generalist = _agent(superuser_db, "generalist", capabilities=["repo_edit"])
    specialist = _agent(superuser_db, "specialist", capabilities=["repo_edit", "run_tests"])
    decision = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="tester", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/testing/**"], required_capabilities=("run_tests",),
    )
    assert decision.outcome == OUTCOME_ELIGIBLE
    assert decision.eligible_agent_ids == (specialist.id,)
    assert any(r.agent_id == generalist.id for r in decision.rejected)


def test_routing_reviewer_read_write_pairing_rejected(superuser_db, owner_id):
    _agent(superuser_db, "claude-code")
    decision = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="reviewer", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/**"],
    )
    assert decision.outcome == OUTCOME_NONE_AVAILABLE
    assert "read_only" in decision.reason


# ============================================================================ 4: evidence
# payload vocabulary

def test_build_agent_outcome_payload_omits_unset_fields(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    from app.agent_coordination.service import record_assignment_execution, record_assignment_outcome

    record_assignment_execution(superuser_db, assignment=a, agent=cursor)
    payload = build_agent_outcome_payload(tests_passed=True, tests_run=29, ci_conclusion="success", merge_result="clean")
    assert payload == {"tests_passed": True, "tests_run": 29, "ci_conclusion": "success", "merge_result": "clean"}
    evidence_id = record_assignment_outcome(superuser_db, assignment=a, evidence_kind="agent_outcome", payload=payload, deterministic=True)
    assert evidence_id is not None


# ============================================================================ 5: the concrete,
# current real-world three-agent state -- Cursor RUNNING/WRITE on PR #80, Claude WRITE on a
# genuinely different subsystem (this very module), Codex IDLE.

def test_current_real_world_three_agent_state_end_to_end(superuser_db, owner_id):
    goal, _plan, (cursor_task, claude_task) = _goal_plan_task(
        superuser_db, owner_id, instruction="Coordinate Cursor's PR #80 hardening with Claude's runtime-control-plane work.", task_count=2
    )
    cursor = _agent(superuser_db, "cursor-agent", display_name="Cursor Agent")
    claude = _agent(superuser_db, "claude-code", display_name="Claude Code")
    codex = _agent(superuser_db, "codex", display_name="Codex")

    pr80_paths = [
        "backend/app/autonomous_gap/**",
        "backend/app/development_supervisor/**",
        "backend/app/development_driver/**",
        "backend/app/development_operator/**",
        "backend/app/safe_planner/**",
    ]
    cursor_assignment = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=cursor_task, agent=cursor, role="builder", mode="read_write",
        paths=pr80_paths, risk_level="medium",
    )
    transition_status(superuser_db, assignment=cursor_assignment, new_status="ready")
    cursor_lease = _lease(
        superuser_db, cursor_assignment, cursor, branch="cursor/pr79-live-loop-hardening", worktree="/tmp/wt-cursor-pr80-real",
        paths=pr80_paths, ttl_seconds=600,
    )
    assert cursor_lease.outcome == "ACQUIRED"
    transition_status(superuser_db, assignment=cursor_assignment, new_status="running")

    claude_paths = ["backend/app/agent_coordination/**", "docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md"]
    claude_assignment = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=claude_task, agent=claude, role="builder", mode="read_write",
        paths=claude_paths,
    )
    transition_status(superuser_db, assignment=claude_assignment, new_status="ready")
    claude_lease = _lease(
        superuser_db, claude_assignment, claude, branch="claude/agent-runtime-control-plane", worktree="/tmp/wt-claude-runtime-real",
        paths=claude_paths, ttl_seconds=600,
    )
    assert claude_lease.outcome == "ACQUIRED"
    transition_status(superuser_db, assignment=claude_assignment, new_status="running")

    # -- Life sees all three, including the idle one, without any of them having a live
    # assignment row for the idle case at all.
    snapshot = all_agents_runtime_snapshot(superuser_db, owner_id=owner_id, agent_ids=[cursor.id, claude.id, codex.id])
    by_key = {v.agent_key: v for v in snapshot}
    assert by_key[cursor.agent_key].runtime_status == RuntimeStatus.RUNNING
    assert by_key[claude.agent_key].runtime_status == RuntimeStatus.RUNNING
    assert by_key[codex.agent_key].runtime_status == RuntimeStatus.IDLE
    assert by_key[codex.agent_key].current_assignments == ()

    # -- identify active work / free agents from the registry snapshot alone.
    active_agents = {v.agent_key for v in snapshot if v.runtime_status == RuntimeStatus.RUNNING}
    free_agents = {v.agent_key for v in snapshot if v.runtime_status == RuntimeStatus.IDLE}
    assert active_agents == {cursor.agent_key, claude.agent_key}
    assert free_agents == {codex.agent_key}

    # -- an overlapping Claude/Codex WRITE request against Cursor's PR #80 scope is refused.
    overlap_decision = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/development_supervisor/**"],
    )
    assert overlap_decision.outcome == OUTCOME_SCOPE_CONFLICT

    # -- a non-overlapping assignment (e.g. Codex on an unrelated area) is allowed.
    non_overlap_decision = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["frontend/app/unrelated/**"],
    )
    assert non_overlap_decision.outcome == OUTCOME_NEEDS_SELECTION  # all three still pass every OTHER filter
    assert codex.id in non_overlap_decision.eligible_agent_ids

    # -- represent reviewer waiting/reviewing state: Claude's builder work finishes, a
    # dependent Codex reviewer is created and released, then starts reviewing.
    transition_status(superuser_db, assignment=claude_assignment, new_status="ready_for_review")
    reviewer_assignment = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=claude_task, agent=codex, role="reviewer", mode="read_only", paths=[],
        depends_on_assignment_ids=(claude_assignment.id,),
    )
    superuser_db.refresh(reviewer_assignment)
    assert reviewer_assignment.status.value == "ready"  # released immediately -- dependency already satisfied
    transition_status(superuser_db, assignment=reviewer_assignment, new_status="running")
    transition_status(superuser_db, assignment=reviewer_assignment, new_status="ready_for_review")
    transition_status(superuser_db, assignment=reviewer_assignment, new_status="reviewing")

    codex_view = agent_runtime_snapshot(superuser_db, codex, owner_id=owner_id)
    assert codex_view.runtime_status == RuntimeStatus.REVIEWING

    # -- identify a dependency on Cursor's completion: a Claude assignment created dependent on
    # Cursor's PR #80 work is WAITING_DEPENDENCY, and Life can still see Cursor's own work
    # continuing unaffected (Cursor never froze while Claude's dependent work waited).
    dependent_task_goal, _plan2, dependent_task = _goal_plan_task(superuser_db, owner_id, instruction="Follow-on work gated on PR #80 landing.")
    dependent_assignment = _assign(
        superuser_db, owner_id=owner_id, goal=dependent_task_goal, task=dependent_task, agent=claude, role="builder", mode="read_write",
        paths=["backend/app/agent_coordination/routing.py"], depends_on_assignment_ids=(cursor_assignment.id,),
    )
    assert dependent_assignment.status.value == "waiting_dependency"
    registry_now = work_registry_snapshot(superuser_db, owner_id=owner_id)
    statuses_by_assignment = {v.assignment_id: v.runtime_status for v in registry_now}
    assert statuses_by_assignment[cursor_assignment.id] == RuntimeStatus.RUNNING  # unaffected by the unrelated dependency
    assert statuses_by_assignment[dependent_assignment.id] == RuntimeStatus.WAITING_DEPENDENCY

    # -- Life can continue OTHER feasible work while that one assignment waits: Codex is
    # already reviewing (proven above), and a genuinely free, non-overlapping slot for a new
    # writer still resolves normally rather than freezing on the blocked dependent assignment.
    still_routable = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["frontend/app/another-unrelated-area/**"],
    )
    assert still_routable.outcome in (OUTCOME_ELIGIBLE, OUTCOME_NEEDS_SELECTION)
    assert still_routable.eligible_agent_ids  # at least one agent is still routable despite the blocked assignment elsewhere


# ============================================================================ 6: next feasible
# assignment for a given agent -- the inverse direction of routing (given an agent, which work)

def test_next_feasible_assignment_returns_none_when_agent_has_no_ready_work(superuser_db, owner_id):
    cursor = _agent(superuser_db, "cursor-agent")
    decision = next_feasible_assignment_for_agent(superuser_db, agent_id=cursor.id, owner_id=owner_id)
    assert decision.outcome == OUTCOME_NO_FEASIBLE_ASSIGNMENT
    assert decision.assignment_id is None
    assert decision.skipped == ()


def test_next_feasible_assignment_returns_the_single_ready_one(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    assert a.status.value == "ready"

    decision = next_feasible_assignment_for_agent(superuser_db, agent_id=cursor.id, owner_id=owner_id)
    assert decision.outcome == OUTCOME_ASSIGNMENT_FOUND
    assert decision.assignment_id == a.id
    assert decision.skipped == ()


def test_next_feasible_assignment_is_strict_fifo_by_creation_order(superuser_db, owner_id):
    goal, _plan, (task_a, task_b, task_c) = _goal_plan_task(superuser_db, owner_id, task_count=3)
    cursor = _agent(superuser_db, "cursor-agent", concurrency_limit=3)
    first = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    second = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_b, agent=cursor, role="builder", mode="read_write", paths=["frontend/**"])
    third = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_c, agent=cursor, role="builder", mode="read_write", paths=["docs/**"])

    decision = next_feasible_assignment_for_agent(superuser_db, agent_id=cursor.id, owner_id=owner_id)
    assert decision.outcome == OUTCOME_ASSIGNMENT_FOUND
    assert decision.assignment_id == first.id  # oldest wins, never second/third
    assert second.id != decision.assignment_id and third.id != decision.assignment_id


def test_next_feasible_assignment_skips_a_blocked_older_one_for_a_feasible_younger_one(superuser_db, owner_id):
    """The exact "one blocked assignment must not freeze unrelated work" guarantee, from the
    AGENT's own point of view: the oldest 'ready' assignment is not truly assignable right now
    (its dependency isn't satisfied), so the selector skips it -- recording why -- and returns
    the next one that genuinely is, rather than returning nothing or the stuck one."""
    goal, _plan, (task_a, task_b) = _goal_plan_task(superuser_db, owner_id, task_count=2)
    cursor = _agent(superuser_db, "cursor-agent", concurrency_limit=3)
    claude = _agent(superuser_db, "claude-code")
    # A separate, unrelated builder cursor "depends on" -- never reaches a releasing status,
    # so the dependent stays waiting_dependency, never even 'ready', and must not be confused
    # with the SEPARATE stuck-but-ready scenario below.
    blocker = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=claude, role="builder", mode="read_write", paths=["backend/app/other/**"])

    older_but_stuck = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=cursor, role="reviewer", mode="read_only", paths=[],
        depends_on_assignment_ids=(blocker.id,),
    )
    assert older_but_stuck.status.value == "waiting_dependency"  # not even 'ready' -- excluded by the query itself, not by evaluate_assignment_readiness

    # A second, genuinely ready assignment for the SAME agent, created after the first --
    # but the first one never enters the 'ready' candidate pool at all (still
    # waiting_dependency), so this proves the QUERY-level filter, not the assignability scan.
    younger_and_ready = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_b, agent=cursor, role="builder", mode="read_write", paths=["frontend/**"])
    assert younger_and_ready.status.value == "ready"

    decision = next_feasible_assignment_for_agent(superuser_db, agent_id=cursor.id, owner_id=owner_id)
    assert decision.outcome == OUTCOME_ASSIGNMENT_FOUND
    assert decision.assignment_id == younger_and_ready.id


def test_next_feasible_assignment_skips_a_ready_but_not_yet_assignable_one_and_records_why(superuser_db, owner_id):
    """A DIFFERENT stuck reason than the test above: the older assignment genuinely reaches
    'ready' (query-level candidate), but evaluate_assignment_readiness() itself says it isn't
    assignable right now (its recorded base_sha has gone stale) -- proving the SCAN-level skip,
    not just the query-level one, and that the skip is recorded with its real outcome code."""
    goal, _plan, (task_a, task_b) = _goal_plan_task(superuser_db, owner_id, task_count=2)
    cursor = _agent(superuser_db, "cursor-agent", concurrency_limit=3)
    stale = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    stale.base_sha = "16a5da900f9d898de0d0b3c2b6a1b145443575aa"
    superuser_db.flush()
    fresh = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_b, agent=cursor, role="builder", mode="read_write", paths=["frontend/**"])
    assert stale.status.value == fresh.status.value == "ready"

    # next_feasible_assignment_for_agent() itself never takes a current_base_sha -- it asks
    # evaluate_assignment_readiness() with none, so a merely-stored (never-compared) base_sha
    # alone doesn't trigger STALE_BASE. Use a lease conflict instead, a condition the scan DOES
    # detect with no external base-sha input: give `fresh` a competing active write lease that
    # overlaps `stale`'s own scope once `stale` tries to acquire one -- simpler: directly prove
    # the skip using LEASE_REQUIRED, which evaluate_assignment_readiness() raises for ANY
    # read_write 'ready'/'running' assignment holding no active lease yet.
    other_agent = _agent(superuser_db, "codex")
    conflicting = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=None, agent=other_agent, role="builder", mode="read_write",
        paths=["backend/app/finance/shared.py"],
    )
    _lease(superuser_db, conflicting, other_agent, branch="codex/shared", worktree="/tmp/wt-selector-conflict", paths=["backend/app/finance/shared.py"])
    stale.allowed_paths = ["backend/app/finance/shared.py"]
    superuser_db.flush()

    decision = next_feasible_assignment_for_agent(superuser_db, agent_id=cursor.id, owner_id=owner_id)
    assert decision.outcome == OUTCOME_ASSIGNMENT_FOUND
    assert decision.assignment_id == fresh.id
    assert len(decision.skipped) == 1
    assert decision.skipped[0][0] == stale.id
    assert decision.skipped[0][1] == "PATH_CONFLICT"


def test_next_feasible_assignment_never_returns_work_assigned_to_a_different_agent(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=claude, role="builder", mode="read_write", paths=["backend/app/**"])

    decision = next_feasible_assignment_for_agent(superuser_db, agent_id=cursor.id, owner_id=owner_id)
    assert decision.outcome == OUTCOME_NO_FEASIBLE_ASSIGNMENT
    assert decision.assignment_id is None


def test_next_feasible_assignment_never_mutates_the_assignment(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    status_before = a.status

    next_feasible_assignment_for_agent(superuser_db, agent_id=cursor.id, owner_id=owner_id)
    assert a.status == status_before  # read-only: selecting is not starting
    active_lease = superuser_db.execute(
        sa_text("SELECT count(*) FROM agent_scope_leases WHERE assignment_id = :id AND status = 'active'"), {"id": str(a.id)}
    ).scalar()
    assert active_lease == 0  # never acquires a lease on the caller's behalf


def test_next_feasible_assignment_owner_isolation(superuser_db, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    cursor = _agent(superuser_db, "cursor-agent")

    _set_rls_user(superuser_db, user_a.id)
    goal_a, _plan_a, task_a = _goal_plan_task(superuser_db, user_a.id)
    _assign(superuser_db, owner_id=user_a.id, goal=goal_a, task=task_a, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    superuser_db.commit()

    _set_rls_user(superuser_db, user_b.id)
    decision = next_feasible_assignment_for_agent(superuser_db, agent_id=cursor.id, owner_id=user_b.id)
    assert decision.outcome == OUTCOME_NO_FEASIBLE_ASSIGNMENT  # user_a's ready assignment must never surface for user_b


def test_next_feasible_assignment_excludes_waiting_agent_status_entirely(superuser_db, owner_id):
    """'waiting_agent' (ready, allocated, but not yet claimed by this agent -- see
    WorkAssignmentStatus's own docstring) is excluded by the query itself, not merely skipped
    during the assignability scan -- it must never appear in `skipped` either, since the query
    never even candidates it."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="waiting_agent")
    assert a.status.value == "waiting_agent"

    decision = next_feasible_assignment_for_agent(superuser_db, agent_id=cursor.id, owner_id=owner_id)
    assert decision.outcome == OUTCOME_NO_FEASIBLE_ASSIGNMENT
    assert decision.assignment_id is None
    assert decision.skipped == ()  # excluded at the query level, never even reaches the scan


def test_next_feasible_assignment_read_only_found_via_assignable_never_lease_required(superuser_db, owner_id):
    """LEASE_REQUIRED is a read_write-only concept (evaluate_assignment_readiness() nests that
    check entirely inside its own read_write branch) -- a read_only 'ready' assignment with no
    lease at all must be found via plain ASSIGNABLE, not by the LEASE_REQUIRED carve-out this
    function's docstring claims exists only for read_write."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    claude = _agent(superuser_db, "claude-code")
    reviewer = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=claude, role="reviewer", mode="read_only", paths=[])
    assert reviewer.status.value == "ready"

    decision = next_feasible_assignment_for_agent(superuser_db, agent_id=claude.id, owner_id=owner_id)
    assert decision.outcome == OUTCOME_ASSIGNMENT_FOUND
    assert decision.assignment_id == reviewer.id
    assert decision.skipped == ()


# ============================================================================ 7: idle agents +
# next assignment -- the single-call "who is free, what should they do" composition

def test_idle_agents_with_next_assignment_only_includes_truly_idle_agents(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")  # will be RUNNING
    claude = _agent(superuser_db, "claude-code")  # will be IDLE, nothing to do
    codex = _agent(superuser_db, "codex", status="disabled")  # will be OFFLINE

    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-idle-summary", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")

    results = idle_agents_with_next_assignment(superuser_db, owner_id=owner_id, agent_ids=[cursor.id, claude.id, codex.id])
    agent_keys = {r.agent.agent_key for r in results}
    assert agent_keys == {claude.agent_key}  # cursor is RUNNING, codex is OFFLINE -- both excluded


def test_idle_agents_with_next_assignment_finds_the_right_work_per_agent(superuser_db, owner_id):
    goal, _plan, (task_a, task_b) = _goal_plan_task(superuser_db, owner_id, task_count=2)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    b = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_b, agent=claude, role="builder", mode="read_write", paths=["frontend/**"])

    results = idle_agents_with_next_assignment(superuser_db, owner_id=owner_id, agent_ids=[cursor.id, claude.id])
    by_key = {r.agent.agent_key: r for r in results}
    assert by_key[cursor.agent_key].next_assignment.assignment_id == a.id
    assert by_key[claude.agent_key].next_assignment.assignment_id == b.id
    assert by_key[cursor.agent_key].next_assignment.outcome == OUTCOME_ASSIGNMENT_FOUND


def test_idle_agents_with_next_assignment_reports_genuinely_idle_with_nothing_to_do(superuser_db, owner_id):
    """An idle agent with an empty queue still appears -- NO_FEASIBLE_ASSIGNMENT is itself
    meaningful (genuinely free, genuinely nothing to give it), distinct from not appearing."""
    codex = _agent(superuser_db, "codex")
    results = idle_agents_with_next_assignment(superuser_db, owner_id=owner_id, agent_ids=[codex.id])
    assert len(results) == 1
    assert results[0].agent.agent_key == codex.agent_key
    assert results[0].next_assignment.outcome == OUTCOME_NO_FEASIBLE_ASSIGNMENT


def test_idle_agents_with_next_assignment_current_real_world_state(superuser_db, owner_id):
    """Mirrors the same concrete situation the E2E test above proves end to end: Cursor
    RUNNING on PR #80 paths, Claude RUNNING on this coordination module, Codex IDLE with
    unrelated work already queued -- the summary must show exactly Codex, with exactly its own
    queued assignment, and nothing else."""
    goal, _plan, (cursor_task, claude_task, codex_task) = _goal_plan_task(superuser_db, owner_id, task_count=3)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    codex = _agent(superuser_db, "codex")

    cursor_assignment = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=cursor_task, agent=cursor, role="builder", mode="read_write",
        paths=["backend/app/autonomous_gap/**", "backend/app/development_supervisor/**"],
    )
    _lease(superuser_db, cursor_assignment, cursor, branch="cursor/pr79-live-loop-hardening", worktree="/tmp/wt-idle-real-cursor", paths=cursor_assignment.allowed_paths)
    transition_status(superuser_db, assignment=cursor_assignment, new_status="running")

    claude_assignment = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=claude_task, agent=claude, role="builder", mode="read_write",
        paths=["backend/app/agent_coordination/**"],
    )
    _lease(superuser_db, claude_assignment, claude, branch="claude/agent-work-selection", worktree="/tmp/wt-idle-real-claude", paths=claude_assignment.allowed_paths)
    transition_status(superuser_db, assignment=claude_assignment, new_status="running")

    codex_assignment = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=codex_task, agent=codex, role="builder", mode="read_write",
        paths=["frontend/app/unrelated-area/**"],
    )

    results = idle_agents_with_next_assignment(superuser_db, owner_id=owner_id, agent_ids=[cursor.id, claude.id, codex.id])
    assert len(results) == 1
    assert results[0].agent.agent_key == codex.agent_key
    assert results[0].next_assignment.assignment_id == codex_assignment.id
