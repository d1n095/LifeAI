"""Extends PR #431's atomic-authorization fix from the workforce execution boundary to the
`app.agent_coordination.dispatch.dispatch_assignment()` boundary (founder directive, 2026-11-15,
"CONTINUE ACTIVE ENGINEERING", Priority 1). `docs/LIFE_WORKFORCE_AUTHORIZATION_RACE_WINDOW.md`
named this as the one remaining real execution path still only protected by an unlocked
approval read (`evaluate_dispatch_readiness()`'s own `require_task_approval()` call) rather
than the atomic, transactional recheck PR #431 gave workforce.

`app.mainai_execution.approval.require_live_task_approval_for_dispatch()` (new) takes a real
`SELECT ... FOR UPDATE` lock on the MainAITask, the SAME idiom
`app.mainai_execution.executor._lock_task()` already uses for cancel_task()/
dispatch_ready_task()/retry_task(), and the SAME pattern
`app.workforce.provider_worker.require_live_task_authorization_for_workforce_dispatch()`
(PR #431) independently applies to the workforce path. `dispatch_assignment()` now calls it
immediately before transitioning the assignment and invoking the real adapter -- never trusting
`evaluate_dispatch_readiness()`'s own earlier, unlocked check as still valid at invoke time.

Every genuine-concurrency test here uses TWO REAL, SEPARATE database connections
(`migration_engine.connect()`, each with its own explicit transaction), mirroring PR #379's
`test_blueprint_approval_concurrency.py` and PR #431's
`test_workforce_task_authorization_concurrency.py` exactly: a lock holder connection, a
`SET LOCAL lock_timeout` on the contending connection so a real block surfaces as a fast,
deterministic `OperationalError` instead of a hang.

This does NOT activate any dormant provider path and does NOT touch `require_adapter_enabled`'s
own existing gate (`app.agent_coordination.adapter_config.adapter_availability()`) -- every test
here dispatches through either the test-only `_FakeAgentAdapter`/`_SlowFakeAgentAdapter` (never
imported as a real adapter) or `NotConfiguredAdapter`, never `get_real_adapter()`."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import OperationalError

from app.agent_coordination.adapters import AgentHealth, AgentObservation, AgentResult
from app.agent_coordination.dispatch import (
    OUTCOME_APPROVAL_REQUIRED,
    OUTCOME_ASSIGNABLE,
    dispatch_assignment,
    evaluate_dispatch_readiness,
)
from app.agent_coordination.service import (
    acquire_lease,
    create_work_assignment,
    register_agent,
    transition_status,
)
from app.db import migration_engine
from app.mainai_execution.approval import (
    grant_task_approval,
    require_live_task_approval_for_dispatch,
)
from app.mainai_execution.executor import cancel_task
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.mainai_execution import MainAITask


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure the other agent_coordination/mainai test modules already use."""
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_plan_task(db, owner_id, *, instruction="Dispatch authorization boundary test.", approval_policy="standard_repo_work"):
    goal = create_goal(db, owner_id=owner_id, title="Dispatch auth boundary test", original_instruction=instruction, created_by="founder", approval_policy=approval_policy)
    plan = create_plan(db, goal=goal, rationale="dispatch auth boundary test", tasks=[PlannedTaskSpec(description="t", task_type="repo_edit")], created_by="founder")
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).first()
    return goal, plan, task


def _agent(db, key, **kwargs):
    defaults = {"display_name": key, "adapter_kind": "cli", "execution_mode": "cli_interactive", "supports_read": True, "supports_write": True, "concurrency_limit": 3}
    defaults.update(kwargs)
    return register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", **defaults)


def _assign(db, *, owner_id, goal, task, agent, role, mode, paths, **kwargs):
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=task.id if task else None, agent_id=agent.id,
        role=role, read_write_mode=mode, repository_identity="lifeai", allowed_paths=list(paths), requested_by="test", **kwargs
    )


def _lease(db, assignment, agent, *, branch, worktree, paths, mode="read_write"):
    return acquire_lease(db, assignment=assignment, agent_id=agent.id, branch=branch, worktree_path=worktree, allowed_paths=list(paths), mode=mode)


class _FakeAgentAdapter:
    """Deterministic, instant test double -- lives HERE, never in production code."""

    def __init__(self):
        self.started: list[uuid.UUID] = []

    async def health(self):
        return AgentHealth(healthy=True, detail="fake adapter")

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


