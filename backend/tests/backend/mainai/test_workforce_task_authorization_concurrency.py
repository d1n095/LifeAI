"""Genuine concurrent-PostgreSQL proof for the atomicity fix (founder directive, 2026-11-15,
"STOP IDLING, CONTINUE ENGINEERING", Priority 1). docs/LIFE_WORKFORCE_AUTHORIZATION_RACE_WINDOW.md
named the gap this closes: the per-task authorization gate's task lookup now takes a real
`SELECT ... FOR UPDATE` row lock (app/workforce/provider_worker.py), the exact same lock
app.mainai_execution.executor._lock_task() already takes on the identical row for
cancel_task()/dispatch_ready_task()/retry_task() -- reusing that module's own established
locking idiom, never inventing a second one.

Every test here uses TWO REAL, SEPARATE database connections (`migration_engine.connect()`,
each with its own explicit transaction) -- not a single session re-read in sequence, which is
what the earlier TOCTOU regression (test_workforce_task_approval_enforcement.py) already
proved and which the founder's directive explicitly distinguished from genuine concurrency.
This mirrors the exact pattern PR #379 already established in this codebase for Blueprint's
own TOCTOU-safe approval (test_blueprint_approval_concurrency.py): a lock holder connection, a
`SET LOCAL lock_timeout` on the contending connection so a real block surfaces as a fast,
deterministic `OperationalError` instead of a hang, and explicit rollback/commit to show the
lock's exact lifetime.

This does NOT activate app.workforce's dormant real-provider path and does NOT touch the
unconditional ProviderActivationBlocked staged-raise -- the lock protects whichever real
effect eventually follows the authorization check, once activated, by ensuring nothing else
can mutate the task's authorization-relevant state while that check's transaction is open."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import OperationalError

from app.db import migration_engine
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.mainai_execution import MainAITask
from app.models.user import User
from app.workforce import (
    REQUIRED_ACTIVATION_GATES,
    GateStatus,
    record_gate_verification,
    register_workforce_agent,
    reset_activation_gates_for_tests,
    resolve_delegation,
    submit_delegation_request,
)
from app.workforce.provider_worker import (
    require_live_task_authorization_for_workforce_dispatch,
)


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine as _engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(_engine)


def _owner(db):
    u = User(email=f"wfac-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _approved_ready_task(db, owner_id):
    goal = create_goal(db, owner_id=owner_id, title="concurrency test", original_instruction="x", created_by="founder", approval_policy="autonomous_development_work")
    plan = create_plan(db, goal=goal, rationale="x", tasks=[PlannedTaskSpec(description="t", task_type="repo_edit")], created_by="founder")
    task = db.query(MainAITask).filter_by(plan_id=plan.id).first()
    grant_task_approval(db, task=task, approved_by="founder")
    db.commit()
    return goal, task


def _delegated_assignment(db, owner_id, task_id):
    register_workforce_agent(
        db, owner_id=owner_id, agent_key=f"b-{uuid.uuid4().hex[:8]}", name="B", role="builder",
        agent_type="CODING", capability_tags=["low_risk_classification"], status="active",
        trust_zone="LOCAL_INTERNAL", allowed_tool_classes=["read_excerpt"],
    )
    verifier = register_workforce_agent(
        db, owner_id=owner_id, agent_key=f"v-{uuid.uuid4().hex[:8]}", name="V", role="verifier",
        agent_type="VERIFIER", capability_tags=["verification"], status="active", trust_zone="LOCAL_INTERNAL",
    )
    request = submit_delegation_request(
        db, owner_id=owner_id, goal_text="x", required_capability="low_risk_classification",
        task_id=task_id, verification_requirement="independent_verifier",
    )
    return resolve_delegation(db, owner_id=owner_id, request=request, verifier_profile_id=verifier.id)


# --- Revocation vs. dispatch -------------------------------------------------------------------

def test_for_update_lock_blocks_a_concurrent_cancel_task_style_write(superuser_db):
    """The actual lock the gate now takes (mirrored here as raw SQL, the identical statement
    cancel_task()'s own _lock_task() issues against the same row) blocks a concurrent
    cancellation attempt from even acquiring its own lock -- proving a revocation cannot land
    between the authorization check and whatever follows it inside the same transaction."""
    owner = _owner(superuser_db)
    _goal, task = _approved_ready_task(superuser_db, owner.id)
    task_id = str(task.id)

    holder = migration_engine.connect()
    canceller = migration_engine.connect()
    try:
        holder_tx = holder.begin()
        held = holder.execute(sa_text("SELECT status FROM mainai_tasks WHERE id = CAST(:id AS uuid) FOR UPDATE"), {"id": task_id}).first()
        assert held is not None

        canceller_tx = canceller.begin()
        canceller.execute(sa_text("SET LOCAL lock_timeout = '750ms'"))
        with pytest.raises(OperationalError) as exc:
            canceller.execute(sa_text("SELECT status FROM mainai_tasks WHERE id = CAST(:id AS uuid) FOR UPDATE"), {"id": task_id})
        assert "lock" in str(exc.value).lower()
        canceller_tx.rollback()
        holder_tx.rollback()
    finally:
        canceller.close()
        holder.close()


# --- Competing workers ---------------------------------------------------------------------

def test_two_concurrent_authorization_attempts_on_the_same_task_serialize_not_interleave(superuser_db):
    """Two workforce workers racing to authorize/dispatch the SAME task cannot both proceed at
    once -- the second's lock attempt blocks until the first's transaction ends, proving
    mutual exclusion rather than two independent, possibly-inconsistent decisions."""
    owner = _owner(superuser_db)
    _goal, task = _approved_ready_task(superuser_db, owner.id)
    task_id = str(task.id)

    first = migration_engine.connect()
    second = migration_engine.connect()
    try:
        first_tx = first.begin()
        first.execute(sa_text("SELECT status FROM mainai_tasks WHERE id = CAST(:id AS uuid) FOR UPDATE"), {"id": task_id})

        second_tx = second.begin()
        second.execute(sa_text("SET LOCAL lock_timeout = '750ms'"))
        with pytest.raises(OperationalError):
            second.execute(sa_text("SELECT status FROM mainai_tasks WHERE id = CAST(:id AS uuid) FOR UPDATE"), {"id": task_id})
        second_tx.rollback()
        first_tx.commit()
    finally:
        second.close()
        first.close()


# --- Transaction rollback / retry ------------------------------------------------------------

def test_rollback_releases_the_lock_and_an_immediate_retry_sees_unchanged_state(superuser_db):
    """A dispatch attempt that fails and rolls back (e.g. an adapter error, a timeout) must not
    leave the task locked or mutated -- a retry immediately after must acquire the lock without
    delay and see the task exactly as it was."""
    owner = _owner(superuser_db)
    _goal, task = _approved_ready_task(superuser_db, owner.id)
    task_id = str(task.id)

    first = migration_engine.connect()
    try:
        first_tx = first.begin()
        first.execute(sa_text("SELECT status FROM mainai_tasks WHERE id = CAST(:id AS uuid) FOR UPDATE"), {"id": task_id})
        first_tx.rollback()
    finally:
        first.close()

    retry = migration_engine.connect()
    try:
        retry_tx = retry.begin()
        row = retry.execute(sa_text("SELECT status FROM mainai_tasks WHERE id = CAST(:id AS uuid) FOR UPDATE"), {"id": task_id}).first()
        assert row is not None and row[0] == "ready"
        retry_tx.rollback()
    finally:
        retry.close()


# --- The real production function, not just the mirrored primitive --------------------------

def test_the_real_gate_function_holds_the_lock_until_its_callers_transaction_ends(superuser_db):
    """Calls require_live_task_authorization_for_workforce_dispatch() for real (not a raw-SQL
    stand-in) and proves, via a genuinely separate connection, that the lock it took is still
    held after the call returns -- because the function itself never commits or rolls back
    `db`; that is the caller's responsibility, which is exactly what lets a future real-dispatch
    effect stay inside the same protected transaction as this check."""
    owner = _owner(superuser_db)
    _goal, task = _approved_ready_task(superuser_db, owner.id)
    assignment = _delegated_assignment(superuser_db, owner.id, task.id)

    reset_activation_gates_for_tests()
    for key in REQUIRED_ACTIVATION_GATES:
        record_gate_verification(key, status=GateStatus.verified, evidence_ref=f"test:{key}")

    # Real call -- succeeds (approved, live task), does not raise, does not commit.
    require_live_task_authorization_for_workforce_dispatch(superuser_db, owner_id=owner.id, assignment=assignment)

    blocker = migration_engine.connect()
    try:
        blocker_tx = blocker.begin()
        blocker.execute(sa_text("SET LOCAL lock_timeout = '750ms'"))
        with pytest.raises(OperationalError):
            blocker.execute(sa_text("SELECT status FROM mainai_tasks WHERE id = CAST(:id AS uuid) FOR UPDATE"), {"id": str(task.id)})
        blocker_tx.rollback()
    finally:
        blocker.close()

    superuser_db.rollback()  # release the real lock this test's own session is holding
    reset_activation_gates_for_tests()

    unblocked = migration_engine.connect()
    try:
        tx = unblocked.begin()
        row = unblocked.execute(sa_text("SELECT status FROM mainai_tasks WHERE id = CAST(:id AS uuid) FOR UPDATE"), {"id": str(task.id)}).first()
        assert row is not None
        tx.rollback()
    finally:
        unblocked.close()
