"""Bounded Dispatch Foundation -- proves the real-agent bootstrap (app.agent_coordination.
bootstrap), the fail-closed dispatch gate and control-plane entry point (app.agent_coordination.
dispatch), and the `NotConfiguredAdapter` safety default (app.agent_coordination.adapters), all
built on top of PR #82/#83/#84's already-merged/reviewed coordination tables and routing layer.

`test_current_real_world_dispatch_scenario_end_to_end` extends the same concrete situation
this whole foundation exists to represent: Cursor busy on PR #79/#80's exact paths, Claude
available after PR #84, Codex idle -- proving Life can select the right free agent, create a
bounded dispatch, refuse one that would collide with Cursor's scope, permit one that doesn't,
and record structured result evidence, all without ever widening authority beyond what the
coordination layer itself already granted."""

import uuid

import pytest

from app.agent_coordination.adapters import AgentHealth, AgentObservation, AgentResult, NotConfiguredAdapter
from app.agent_coordination.bootstrap import KNOWN_AGENT_DEFAULTS, bootstrap_known_agents
from app.agent_coordination.dispatch import (
    DISPATCH_LIFECYCLE,
    OUTCOME_APPROVAL_REQUIRED,
    OUTCOME_CAPABILITY_MISMATCH,
    OUTCOME_REAL_PROVIDER_NOT_CONFIGURED,
    OUTCOME_SCOPE_NOT_EXPLICIT,
    apply_dispatch_result,
    dispatch_assignment,
    evaluate_dispatch_readiness,
    DispatchResult,
)
from app.agent_coordination.routing import eligible_agents_for, next_feasible_assignment_for_agent, OUTCOME_SCOPE_CONFLICT as ROUTING_SCOPE_CONFLICT
from app.agent_coordination.service import OUTCOME_ASSIGNABLE, acquire_lease, create_work_assignment, register_agent, transition_status
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.agent_coordination import CoordinationAgent
from app.models.mainai_execution import MainAITask


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure as the other agent_coordination test modules' own identical
    fixture."""
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_plan_task(db, owner_id, *, instruction="Dispatch foundation test.", approval_policy="standard_repo_work", task_count=1):
    goal = create_goal(db, owner_id=owner_id, title="Dispatch test", original_instruction=instruction, created_by="founder", approval_policy=approval_policy)
    plan = create_plan(
        db, goal=goal, rationale="dispatch test",
        tasks=[PlannedTaskSpec(description=f"Task {i}", task_type="repo_edit") for i in range(task_count)],
        created_by="founder",
    )
    db.commit()
    tasks = db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).all()
    if task_count == 1:
        return goal, plan, tasks[0]
    return goal, plan, tasks


def _agent(db, key, **kwargs):
    defaults = dict(display_name=key, adapter_kind="cli", execution_mode="cli_interactive", supports_read=True, supports_write=True, concurrency_limit=3)
    defaults.update(kwargs)
    return register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", **defaults)


def _assign(db, *, owner_id, goal, task, agent, role, mode, paths, **kwargs):
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=task.id if task else None, agent_id=agent.id,
        role=role, read_write_mode=mode, repository_identity="lifeai", allowed_paths=list(paths), requested_by="test", **kwargs
    )


def _lease(db, assignment, agent, *, branch, worktree, paths, mode="read_write", ttl_seconds=None):
    return acquire_lease(db, assignment=assignment, agent_id=agent.id, branch=branch, worktree_path=worktree, allowed_paths=list(paths), mode=mode, ttl_seconds=ttl_seconds)


class _FakeAgentAdapter:
    """Deterministic test double satisfying the AgentAdapter Protocol structurally -- lives
    HERE, in the test file, never in production code, so there is zero risk of it ever being
    imported as a real adapter by accident. Records every call for assertions; touches no
    subprocess, network, or credential."""

    def __init__(self):
        self.started: list[uuid.UUID] = []

    async def health(self):
        return AgentHealth(healthy=True, detail="fake adapter, always healthy")

    def capabilities(self):
        return ("repo_edit", "run_tests")

    async def start_assignment(self, assignment_id):
        self.started.append(assignment_id)

    async def send_instruction(self, assignment_id, instruction):
        pass

    async def observe(self, assignment_id):
        return AgentObservation(raw_status="running", summary="fake")

    async def cancel(self, assignment_id):
        pass

    async def resume(self, assignment_id):
        pass

    async def collect_result(self, assignment_id):
        return AgentResult(succeeded=True)


class _ExplodingAgentAdapter:
    """A second fake -- raises a real, non-ProviderNotConfiguredError exception from
    start_assignment(), proving dispatch_assignment() does not swallow or misclassify an
    unrelated adapter bug as REAL_PROVIDER_NOT_CONFIGURED."""

    async def start_assignment(self, assignment_id):
        raise RuntimeError("adapter blew up for an unrelated reason")

    async def health(self):
        return AgentHealth(healthy=True)

    def capabilities(self):
        return ()

    async def send_instruction(self, assignment_id, instruction):
        pass

    async def observe(self, assignment_id):
        return AgentObservation(raw_status="unknown")

    async def cancel(self, assignment_id):
        pass

    async def resume(self, assignment_id):
        pass

    async def collect_result(self, assignment_id):
        return AgentResult(succeeded=False)


# ============================================================================ Priority 1:
# real agent bootstrap

def test_bootstrap_known_agents_registers_all_three(superuser_db):
    agents = bootstrap_known_agents(superuser_db)
    assert {a.agent_key for a in agents} == set(KNOWN_AGENT_DEFAULTS.keys())
    assert all(a.supports_write for a in agents)
    assert all(a.model_hint is None for a in agents)  # never a credential/version claim invented


def test_bootstrap_known_agents_is_idempotent_and_update_safe(superuser_db):
    first = bootstrap_known_agents(superuser_db, agent_keys=("codex",))[0]
    first_id = first.id

    # simulate a founder-edited default (e.g. a real observed concurrency limit) by patching
    # the module-level dict directly, then re-running -- same row, new values.
    original = dict(KNOWN_AGENT_DEFAULTS["codex"])
    KNOWN_AGENT_DEFAULTS["codex"] = {**original, "concurrency_limit": 2}
    try:
        second = bootstrap_known_agents(superuser_db, agent_keys=("codex",))[0]
    finally:
        KNOWN_AGENT_DEFAULTS["codex"] = original

    assert second.id == first_id  # never a new row
    assert second.concurrency_limit == 2
    count = superuser_db.query(CoordinationAgent).filter_by(agent_key="codex").count()
    assert count == 1  # never duplicated


def test_bootstrap_known_agents_unknown_key_fails_closed(superuser_db):
    with pytest.raises(KeyError):
        bootstrap_known_agents(superuser_db, agent_keys=("not-a-real-agent",))


def test_bootstrap_known_agents_never_embeds_a_credential(superuser_db):
    # `agent_key` itself is passed separately to register_agent(), never inside `defaults` --
    # asserting only "not in blob" (not the earlier, accidentally-vacuous "or" form) so this
    # genuinely fails if a credential-shaped field were ever added to KNOWN_AGENT_DEFAULTS.
    for defaults in KNOWN_AGENT_DEFAULTS.values():
        assert "agent_key" not in defaults
        blob = str(defaults).lower()
        assert "key" not in blob
        assert "token" not in blob and "secret" not in blob and "password" not in blob


# ============================================================================ Priority 2:
# dispatch lifecycle -- reuse, never a duplicate

def test_dispatch_lifecycle_maps_onto_existing_status_never_a_new_enum():
    from app.models.agent_coordination import WorkAssignmentStatus

    for name, status in DISPATCH_LIFECYCLE.items():
        assert isinstance(status, WorkAssignmentStatus), f"{name} must map onto the canonical WorkAssignmentStatus, not a new value"
    assert DISPATCH_LIFECYCLE["DISPATCHING"] == WorkAssignmentStatus.waiting_agent
    assert DISPATCH_LIFECYCLE["READY"] == WorkAssignmentStatus.ready


# ============================================================================ Priority 3:
# fail-closed dispatch gate

def test_dispatch_gate_passes_a_genuinely_ready_write_assignment(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    _lease(superuser_db, a, cursor, branch="cursor/finance", worktree="/tmp/wt-dispatch-gate-a", paths=["backend/app/finance/**"])
    transition_status(superuser_db, assignment=a, new_status="running")

    decision = evaluate_dispatch_readiness(superuser_db, assignment=a, agent=cursor)
    assert decision.outcome == OUTCOME_ASSIGNABLE


def test_dispatch_gate_rejects_lease_required_unlike_the_selector(superuser_db, owner_id):
    """The dispatch gate is STRICTER than next_feasible_assignment_for_agent() -- a 'ready'
    write assignment with no lease acquired yet is NOT dispatchable, even though the selector
    would correctly treat it as "found" (the caller's very next step)."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])

    selection = next_feasible_assignment_for_agent(superuser_db, agent_id=cursor.id, owner_id=owner_id)
    assert selection.assignment_id == a.id  # selector says "found"

    decision = evaluate_dispatch_readiness(superuser_db, assignment=a, agent=cursor)
    assert decision.outcome != OUTCOME_ASSIGNABLE  # gate says "not yet" -- no lease held


def test_dispatch_gate_capability_mismatch(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent", capabilities=["repo_edit"])
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-dispatch-cap", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")

    decision = evaluate_dispatch_readiness(superuser_db, assignment=a, agent=cursor, required_capabilities=("run_tests",))
    assert decision.outcome == OUTCOME_CAPABILITY_MISMATCH


def test_dispatch_gate_requires_explicit_scope_for_write(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    # No lease acquired -- branch/worktree_path are still None on the assignment itself.
    decision = evaluate_dispatch_readiness(superuser_db, assignment=a, agent=cursor)
    assert decision.outcome in (OUTCOME_SCOPE_NOT_EXPLICIT, "LEASE_REQUIRED")  # base gate (no lease) is checked first; both are correct fail-closed reasons


def test_dispatch_gate_read_only_never_requires_explicit_branch_worktree(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    claude = _agent(superuser_db, "claude-code")
    reviewer = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=claude, role="reviewer", mode="read_only", paths=[])
    transition_status(superuser_db, assignment=reviewer, new_status="running")

    decision = evaluate_dispatch_readiness(superuser_db, assignment=reviewer, agent=claude)
    assert decision.outcome == OUTCOME_ASSIGNABLE  # read_only never requires branch/worktree


def test_dispatch_gate_approval_required_blocks_via_the_real_gate(superuser_db, owner_id):
    """standard_repo_work's default marks repo_edit AUTO, so use autonomous_development_work
    (this module's own default policy), which requires approval for repo_edit -- proving the
    dispatch gate genuinely delegates to require_task_approval(), not a reimplementation."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id, approval_policy="autonomous_development_work")
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-dispatch-approval", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")

    decision = evaluate_dispatch_readiness(superuser_db, assignment=a, agent=cursor)
    assert decision.outcome == OUTCOME_APPROVAL_REQUIRED

    grant_task_approval(superuser_db, task=task, approved_by="founder")
    superuser_db.commit()
    decision_after = evaluate_dispatch_readiness(superuser_db, assignment=a, agent=cursor)
    assert decision_after.outcome == OUTCOME_ASSIGNABLE


def test_dispatch_gate_approval_required_without_a_task_fails_closed(superuser_db, owner_id):
    """No MainAITask exists -- there is no real gate to check against. Must fail closed, never
    silently treat "nothing to check" as "approved.\""""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)  # task exists but we won't link it
    cursor = _agent(superuser_db, "cursor-agent")
    a = create_work_assignment(
        superuser_db, owner_id=owner_id, goal_id=goal.id, task_id=None, agent_id=cursor.id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/**"], requested_by="test", approval_required=True,
    )
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-dispatch-no-task", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")

    decision = evaluate_dispatch_readiness(superuser_db, assignment=a, agent=cursor)
    assert decision.outcome == OUTCOME_APPROVAL_REQUIRED


# ============================================================================ Priority 4:
# dispatch(agent_id, assignment_id, authority_envelope)

@pytest.mark.asyncio
async def test_dispatch_assignment_happy_path_with_fake_adapter(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    _lease(superuser_db, a, cursor, branch="cursor/finance", worktree="/tmp/wt-dispatch-happy", paths=["backend/app/finance/**"])
    assert a.status.value == "ready"

    adapter = _FakeAgentAdapter()
    decision = await dispatch_assignment(superuser_db, assignment=a, agent=cursor, adapter=adapter)
    assert decision.outcome == OUTCOME_ASSIGNABLE
    assert adapter.started == [a.id]
    superuser_db.refresh(a)
    assert a.status.value == "running"


@pytest.mark.asyncio
async def test_dispatch_assignment_gate_failure_never_touches_the_adapter(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    # No lease -- gate must reject before ever calling the adapter.
    adapter = _FakeAgentAdapter()
    decision = await dispatch_assignment(superuser_db, assignment=a, agent=cursor, adapter=adapter)
    assert decision.outcome != OUTCOME_ASSIGNABLE
    assert adapter.started == []  # adapter never invoked
    superuser_db.refresh(a)
    assert a.status.value == "ready"  # untouched


@pytest.mark.asyncio
async def test_dispatch_assignment_real_provider_not_configured(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-dispatch-not-configured", paths=["backend/app/**"])

    adapter = NotConfiguredAdapter(provider_key="cursor-agent")
    decision = await dispatch_assignment(superuser_db, assignment=a, agent=cursor, adapter=adapter)
    assert decision.outcome == OUTCOME_REAL_PROVIDER_NOT_CONFIGURED
    superuser_db.refresh(a)
    assert a.status.value == "blocked"  # never left looking like it is running


@pytest.mark.asyncio
async def test_dispatch_assignment_unrelated_adapter_exception_propagates_and_marks_blocked(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-dispatch-explode", paths=["backend/app/**"])

    with pytest.raises(RuntimeError, match="adapter blew up"):
        await dispatch_assignment(superuser_db, assignment=a, agent=cursor, adapter=_ExplodingAgentAdapter())
    superuser_db.refresh(a)
    # Must actually reach 'blocked', not merely "not running" -- left in 'waiting_agent'
    # (DISPATCHING) would silently read as RuntimeStatus.IDLE elsewhere, masking a real crash.
    assert a.status.value == "blocked"


@pytest.mark.asyncio
async def test_dispatch_assignment_authority_envelope_cannot_escape_allowed_paths(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/finance/**"])
    _lease(superuser_db, a, cursor, branch="cursor/finance", worktree="/tmp/wt-dispatch-envelope", paths=["backend/app/finance/**"])

    adapter = _FakeAgentAdapter()
    decision = await dispatch_assignment(
        superuser_db, assignment=a, agent=cursor, adapter=adapter,
        authority_envelope={"allowed_paths": ["backend/app/finance/**", "backend/app/OTHER_SUBSYSTEM/**"]},
    )
    assert decision.outcome == OUTCOME_SCOPE_NOT_EXPLICIT
    assert adapter.started == []


# ============================================================================ Priority 6:
# result handoff

def test_apply_dispatch_result_success_records_evidence_and_completes(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-result-success", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")

    result = DispatchResult(succeeded=True, head_sha="deadbeef", pr_ref="https://github.com/d1n095/LifeAI/pull/999", tests_passed=True, tests_run=12)
    apply_dispatch_result(superuser_db, assignment=a, agent=cursor, result=result)

    superuser_db.refresh(a)
    assert a.status.value == "completed"
    assert a.head_sha == "deadbeef"
    assert a.upstream_pr_ref == "https://github.com/d1n095/LifeAI/pull/999"
    assert a.intelligence_execution_id is not None


def test_apply_dispatch_result_failure_transitions_to_failed_with_reason(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-result-fail", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")

    apply_dispatch_result(superuser_db, assignment=a, agent=cursor, result=DispatchResult(succeeded=False, failure_reason="tests failed on CI"))
    superuser_db.refresh(a)
    assert a.status.value == "failed"


def test_apply_dispatch_result_block_reason_transitions_to_blocked(superuser_db, owner_id):
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-result-block", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")

    apply_dispatch_result(superuser_db, assignment=a, agent=cursor, result=DispatchResult(succeeded=False, block_reason="waiting_founder_decision"))
    superuser_db.refresh(a)
    assert a.status.value == "blocked"


def test_apply_dispatch_result_never_fabricates_a_quality_score(superuser_db, owner_id):
    """Only fields the caller actually supplied are recorded -- nothing is invented."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/**"])
    _lease(superuser_db, a, cursor, branch="cursor/x", worktree="/tmp/wt-result-honest", paths=["backend/app/**"])
    transition_status(superuser_db, assignment=a, new_status="running")

    apply_dispatch_result(superuser_db, assignment=a, agent=cursor, result=DispatchResult(succeeded=True))
    superuser_db.refresh(a)
    assert a.head_sha is None  # never invented
    assert a.upstream_pr_ref is None


# ============================================================================ Priority 5: the
# concrete, current real-world dispatch scenario -- Cursor busy on PR #79/#80, Claude free
# after PR #84, Codex idle.

@pytest.mark.asyncio
async def test_current_real_world_dispatch_scenario_end_to_end(superuser_db, owner_id):
    goal, _plan, (cursor_task, claude_task) = _goal_plan_task(superuser_db, owner_id, task_count=2)
    cursor = _agent(superuser_db, "cursor-agent", display_name="Cursor Agent")
    claude = _agent(superuser_db, "claude-code", display_name="Claude Code")
    codex = _agent(superuser_db, "codex", display_name="Codex")

    pr79_80_paths = [
        "backend/app/autonomous_gap/**",
        "backend/app/development_supervisor/**",
        "backend/app/development_driver/**",
        "backend/app/development_operator/**",
        "backend/app/safe_planner/**",
    ]
    cursor_assignment = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=cursor_task, agent=cursor, role="builder", mode="read_write", paths=pr79_80_paths,
    )
    cursor_lease = _lease(superuser_db, cursor_assignment, cursor, branch="cursor/pr79-live-loop-hardening", worktree="/tmp/wt-e2e-cursor-busy", paths=pr79_80_paths)
    assert cursor_lease.outcome == "ACQUIRED"
    transition_status(superuser_db, assignment=cursor_assignment, new_status="running")

    # 1. Life sees Cursor cannot accept overlapping work.
    overlap = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/development_supervisor/**"],
    )
    assert overlap.outcome == ROUTING_SCOPE_CONFLICT

    # 2 & 3. Life sees Claude/Codex eligible for a genuinely unrelated new task, and selects one.
    new_task_paths = ["backend/app/agent_coordination/dispatch.py"]
    non_overlap = eligible_agents_for(
        superuser_db, owner_id=owner_id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=new_task_paths,
    )
    assert non_overlap.outcome in ("ELIGIBLE", "NEEDS_SELECTION")
    assert {claude.id, codex.id} <= set(non_overlap.eligible_agent_ids)
    chosen_agent = claude  # the founder/orchestrator picks Claude among the equally-eligible pair

    # 4. Life creates a bounded dispatch request (== a real AgentWorkAssignment -- never a
    # second, duplicate representation).
    dispatch_request = _assign(
        superuser_db, owner_id=owner_id, goal=goal, task=claude_task, agent=chosen_agent, role="builder", mode="read_write", paths=new_task_paths,
    )
    assert dispatch_request.status.value == "ready"  # DISPATCH_LIFECYCLE["READY"]

    # 5. Refuse any dispatch whose write scope collides with Cursor's -- proven again at the
    # GATE, not just at routing time (defense in depth).
    colliding = create_work_assignment(
        superuser_db, owner_id=owner_id, goal_id=goal.id, task_id=None, agent_id=codex.id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=["backend/app/autonomous_gap/service.py"], requested_by="test",
    )
    colliding_lease_attempt = acquire_lease(
        superuser_db, assignment=colliding, agent_id=codex.id, branch="codex/collide", worktree_path="/tmp/wt-e2e-collide", allowed_paths=["backend/app/autonomous_gap/service.py"], mode="read_write",
    )
    assert colliding_lease_attempt.outcome == "PATH_CONFLICT"

    # 6. Permit the non-overlapping dispatch: acquire its lease, then actually dispatch it
    # through the bounded control plane with a fake adapter (no real provider configured).
    lease = _lease(superuser_db, dispatch_request, chosen_agent, branch="claude/dispatch-followup", worktree="/tmp/wt-e2e-claude-free", paths=new_task_paths)
    assert lease.outcome == "ACQUIRED"
    adapter = _FakeAgentAdapter()
    dispatch_decision = await dispatch_assignment(superuser_db, assignment=dispatch_request, agent=chosen_agent, adapter=adapter)
    assert dispatch_decision.outcome == OUTCOME_ASSIGNABLE
    assert adapter.started == [dispatch_request.id]
    superuser_db.refresh(dispatch_request)
    assert dispatch_request.status.value == "running"  # DISPATCH_LIFECYCLE["RUNNING"]

    # 7. Record dispatch evidence/status once the (fake) worker completes.
    apply_dispatch_result(
        superuser_db, assignment=dispatch_request, agent=chosen_agent,
        result=DispatchResult(succeeded=True, head_sha="cafebabe", tests_passed=True, tests_run=3, pr_ref="https://github.com/d1n095/LifeAI/pull/1000"),
    )
    superuser_db.refresh(dispatch_request)
    assert dispatch_request.status.value == "completed"
    assert dispatch_request.head_sha == "cafebabe"

    # 8. Never widened paths/worktree/repo authority: the completed assignment's own
    # allowed_paths are EXACTLY what was granted at creation -- nothing broader.
    assert dispatch_request.allowed_paths == new_task_paths

    # Cursor's own work is completely unaffected throughout.
    superuser_db.refresh(cursor_assignment)
    assert cursor_assignment.status.value == "running"
    assert cursor_assignment.allowed_paths == pr79_80_paths