class _SlowFakeAgentAdapter(_FakeAgentAdapter):
    """Same deterministic double, but `start_assignment()` genuinely awaits for `delay_seconds`
    before returning -- making the real `await adapter.start_assignment()` window inside
    `dispatch_assignment()` observable from a second, real database connection. Mirrors what
    the ONE real adapter implementation (`LocalCLIAdapter.start_assignment()`,
    app/agent_coordination/adapters.py) actually does: spawn a subprocess and return, never
    await the agent's own work -- so this models a (deliberately exaggerated) process-spawn
    delay, never "wait for the agent to finish.\""""

    def __init__(self, delay_seconds: float):
        super().__init__()
        self._delay = delay_seconds

    async def start_assignment(self, assignment_id):
        await asyncio.sleep(self._delay)
        self.started.append(assignment_id)


def _lock_task_row(conn, task_id: str, *, lock_timeout_ms: int | None = None):
    if lock_timeout_ms is not None:
        conn.execute(sa_text(f"SET LOCAL lock_timeout = '{lock_timeout_ms}ms'"))
    return conn.execute(sa_text("SELECT status FROM mainai_tasks WHERE id = CAST(:id AS uuid) FOR UPDATE"), {"id": task_id})


# ============================================================================ 1: the real
# production path closes the gap evaluate_dispatch_readiness() alone leaves open


@pytest.mark.asyncio
async def test_dispatch_assignment_rejects_a_task_cancelled_after_an_earlier_readiness_check_passed(superuser_db, owner_id):
    """Reproduces the exact TOCTOU gap this PR closes: an EARLIER evaluate_dispatch_readiness()
    call sees the task approved and live -- exactly what a caller that checked readiness once
    and then (wrongly) trusted it would see -- but by the time dispatch_assignment() itself
    runs, the founder has cancelled the task. The atomic re-check inside dispatch_assignment()
    must still catch this even though evaluate_dispatch_readiness(), called earlier, did not
    (and structurally cannot, since nothing has locked anything at that point)."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/revoke/**"])
    _lease(superuser_db, a, cursor, branch="cursor/revoke", worktree="/tmp/wt-dispatch-revoke", paths=["backend/app/revoke/**"])
    transition_status(superuser_db, assignment=a, new_status="running")

    stale_decision = evaluate_dispatch_readiness(superuser_db, assignment=a, agent=cursor)
    assert stale_decision.outcome == OUTCOME_ASSIGNABLE  # the earlier, now-stale "all clear"

    cancel_task(superuser_db, task=task, cancelled_by="founder")  # founder cancels in the gap

    adapter = _FakeAgentAdapter()
    decision = await dispatch_assignment(superuser_db, assignment=a, agent=cursor, adapter=adapter)
    assert decision.outcome == OUTCOME_APPROVAL_REQUIRED
    assert adapter.started == []  # never invoked -- caught before the real call
    superuser_db.refresh(a)
    assert a.status.value == "running"  # dispatch_assignment() never mutated it on rejection


@pytest.mark.asyncio
async def test_dispatch_assignment_still_succeeds_for_a_task_that_remains_live_and_approved(superuser_db, owner_id):
    """Control case -- the atomic recheck must not be a stricter false-positive gate: a task
    that genuinely remains live and approved still dispatches normally."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/live/**"])
    _lease(superuser_db, a, cursor, branch="cursor/live", worktree="/tmp/wt-dispatch-live", paths=["backend/app/live/**"])

    adapter = _FakeAgentAdapter()
    decision = await dispatch_assignment(superuser_db, assignment=a, agent=cursor, adapter=adapter)
    assert decision.outcome == OUTCOME_ASSIGNABLE
    assert adapter.started == [a.id]


# ============================================================================ 2: real-invoke
# timing -- authorization remains valid THROUGH the actual adapter invoke, not merely at an
# earlier check, and the lock-hold window is bounded by "time to start", not "time to finish"


