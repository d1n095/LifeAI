"""Founder security review, 2026-11-14, Priority 1 + 2: "No path may bypass the gate by
invoking a lower-level adapter directly" and "An environment variable alone must not
constitute production activation authority." This file proves both, against real coordination
state and a real `system_activation_authorizations` row, never a mock.

`current_system_activation()` tests are pure service-layer tests. The dispatch-bypass test is
the actual security proof: it constructs a `LocalCLIAdapter` DIRECTLY (not via
`get_real_adapter()`) and hands it to `dispatch_assignment()` -- the exact shape of the bypass
an earlier pass of this gate left open, confirmed closed here."""

import uuid
from datetime import datetime, timedelta

import pytest

from app.agent_coordination.activation_gate import SystemActivationNotAuthorizedError
from app.agent_coordination.adapters import LocalCLIAdapter
from app.agent_coordination.dispatch import OUTCOME_ASSIGNABLE, dispatch_assignment
from app.agent_coordination.service import create_work_assignment, register_agent
from app.agent_coordination.system_activation import current_system_activation
from app.mainai_execution.planner import create_goal
from app.models.agent_coordination import SystemActivationAuthorization
from app.models.user import User


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure as every other agent_coordination test module's own
    identical fixture -- required here specifically to exercise the real, boot-verified
    REVOKE on system_activation_authorizations (migration 0069), not just the model/ORM
    layer."""
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _owner(db) -> User:
    user = User(email=f"sysact-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _agent(db, key: str):
    return register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", display_name=key, adapter_kind="cli")


# ============================================================================ current_system_activation():
# missing / active / expired / revoked

def test_current_system_activation_is_none_when_no_row_exists(superuser_db):
    assert current_system_activation(superuser_db) is None


def test_current_system_activation_returns_an_active_unexpiring_row(superuser_db):
    superuser_db.add(SystemActivationAuthorization(authorized_by="founder", reason="test"))
    superuser_db.commit()

    current = current_system_activation(superuser_db)
    assert current is not None
    assert current.authorized_by == "founder"


def test_current_system_activation_is_none_once_expired(superuser_db):
    superuser_db.add(
        SystemActivationAuthorization(
            authorized_by="founder", reason="test", expires_at=datetime.utcnow() - timedelta(seconds=1)  # noqa: DTZ003 -- naive UTC, matching the column
        )
    )
    superuser_db.commit()

    assert current_system_activation(superuser_db) is None


def test_current_system_activation_sees_a_not_yet_expired_row(superuser_db):
    superuser_db.add(
        SystemActivationAuthorization(
            authorized_by="founder", reason="test", expires_at=datetime.utcnow() + timedelta(hours=1)  # noqa: DTZ003 -- naive UTC, matching the column
        )
    )
    superuser_db.commit()

    assert current_system_activation(superuser_db) is not None


def test_current_system_activation_is_none_once_revoked(superuser_db):
    superuser_db.add(
        SystemActivationAuthorization(
            authorized_by="founder", reason="test", status="revoked",
            revoked_at=datetime.utcnow(), revoked_by="founder",  # noqa: DTZ003 -- naive UTC, matching the column
        )
    )
    superuser_db.commit()

    assert current_system_activation(superuser_db) is None


def test_current_system_activation_reflects_a_revocation_immediately_not_cached(superuser_db):
    """The 'concurrent changes and stale authorization' scenario the audit asked to test: a
    row that WAS valid stops being valid the moment it's revoked, with no caching anywhere in
    between -- the same session, same object, re-queried."""
    row = SystemActivationAuthorization(authorized_by="founder", reason="test")
    superuser_db.add(row)
    superuser_db.commit()
    assert current_system_activation(superuser_db) is not None

    row.status = "revoked"
    row.revoked_at = datetime.utcnow()  # noqa: DTZ003 -- naive UTC, matching the column
    row.revoked_by = "founder"
    superuser_db.commit()

    assert current_system_activation(superuser_db) is None


# ============================================================================ mainai_app cannot
# write this table at all -- the database-level half of "an env var alone is not authority"

def test_mainai_app_role_cannot_insert_a_system_activation_authorization(db_session):
    """`db_session` is bound to the restricted `mainai_app` role (see conftest.py) -- the same
    role every real request/worker uses. Confirms migration 0069's REVOKE is real, not just
    documented: even a straightforward INSERT from the app's own normal connection is
    rejected by Postgres itself."""
    db_session.add(SystemActivationAuthorization(authorized_by="attacker", reason="self-authorize"))
    with pytest.raises(Exception):  # noqa: B017 -- the exact exception varies by Postgres driver/error path, same as every sibling RLS-rejection test in this codebase
        db_session.commit()
    db_session.rollback()


# ============================================================================ the actual bypass
# proof: a directly-constructed LocalCLIAdapter, never touching get_real_adapter(), still
# cannot execute without both FIRST_CYCLE_GO and a current system_activation row.

def _ready_assignment(db, *, owner_id, cwd):
    """Returns an assignment already at `ready` -- `create_work_assignment()`'s own
    `_refresh_dependency_state()` call promotes a dependency-free assignment there
    automatically (see its docstring; this is NOT `planned`). `dispatch_assignment()` itself
    owns the `ready -> waiting_agent -> running` transition, so this helper must never advance
    the assignment past `ready` itself -- doing so would make `agent_availability()`'s own
    concurrency count see this assignment as already active and reject it as
    `AGENT_UNAVAILABLE` before the gate under test is ever reached."""
    goal = create_goal(db, owner_id=owner_id, title="Bypass test goal", original_instruction="x", created_by="test")
    agent = _agent(db, "bypass-test")
    assignment = create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=None, agent_id=agent.id, role="builder", read_write_mode="read_only",
        repository_identity="lifeai", allowed_paths=["backend/app/**"], requested_by="test",
    )
    db.commit()
    return assignment, agent


@pytest.mark.asyncio
async def test_a_directly_constructed_adapter_handed_to_dispatch_assignment_still_cannot_run_without_authorization(
    superuser_db, tmp_path, monkeypatch
):
    """THE bypass proof: never calls get_real_adapter() at all -- constructs LocalCLIAdapter
    directly (exactly the shape of the gap a security review found: dispatch_assignment()
    takes an adapter as a caller-supplied parameter and never calls the factory itself) and
    hands it straight to dispatch_assignment(). With no FIRST_CYCLE_GO and no DB evidence,
    this must still fail -- proving the gate now lives at the one place with no lower level to
    invoke around."""
    monkeypatch.delenv("FIRST_CYCLE_GO", raising=False)
    owner = _owner(superuser_db)
    superuser_db.commit()
    assignment, agent = _ready_assignment(superuser_db, owner_id=owner.id, cwd=str(tmp_path))

    adapter = LocalCLIAdapter(
        provider_key="bypass-test", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5, db=superuser_db,
    )
    # dispatch_assignment() marks the assignment `blocked` with a structured reason and then
    # RE-RAISES any unanticipated adapter exception (its own documented contract) -- it never
    # swallows this into a DispatchDecision outcome, and never silently reports success.
    with pytest.raises(SystemActivationNotAuthorizedError):
        await dispatch_assignment(superuser_db, assignment=assignment, agent=agent, adapter=adapter)

    superuser_db.refresh(assignment)
    assert assignment.status.value == "blocked"


@pytest.mark.asyncio
async def test_a_directly_constructed_adapter_succeeds_once_both_conditions_are_met(superuser_db, tmp_path, monkeypatch):
    """The positive control: the SAME direct-construction path, but with both FIRST_CYCLE_GO
    and a current system_activation row present, genuinely succeeds -- proving the gate is
    additive, not an unconditional block."""
    monkeypatch.setenv("FIRST_CYCLE_GO", "true")
    superuser_db.add(SystemActivationAuthorization(authorized_by="founder", reason="test"))
    superuser_db.commit()

    owner = _owner(superuser_db)
    superuser_db.commit()
    assignment, agent = _ready_assignment(superuser_db, owner_id=owner.id, cwd=str(tmp_path))

    adapter = LocalCLIAdapter(
        provider_key="bypass-test", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5, db=superuser_db,
    )
    decision = await dispatch_assignment(superuser_db, assignment=assignment, agent=agent, adapter=adapter)

    assert decision.outcome == OUTCOME_ASSIGNABLE
    superuser_db.refresh(assignment)
    assert assignment.status.value == "running"


@pytest.mark.asyncio
async def test_env_var_alone_without_db_evidence_is_not_sufficient(superuser_db, tmp_path, monkeypatch):
    """Priority 2's own literal requirement: FIRST_CYCLE_GO=true with NO
    system_activation_authorizations row must still refuse -- an environment variable alone is
    not authority."""
    monkeypatch.setenv("FIRST_CYCLE_GO", "true")
    # Deliberately no SystemActivationAuthorization row inserted.

    adapter = LocalCLIAdapter(
        provider_key="env-only-test", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5, db=superuser_db,
    )
    with pytest.raises(SystemActivationNotAuthorizedError) as exc_info:
        await adapter.start_assignment(uuid.uuid4())
    assert "system_activation_authorizations" in str(exc_info.value)


@pytest.mark.asyncio
async def test_db_evidence_alone_without_env_var_is_not_sufficient(superuser_db, tmp_path, monkeypatch):
    """The mirror case: a valid DB row with NO FIRST_CYCLE_GO env var must still refuse --
    neither condition is sufficient alone, only both together."""
    monkeypatch.delenv("FIRST_CYCLE_GO", raising=False)
    superuser_db.add(SystemActivationAuthorization(authorized_by="founder", reason="test"))
    superuser_db.commit()

    adapter = LocalCLIAdapter(
        provider_key="db-only-test", executable="/usr/bin/true", args_template=(), cwd=str(tmp_path), timeout_seconds=5, db=superuser_db,
    )
    with pytest.raises(SystemActivationNotAuthorizedError) as exc_info:
        await adapter.start_assignment(uuid.uuid4())
    assert "FIRST_CYCLE_GO" in str(exc_info.value)
