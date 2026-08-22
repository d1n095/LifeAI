"""Multi-Agent Work Coordination foundation -- proves deterministic conflict prevention,
lease fencing/takeover, dependency handoff, parallel exploration, and capability-evidence
integration against a real Postgres database (never mocked). See
docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md for the architecture these tests hold to.

The concrete real-world scenario this foundation was built to represent -- Cursor hardening
PR #79's live-loop areas while Claude reviews read-only, then a second Claude BUILDER request
into the same scope is rejected, then Codex works an unrelated area concurrently, then Cursor
and Codex intentionally compete on the same canonical problem in isolated worktrees -- is
proved end to end in `test_pr79_hardening_scenario_end_to_end` below."""

import hashlib
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

from app.agent_coordination.service import (
    DuplicateWorkError,
    LeaseFencingError,
    LeaseStillLiveError,
    ScopeExpansionError,
    acquire_lease,
    agent_availability,
    count_active_assignments,
    create_parallel_exploration_group,
    create_work_assignment,
    evaluate_assignment_readiness,
    is_lease_stale,
    paths_conflict,
    paths_conflict_any,
    record_assignment_execution,
    record_assignment_outcome,
    register_agent,
    renew_lease,
    take_over_lease,
    transition_status,
)
from app.agent_coordination.service import CoordinationError, OUTCOME_ASSIGNABLE, OUTCOME_AGENT_UNAVAILABLE
from app.agent_coordination import adapters as adapters_module
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.agent_coordination import AgentWorkAssignment, CoordinationAgent
from app.models.mainai_execution import MainAITask
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure as test_partial_plan_insertion.py's/
    test_autonomous_gap_child_task.py's own identical fixture -- migration 0046's
    privilege lockdown (agent_work_assignment_events' REVOKE + the erasure function's GRANT)
    lives in the SAME apply_mainai_execution_privileges() those modules already prime."""
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


def _goal_plan_task(db, owner_id, *, instruction="Harden PR #79's live-loop wiring, safely.", approval_policy="autonomous_development_work", task_count=1):
    goal = create_goal(db, owner_id=owner_id, title="PR79 hardening", original_instruction=instruction, created_by="founder", approval_policy=approval_policy)
    plan = create_plan(
        db, goal=goal, rationale="coordination test",
        tasks=[PlannedTaskSpec(description=f"Task {i}", task_type="repo_edit") for i in range(task_count)],
        created_by="founder",
    )
    db.commit()
    tasks = db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).all()
    if task_count == 1:
        return goal, plan, tasks[0]
    return goal, plan, tasks


def _agent(db, key, **kwargs):
    """CoordinationAgent is deliberately FOUNDER-WIDE (see its own model docstring) -- a
    concurrency_limit is a real GLOBAL limit on that one agent identity, not scoped per owner.
    Each call here therefore gets its OWN unique agent_key (a per-test suffix) so unrelated
    tests never accidentally share -- and let their assignment counts leak across -- the same
    registry row; a test that specifically wants a STABLE, repeatable key (e.g. proving
    register_agent()'s own upsert idempotency) passes `key` already suffixed itself and calls
    register_agent() directly instead of this helper."""

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


# ============================================================================ pure path-prefix
# primitive (19, 20)

def test_path_prefix_collision_correctly_detected():
    assert paths_conflict("backend/app/finance/**", "frontend/**") is False
    assert paths_conflict_any(["backend/app/finance/**"], ["frontend/**"]) is False
    assert paths_conflict_any(["backend/app/**"], ["backend/app/finance/**"]) is True
    # boundary-aware: a raw string prefix must NOT be mistaken for a directory ancestor
    assert paths_conflict("backend/app/dev", "backend/app/development_x/foo.py") is False


def test_exact_file_collision_detected():
    assert paths_conflict("backend/app/development_supervisor/**", "backend/app/development_supervisor/service.py") is True
    assert paths_conflict("backend/app/development_supervisor/service.py", "backend/app/development_supervisor/service.py") is True
    assert paths_conflict("backend/app/development_supervisor/service.py", "backend/app/development_supervisor/other.py") is False


# ============================================================================ 1-4: lease
# acquisition conflict rules

def test_non_overlapping_writers_allowed(superuser_db, owner_id):
    goal, _plan, (task_a, task_b) = _goal_plan_task(superuser_db, owner_id, task_count=2)
    cursor = _agent(superuser_db, "cursor-agent")
    codex = _agent(superuser_db, "codex")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    b = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_b, agent=codex, role="builder", mode="read_write", paths=["frontend/**"])
    result_a = _lease(superuser_db, a, cursor, branch="cursor/finance", worktree="/tmp/wt-a", paths=["backend/app/finance/**"])
    result_b = _lease(superuser_db, b, codex, branch="codex/frontend", worktree="/tmp/wt-b", paths=["frontend/**"])
    assert result_a.outcome == "ACQUIRED"
    assert result_b.outcome == "ACQUIRED"


def test_overlapping_writers_rejected(superuser_db, owner_id):
    goal, _plan, (task_a, task_b) = _goal_plan_task(superuser_db, owner_id, task_count=2)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=cursor, role="builder", mode="read_write", paths=["backend/app/development_supervisor/**"])
    b = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_b, agent=claude, role="builder", mode="read_write", paths=["backend/app/development_supervisor/service.py"])
    assert _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-a2", paths=["backend/app/development_supervisor/**"]).outcome == "ACQUIRED"
    result_b = _lease(superuser_db, b, claude, branch="claude/y", worktree="/tmp/wt-b2", paths=["backend/app/development_supervisor/service.py"])
    assert result_b.outcome == "PATH_CONFLICT"
    assert result_b.lease is None


def test_read_only_reviewer_may_overlap_writer(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    builder = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    reviewer = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=claude, role="reviewer", mode="read_only", paths=[])
    assert _lease(superuser_db, builder, cursor, branch="cursor/z", worktree="/tmp/wt-c", paths=["backend/app/**"]).outcome == "ACQUIRED"
    result = _lease(superuser_db, reviewer, claude, branch="claude/review", worktree="/tmp/wt-review", paths=[], mode="read_only")
    assert result.outcome == "ACQUIRED"


def test_same_physical_worktree_cannot_have_two_writers(superuser_db, owner_id):
    goal, _plan, (task_a, task_b) = _goal_plan_task(superuser_db, owner_id, task_count=2)
    cursor = _agent(superuser_db, "cursor-agent")
    codex = _agent(superuser_db, "codex")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    b = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_b, agent=codex, role="builder", mode="read_write", paths=["frontend/**"])
    assert _lease(superuser_db, a, cursor, branch="shared", worktree="/tmp/shared-wt", paths=["backend/app/finance/**"]).outcome == "ACQUIRED"
    result = _lease(superuser_db, b, codex, branch="shared", worktree="/tmp/shared-wt", paths=["frontend/**"])
    assert result.outcome == "WORKTREE_CONFLICT"


# ============================================================================ 5-6: staleness /
# takeover fencing

def test_stale_lease_rejected(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    result = _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-stale", paths=["backend/app/**"], ttl_seconds=1)
    lease = result.lease
    lease.expires_at = datetime.utcnow() - timedelta(seconds=1)
    superuser_db.flush()
    assert is_lease_stale(lease) is True
    with pytest.raises(LeaseFencingError):
        renew_lease(superuser_db, lease_id=lease.id, owner_id=owner_id, agent_id=cursor.id, lease_generation=lease.lease_generation, ttl_seconds=60)


def test_lease_takeover_fences_old_worker(superuser_db, owner_id):
    goal, _plan, (task_a, task_b) = _goal_plan_task(superuser_db, owner_id, task_count=2)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_a, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    result = _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-take", paths=["backend/app/**"], ttl_seconds=1)
    lease = result.lease
    old_generation = lease.lease_generation
    lease.expires_at = datetime.utcnow() - timedelta(seconds=1)
    superuser_db.flush()

    with pytest.raises(LeaseStillLiveError):
        # a NOT-yet-stale lease cannot be taken over -- prove this on a second, live lease.
        b = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task_b, agent=claude, role="builder", mode="read_write", paths=["frontend/**"])
        live = _lease(superuser_db, b, claude, branch="claude/x", worktree="/tmp/wt-live", paths=["frontend/**"], ttl_seconds=600).lease
        take_over_lease(superuser_db, lease_id=live.id, owner_id=owner_id, new_agent_id=cursor.id, ttl_seconds=60)

    taken = take_over_lease(superuser_db, lease_id=lease.id, owner_id=owner_id, new_agent_id=claude.id, ttl_seconds=60)
    assert taken.lease_generation == old_generation + 1
    assert taken.agent_id == claude.id

    # the OLD worker's reference to the pre-takeover generation is now fenced out.
    with pytest.raises(LeaseFencingError):
        renew_lease(superuser_db, lease_id=lease.id, owner_id=owner_id, agent_id=cursor.id, lease_generation=old_generation, ttl_seconds=60)
    # the new holder, presenting the correct fenced generation, succeeds.
    renewed = renew_lease(superuser_db, lease_id=lease.id, owner_id=owner_id, agent_id=claude.id, lease_generation=old_generation + 1, ttl_seconds=60)
    assert renewed.id == lease.id


def test_takeover_concurrency_race(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    codex = _agent(superuser_db, "codex")
    claude = _agent(superuser_db, "claude-code")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    lease = _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-race", paths=["backend/app/**"], ttl_seconds=1).lease
    generation_before = lease.lease_generation
    lease.expires_at = datetime.utcnow() - timedelta(seconds=1)
    superuser_db.flush()

    winner = take_over_lease(superuser_db, lease_id=lease.id, owner_id=owner_id, new_agent_id=codex.id, expected_generation=generation_before, ttl_seconds=60)
    assert winner.lease_generation == generation_before + 1

    with pytest.raises(LeaseFencingError):
        # the second racer's expectation (the PRE-takeover generation) no longer matches --
        # rejected, not silently re-applied.
        take_over_lease(superuser_db, lease_id=lease.id, owner_id=owner_id, new_agent_id=claude.id, expected_generation=generation_before, ttl_seconds=60)


def test_scope_expansion_rejected(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-expand", paths=["backend/app/finance/**"])
    with pytest.raises(ScopeExpansionError):
        _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-expand", paths=["backend/app/finance/**", "backend/app/development_supervisor/**"])


def test_lease_replay_idempotency(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    first = _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-replay", paths=["backend/app/finance/**"])
    second = _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-replay", paths=["backend/app/finance/**"])
    assert first.lease.id == second.lease.id
    assert first.lease.lease_generation == second.lease.lease_generation == 1


# ============================================================================ 8-9: dependency
# wait / release

def test_dependency_wait(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    builder = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    reviewer = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=task, agent=claude, role="reviewer", mode="read_only", paths=[],
        depends_on_assignment_ids=(builder.id,),
    )
    superuser_db.refresh(reviewer)
    assert reviewer.status.value == "waiting_dependency"
    decision = evaluate_assignment_readiness(superuser_db, assignment=reviewer)
    assert decision.outcome == "WAITING_DEPENDENCY"


def test_dependency_completion_releases_reviewer(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    builder = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    reviewer = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=task, agent=claude, role="reviewer", mode="read_only", paths=[],
        depends_on_assignment_ids=(builder.id,),
    )
    assert reviewer.status.value == "waiting_dependency"
    transition_status(superuser_db, assignment=builder, new_status="running")
    transition_status(superuser_db, assignment=builder, new_status="ready_for_review")
    superuser_db.refresh(reviewer)
    assert reviewer.status.value == "ready"


# ============================================================================ 10-12: duplicate
# work / parallel exploration

def test_duplicate_canonical_job_rejected_normally(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    with pytest.raises(DuplicateWorkError):
        _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=claude, role="builder", mode="read_write", paths=["backend/app/**"])


def test_duplicate_canonical_job_allowed_in_parallel_exploration(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    codex = _agent(superuser_db, "codex")
    group = create_parallel_exploration_group(superuser_db, owner_id=owner_id, goal_id=goal.id, canonical_problem_ref="fix-flaky-lease-test", created_by="founder")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"], parallel_exploration_group_id=group.id)
    b = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=codex, role="builder", mode="read_write", paths=["backend/app/**"], parallel_exploration_group_id=group.id)
    assert a.id != b.id
    result_a = _lease(superuser_db, a, cursor, branch="cursor/explore-a", worktree="/tmp/wt-explore-a", paths=["backend/app/**"])
    result_b = _lease(superuser_db, b, codex, branch="codex/explore-b", worktree="/tmp/wt-explore-b", paths=["backend/app/**"])
    assert result_a.outcome == "ACQUIRED"
    assert result_b.outcome == "ACQUIRED"


def test_parallel_exploration_requires_isolated_branches_worktrees(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    codex = _agent(superuser_db, "codex")
    group = create_parallel_exploration_group(superuser_db, owner_id=owner_id, goal_id=goal.id, canonical_problem_ref="fix-flaky-lease-test", created_by="founder")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"], parallel_exploration_group_id=group.id)
    b = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=codex, role="builder", mode="read_write", paths=["backend/app/**"], parallel_exploration_group_id=group.id)
    assert _lease(superuser_db, a, cursor, branch="shared-explore", worktree="/tmp/wt-shared-explore", paths=["backend/app/**"]).outcome == "ACQUIRED"
    # SAME physical worktree, even inside the SAME exploration group, is still rejected --
    # PARALLEL_EXPLORATION exempts path overlap, never worktree identity.
    result = _lease(superuser_db, b, codex, branch="shared-explore", worktree="/tmp/wt-shared-explore", paths=["backend/app/**"])
    assert result.outcome == "WORKTREE_CONFLICT"


# ============================================================================ 13-14: base/
# authority staleness

def test_stale_base_sha_detected(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    a.base_sha = "16a5da900f9d898de0d0b3c2b6a1b145443575aa"
    superuser_db.flush()
    decision = evaluate_assignment_readiness(superuser_db, assignment=a, current_base_sha="deadbeef00000000000000000000000000000000")
    assert decision.outcome == "STALE_BASE"
    same = evaluate_assignment_readiness(superuser_db, assignment=a, current_base_sha=a.base_sha)
    assert same.outcome != "STALE_BASE"


def test_superseded_parent_rejects_new_work(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    original_hash = hashlib.sha256(goal.original_instruction.encode()).hexdigest()
    a = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"],
        provenance={"authorized_instruction_sha256": original_hash},
    )
    assert evaluate_assignment_readiness(superuser_db, assignment=a).outcome != "AUTHORITY_CHANGED"
    goal.original_instruction = "A founder correction supersedes the original direction."
    superuser_db.flush()
    decision = evaluate_assignment_readiness(superuser_db, assignment=a)
    assert decision.outcome == "AUTHORITY_CHANGED"


# ============================================================================ 15-16: agent
# availability / concurrency

def test_agent_unavailable(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent", status="disabled")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    decision = evaluate_assignment_readiness(superuser_db, assignment=a)
    assert decision.outcome == OUTCOME_AGENT_UNAVAILABLE


def test_concurrency_limit_enforced(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent", concurrency_limit=1)
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    # a merely READY (queued, not yet claimed) assignment does not yet consume a concurrency
    # slot -- create_work_assignment() already advances a dependency-free assignment straight
    # to READY, so this proves READY specifically, not just the initial PLANNED state.
    assert a.status.value == "ready"
    available_before, _ = agent_availability(superuser_db, agent_id=cursor.id)
    assert available_before is True
    transition_status(superuser_db, assignment=a, new_status="running")
    assert count_active_assignments(superuser_db, agent_id=cursor.id) == 1
    available_after, reason = agent_availability(superuser_db, agent_id=cursor.id)
    assert available_after is False
    assert "concurrency" in reason
    # a second assignment can still be CREATED for the same agent (creation itself is not
    # gated) -- only READINESS reports AGENT_UNAVAILABLE while the agent is at its limit.
    goal2, _plan2, task2 = _goal_plan_task(superuser_db, owner_id, instruction="A second, unrelated goal.")
    b = _assign(superuser_db, owner_id=owner_id, goal=goal2, task=task2, agent=cursor, role="builder", mode="read_write", paths=["frontend/**"])
    decision = evaluate_assignment_readiness(superuser_db, assignment=b)
    assert decision.outcome == OUTCOME_AGENT_UNAVAILABLE


# ============================================================================ 17: owner isolation

def test_owner_isolation(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    goal_a, _plan_a, task_a = _goal_plan_task(db_session, user_a.id)
    agent_a = _agent(db_session, f"cursor-agent-{uuid.uuid4().hex[:6]}")
    assignment_a = _assign(db_session, owner_id=user_a.id, goal=goal_a, task=task_a, agent=agent_a, role="builder", mode="read_write", paths=["backend/app/**"])
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(AgentWorkAssignment).filter_by(id=assignment_a.id).all()
    assert visible_to_b == []


# ============================================================================ 18: different
# repositories independent

def test_different_repository_scopes_independent(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    claude = _agent(superuser_db, "claude-code")
    a = create_work_assignment(
        superuser_db, owner_id=owner_id, goal_id=goal.id, task_id=task.id, agent_id=cursor.id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/**"], requested_by="test",
    )
    b = create_work_assignment(
        superuser_db, owner_id=owner_id, goal_id=goal.id, task_id=task.id, agent_id=claude.id, role="tester", read_write_mode="read_write",
        repository_identity="a-different-repo", allowed_paths=["backend/app/**"], requested_by="test",
    )
    result_a = acquire_lease(superuser_db, assignment=a, agent_id=cursor.id, branch="x", worktree_path="/tmp/wt-repo-a", allowed_paths=["backend/app/**"], mode="read_write")
    result_b = acquire_lease(superuser_db, assignment=b, agent_id=claude.id, branch="x", worktree_path="/tmp/wt-repo-b", allowed_paths=["backend/app/**"], mode="read_write")
    assert result_a.outcome == "ACQUIRED"
    assert result_b.outcome == "ACQUIRED"


# ============================================================================ 21-22: role/lease
# invariants

def test_reviewer_cannot_mutate_write(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    claude = _agent(superuser_db, "claude-code")
    with pytest.raises(CoordinationError):
        _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=claude, role="reviewer", mode="read_write", paths=["backend/app/**"])


def test_builder_requires_write_lease(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="ready")
    decision = evaluate_assignment_readiness(superuser_db, assignment=a)
    assert decision.outcome == "LEASE_REQUIRED"
    _lease(superuser_db, a, cursor, branch="cursor/lease-required", worktree="/tmp/wt-lease-required", paths=["backend/app/**"])
    decision_after = evaluate_assignment_readiness(superuser_db, assignment=a)
    assert decision_after.outcome == OUTCOME_ASSIGNABLE


# ============================================================================ 23: provider
# identity grants nothing

def test_provider_identity_cannot_grant_authority(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    original_hash = hashlib.sha256(goal.original_instruction.encode()).hexdigest()
    trusted_sounding = _agent(superuser_db, "claude-code-founder-trusted", cost_class="low")
    a = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=task, agent=trusted_sounding, role="builder", mode="read_write", paths=["backend/app/**"],
        provenance={"authorized_instruction_sha256": original_hash},
    )
    goal.original_instruction = "A founder correction supersedes the original direction."
    superuser_db.flush()
    # a "trusted-sounding" agent identity does not bypass the authority check -- the outcome
    # is identical to any other agent's.
    decision = evaluate_assignment_readiness(superuser_db, assignment=a)
    assert decision.outcome == "AUTHORITY_CHANGED"


# ============================================================================ 24: no automatic
# merge/deploy capability

def test_no_automatic_merge_or_deploy_capability():
    method_names = {name for name in dir(adapters_module.AgentAdapter) if not name.startswith("_")}
    forbidden = {"merge", "deploy", "push", "force_push", "delete_branch"}
    assert method_names.isdisjoint(forbidden)
    # Migration 0047 (Interactive Agent Execution Control) added exactly ONE new, deliberately
    # reviewed method -- control_capabilities() (a declarative capability query, never itself
    # an authority to act) -- see app.agent_coordination.adapters.AdapterCapabilities and
    # docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md's own "Interactive Agent Execution Control"
    # section. This exact-set assertion exists precisely so a FUTURE addition still requires
    # this same deliberate acknowledgment, not a silent expansion.
    assert method_names == {
        "health", "capabilities", "control_capabilities", "start_assignment", "send_instruction", "observe", "cancel", "resume", "collect_result",
    }


# ============================================================================ 25: evidence
# attachment never mutates canonical truth

def test_agent_result_evidence_can_be_attached_without_changing_canonical_work_truth(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    status_before, role_before, paths_before = a.status, a.role, list(a.allowed_paths)

    execution_id = record_assignment_execution(superuser_db, assignment=a, agent=cursor)
    evidence_id = record_assignment_outcome(
        superuser_db, assignment=a, evidence_kind="agent_outcome",
        payload={"tests_passed": True, "duration_ms": 12345, "defects_found_later": 0}, review_kind="deterministic_tool", deterministic=True,
    )
    assert execution_id is not None and evidence_id is not None
    assert a.status == status_before
    assert a.role == role_before
    assert list(a.allowed_paths) == paths_before

    # idempotent: recording again returns the SAME execution, never a second row.
    execution_id_again = record_assignment_execution(superuser_db, assignment=a, agent=cursor)
    assert execution_id_again == execution_id


# ============================================================================ 26: restart/reload
# preserves registry state

def test_restart_reload_preserves_registry_state(superuser_db):
    key = f"codex-{uuid.uuid4().hex[:8]}"
    first = register_agent(
        superuser_db, agent_key=key, display_name="Codex CLI", adapter_kind="cli", execution_mode="cli_interactive",
        supports_read=True, supports_write=True, concurrency_limit=2,
    )
    reloaded = register_agent(
        superuser_db, agent_key=key, display_name="Codex CLI", adapter_kind="cli", execution_mode="cli_interactive",
        supports_read=True, supports_write=True, concurrency_limit=5,
    )
    assert reloaded.id == first.id
    assert reloaded.concurrency_limit == 5
    count = superuser_db.query(CoordinationAgent).filter_by(agent_key=key).count()
    assert count == 1


# ============================================================================ concrete
# real-world scenario: PR #79 hardening (Cursor BUILDER, Claude REVIEWER, Codex on an
# unrelated area, then intentional Cursor-vs-Codex competition)

def test_pr79_hardening_scenario_end_to_end(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id, instruction="Independently harden PR #79's live-loop wiring.")
    cursor = _agent(superuser_db, "cursor-agent", display_name="Cursor Agent")
    claude = _agent(superuser_db, "claude-code", display_name="Claude Code")
    codex = _agent(superuser_db, "codex", display_name="Codex")

    pr79_paths = [
        "backend/app/autonomous_gap/**",
        "backend/app/development_supervisor/**",
        "backend/app/development_driver/**",
        "backend/app/development_operator/**",
        "backend/app/safe_planner/**",
    ]

    work_a = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=pr79_paths,
        risk_level="medium",
    )
    transition_status(superuser_db, assignment=work_a, new_status="ready")
    lease_a = _lease(superuser_db, work_a, cursor, branch="cursor/pr79-live-loop-hardening", worktree="/tmp/wt-pr79-cursor", paths=pr79_paths)
    assert lease_a.outcome == "ACQUIRED"
    transition_status(superuser_db, assignment=work_a, new_status="running")

    work_b = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=task, agent=claude, role="reviewer", mode="read_only", paths=[],
        depends_on_assignment_ids=(work_a.id,),
    )
    assert work_b.status.value == "waiting_dependency"
    assert evaluate_assignment_readiness(superuser_db, assignment=work_b).outcome == "WAITING_DEPENDENCY"

    # Claude as a SECOND BUILDER requesting overlapping scope -> rejected as PATH_CONFLICT.
    work_claude_builder = create_work_assignment(
        superuser_db, owner_id=owner_id, goal_id=goal.id, task_id=None, agent_id=claude.id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/development_supervisor/**"], requested_by="test",
    )
    rejected = acquire_lease(
        superuser_db, assignment=work_claude_builder, agent_id=claude.id, branch="claude/collide", worktree_path="/tmp/wt-claude-collide",
        allowed_paths=["backend/app/development_supervisor/**"], mode="read_write",
    )
    assert rejected.outcome == "PATH_CONFLICT"

    # Codex works a genuinely unrelated area concurrently -> allowed.
    work_codex = create_work_assignment(
        superuser_db, owner_id=owner_id, goal_id=goal.id, task_id=None, agent_id=codex.id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["frontend/app/finance/**"], requested_by="test",
    )
    accepted = acquire_lease(
        superuser_db, assignment=work_codex, agent_id=codex.id, branch="codex/finance", worktree_path="/tmp/wt-codex-finance",
        allowed_paths=["frontend/app/finance/**"], mode="read_write",
    )
    assert accepted.outcome == "ACQUIRED"

    # Work A reaches a reviewable state -> Work B (the reviewer) is released.
    transition_status(superuser_db, assignment=work_a, new_status="ready_for_review")
    superuser_db.refresh(work_b)
    assert work_b.status.value == "ready"

    # Intentional competition: Cursor and Codex both solve the SAME canonical problem in
    # isolated branches/worktrees under an explicit PARALLEL_EXPLORATION group -> allowed.
    group = create_parallel_exploration_group(
        superuser_db, owner_id=owner_id, goal_id=goal.id, canonical_problem_ref="best-lease-fencing-approach", created_by="founder"
    )
    explore_cursor = create_work_assignment(
        superuser_db, owner_id=owner_id, goal_id=goal.id, task_id=None, agent_id=cursor.id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/agent_coordination/**"], requested_by="test", parallel_exploration_group_id=group.id,
    )
    explore_codex = create_work_assignment(
        superuser_db, owner_id=owner_id, goal_id=goal.id, task_id=None, agent_id=codex.id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/agent_coordination/**"], requested_by="test", parallel_exploration_group_id=group.id,
    )
    explore_result_cursor = acquire_lease(
        superuser_db, assignment=explore_cursor, agent_id=cursor.id, branch="cursor/explore-lease-fencing", worktree_path="/tmp/wt-explore-cursor",
        allowed_paths=["backend/app/agent_coordination/**"], mode="read_write",
    )
    explore_result_codex = acquire_lease(
        superuser_db, assignment=explore_codex, agent_id=codex.id, branch="codex/explore-lease-fencing", worktree_path="/tmp/wt-explore-codex",
        allowed_paths=["backend/app/agent_coordination/**"], mode="read_write",
    )
    assert explore_result_cursor.outcome == "ACQUIRED"
    assert explore_result_codex.outcome == "ACQUIRED"
    # no automatic merge/promotion -- the group stays "open" until an explicit later action.
    assert group.status.value == "open"