@pytest.mark.asyncio
async def test_lock_is_held_through_the_real_adapter_invoke_not_only_the_earlier_check(superuser_db, owner_id):
    """While dispatch_assignment() is awaiting adapter.start_assignment() -- the real external-
    call boundary -- a genuinely separate Postgres connection cannot acquire the SAME
    MainAITask row's lock, proving no concurrent revocation/mutation of that row can land
    mid-dispatch. The one real adapter implementation, LocalCLIAdapter.start_assignment()
    (app/agent_coordination/adapters.py), only calls asyncio.create_subprocess_exec and
    returns -- it does not await the agent's actual work (that happens later, via
    collect_dispatch_result(), entirely outside this transaction) -- so in production this
    lock-hold window is bounded by "time to spawn a process", never "time to do the work",
    which is what makes holding the lock across this specific await acceptable rather than the
    "unnecessary long-lived lock across an external provider call" the founder's directive
    warned against. _SlowFakeAgentAdapter's artificial delay only makes that bounded window
    observable from a second connection; it does not change what dispatch_assignment() itself
    holds the lock across."""
    goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cursor = _agent(superuser_db, "cursor-agent")
    a = _assign(superuser_db, owner_id=owner_id, goal=goal, task=task, agent=cursor, role="builder", mode="read_write", paths=["backend/app/timing/**"])
    _lease(superuser_db, a, cursor, branch="cursor/timing", worktree="/tmp/wt-dispatch-timing", paths=["backend/app/timing/**"])
    superuser_db.commit()

    adapter = _SlowFakeAgentAdapter(delay_seconds=0.4)
    dispatch_task = asyncio.create_task(dispatch_assignment(superuser_db, assignment=a, agent=cursor, adapter=adapter))
    await asyncio.sleep(0.1)  # let dispatch_assignment() reach the atomic lock and enter the adapter's sleep
    assert not dispatch_task.done()

    blocker = migration_engine.connect()
    try:
        blocker_tx = blocker.begin()
        with pytest.raises(OperationalError) as exc:
            _lock_task_row(blocker, str(task.id), lock_timeout_ms=500)
        assert "lock" in str(exc.value).lower()
        blocker_tx.rollback()
    finally:
        blocker.close()

    decision = await dispatch_task
    assert decision.outcome == OUTCOME_ASSIGNABLE
    assert adapter.started == [a.id]
    superuser_db.commit()  # release the lock this test's own session was holding

    released = migration_engine.connect()
    try:
        tx = released.begin()
        row = _lock_task_row(released, str(task.id)).first()
        assert row is not None  # lock is free immediately after commit -- not leaked
        tx.rollback()
    finally:
        released.close()


# ============================================================================ 3: liveness --
# unrelated tasks are never serialized behind this fix (row-level, not table-level, locking)


def test_locks_on_different_tasks_never_contend_unrelated_dispatch_is_never_blocked(superuser_db, owner_id):
    """Two different tasks' authorization locks must never contend -- proving this fix adds
    per-task row-level exclusion only, never a broader serialization point that would make one
    task's dispatch wait on an unrelated task's."""
    _goal_a, _plan_a, task_a = _goal_plan_task(superuser_db, owner_id, instruction="task A")
    _goal_b, _plan_b, task_b = _goal_plan_task(superuser_db, owner_id, instruction="task B")

    conn_a = migration_engine.connect()
    conn_b = migration_engine.connect()
    try:
        tx_a = conn_a.begin()
        _lock_task_row(conn_a, str(task_a.id))

        tx_b = conn_b.begin()
        # Different row -- must succeed immediately, never wait on conn_a's lock.
        _lock_task_row(conn_b, str(task_b.id), lock_timeout_ms=500)
        tx_b.rollback()
        tx_a.rollback()
    finally:
        conn_a.close()
        conn_b.close()


# ============================================================================ 4: competing
# workers / revocation vs. dispatch -- genuine two-connection proofs


def test_for_update_lock_blocks_a_concurrent_cancel_task_style_write(superuser_db, owner_id):
    """The actual lock require_live_task_approval_for_dispatch() now takes (mirrored here as
    raw SQL, the identical statement cancel_task()'s own _lock_task() issues against the same
    row) blocks a concurrent cancellation attempt from even acquiring its own lock -- proving a
    revocation cannot land between the authorization recheck and whatever follows it inside
    the same transaction."""
    _goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    task_id = str(task.id)

    holder = migration_engine.connect()
    canceller = migration_engine.connect()
    try:
        holder_tx = holder.begin()
        held = _lock_task_row(holder, task_id).first()
        assert held is not None

        canceller_tx = canceller.begin()
        with pytest.raises(OperationalError) as exc:
            _lock_task_row(canceller, task_id, lock_timeout_ms=750)
        assert "lock" in str(exc.value).lower()
        canceller_tx.rollback()
        holder_tx.rollback()
    finally:
        canceller.close()
        holder.close()


def test_two_concurrent_authorization_attempts_on_the_same_task_serialize_not_interleave(superuser_db, owner_id):
    """Two agent-coordination dispatch attempts racing to authorize the SAME task cannot both
    proceed at once -- the second's lock attempt blocks until the first's transaction ends,
    proving mutual exclusion rather than two independent, possibly-inconsistent decisions."""
    _goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    task_id = str(task.id)

    first = migration_engine.connect()
    second = migration_engine.connect()
    try:
        first_tx = first.begin()
        _lock_task_row(first, task_id)

        second_tx = second.begin()
        with pytest.raises(OperationalError):
            _lock_task_row(second, task_id, lock_timeout_ms=750)
        second_tx.rollback()
        first_tx.commit()
    finally:
        second.close()
        first.close()


def test_rollback_releases_the_lock_and_an_immediate_retry_sees_unchanged_state(superuser_db, owner_id):
    """A dispatch attempt that fails and rolls back (e.g. an adapter error) must not leave the
    task locked or mutated -- a retry immediately after must acquire the lock without delay and
    see the task exactly as it was."""
    _goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    task_id = str(task.id)

    first = migration_engine.connect()
    try:
        first_tx = first.begin()
        _lock_task_row(first, task_id)
        first_tx.rollback()
    finally:
        first.close()

    retry = migration_engine.connect()
    try:
        retry_tx = retry.begin()
        row = _lock_task_row(retry, task_id).first()
        assert row is not None and row[0] == "pending"
        retry_tx.rollback()
    finally:
        retry.close()


def test_the_real_gate_function_for_dispatch_holds_the_lock_until_callers_transaction_ends(superuser_db, owner_id):
    """Calls require_live_task_approval_for_dispatch() for real (not a raw-SQL stand-in) and
    proves, via a genuinely separate connection, that the lock it took is still held after the
    call returns -- it never commits or rolls back `db` itself; that stays the caller's
    responsibility. The agent_coordination/MainAITask-linked counterpart of PR #431's
    test_the_real_gate_function_holds_the_lock_until_its_callers_transaction_ends."""
    _goal, _plan, task = _goal_plan_task(superuser_db, owner_id, approval_policy="autonomous_development_work")
    grant_task_approval(superuser_db, task=task, approved_by="founder")
    superuser_db.commit()

    require_live_task_approval_for_dispatch(superuser_db, owner_id=owner_id, task_id=task.id)  # real call, no commit

    blocker = migration_engine.connect()
    try:
        blocker_tx = blocker.begin()
        with pytest.raises(OperationalError):
            _lock_task_row(blocker, str(task.id), lock_timeout_ms=750)
        blocker_tx.rollback()
    finally:
        blocker.close()

    superuser_db.rollback()  # release the real lock this test's own session is holding

    unblocked = migration_engine.connect()
    try:
        tx = unblocked.begin()
        row = _lock_task_row(unblocked, str(task.id)).first()
        assert row is not None
        tx.rollback()
    finally:
        unblocked.close()


# ============================================================================ 5: fail-closed
# liveness checks on the function itself (sequential, not concurrency, but real production code)


def test_require_live_task_approval_for_dispatch_rejects_a_cancelled_task(superuser_db, owner_id):
    _goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    cancel_task(superuser_db, task=task, cancelled_by="founder")
    superuser_db.commit()

    from app.mainai_execution.approval import ApprovalRequiredError

    with pytest.raises(ApprovalRequiredError):
        require_live_task_approval_for_dispatch(superuser_db, owner_id=owner_id, task_id=task.id)


def test_require_live_task_approval_for_dispatch_rejects_a_foreign_owner(superuser_db, owner_id, make_verified_user):
    _goal, _plan, task = _goal_plan_task(superuser_db, owner_id)
    other_user, _ = make_verified_user()

    from app.mainai_execution.approval import ApprovalRequiredError

    with pytest.raises(ApprovalRequiredError):
        require_live_task_approval_for_dispatch(superuser_db, owner_id=other_user.id, task_id=task.id)
