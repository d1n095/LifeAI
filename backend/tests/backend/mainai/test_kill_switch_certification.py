"""P0 certification: kill-switch owner/global, boot cannot clear, durable stop."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.models.user import User
from app.request_context import current_user_id as current_user_id_var
from app.workforce.kill_switch import (
    KillSwitchError,
    activate_global_emergency_stop,
    activate_owner_stop,
    assert_not_killed,
    clear_global_emergency_stop,
    clear_kill_switch_for_recovery,
    clear_owner_stop,
    query_stop_status,
    record_boot_blocked,
    reset_kill_switch_for_tests,
)
from app.mainai_executive.internal_start import run_first_real_internal_boot
from app.mainai_executive.safe_composed_run import run_composed_safe_internal_mainai_run


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture(autouse=True)
def _reset(superuser_db):
    reset_kill_switch_for_tests(superuser_db)
    superuser_db.execute(sa_text("UPDATE mainai_stop_state SET active=false, reason='', sequence=0"))
    superuser_db.flush()


def _owner(db, tag: str = "a"):
    u = User(email=f"ks-{tag}-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def test_boot_never_clears_active_stop(superuser_db):
    owner = _owner(superuser_db, "boot")
    activate_owner_stop(superuser_db, owner_id=owner.id, reason="operator_stop")
    with pytest.raises(KillSwitchError):
        clear_kill_switch_for_recovery(founder_ack="composed_safe_internal_clear")
    report = run_first_real_internal_boot(
        superuser_db, owner_email=owner.email, session_id=f"blocked-{uuid.uuid4()}"
    )
    assert report.blocked_by_kill_switch is True
    assert report.first_task_phase == "BLOCKED_BY_KILL_SWITCH"
    assert query_stop_status(superuser_db, owner_id=owner.id)["owner"]["active"] is True


def test_fabricated_ack_rejected(superuser_db):
    owner = _owner(superuser_db, "fab")
    activate_owner_stop(superuser_db, owner_id=owner.id, reason="x")
    with pytest.raises(KillSwitchError) as ei:
        clear_owner_stop(
            superuser_db,
            owner_id=owner.id,
            founder_ack="composed_safe_internal_clear",
            clear_request_id=uuid.uuid4(),
        )
    assert ei.value.code == "FABRICATED_ACK"


def test_owner_stop_isolation_two_owners(superuser_db):
    a = _owner(superuser_db, "iso-a")
    b = _owner(superuser_db, "iso-b")
    activate_owner_stop(superuser_db, owner_id=a.id, reason="stop_a")
    assert_not_killed(superuser_db, owner_id=b.id)  # B unaffected
    with pytest.raises(KillSwitchError):
        assert_not_killed(superuser_db, owner_id=a.id)
    activate_owner_stop(superuser_db, owner_id=b.id, reason="stop_b")
    with pytest.raises(KillSwitchError):
        assert_not_killed(superuser_db, owner_id=b.id)
    # Clear A only
    clear_owner_stop(
        superuser_db,
        owner_id=a.id,
        founder_ack="founder_ack:clear-owner-a-explicit",
        clear_request_id=uuid.uuid4(),
    )
    assert_not_killed(superuser_db, owner_id=a.id)
    with pytest.raises(KillSwitchError):
        assert_not_killed(superuser_db, owner_id=b.id)


def test_global_emergency_blocks_all(superuser_db):
    a = _owner(superuser_db, "g-a")
    b = _owner(superuser_db, "g-b")
    activate_global_emergency_stop(
        superuser_db,
        reason="system_emergency",
        founder_authority_ref="founder_ack:declare-global-stop",
    )
    with pytest.raises(KillSwitchError):
        assert_not_killed(superuser_db, owner_id=a.id)
    with pytest.raises(KillSwitchError):
        assert_not_killed(superuser_db, owner_id=b.id)
    # Clearing global does not invent owner authority (owners not auto-stopped)
    clear_global_emergency_stop(
        superuser_db,
        founder_ack="founder_ack:clear-global-stop",
        clear_request_id=uuid.uuid4(),
    )
    assert_not_killed(superuser_db, owner_id=a.id)
    assert_not_killed(superuser_db, owner_id=b.id)


def test_stale_clear_request_rejected(superuser_db):
    owner = _owner(superuser_db, "stale")
    st = activate_owner_stop(superuser_db, owner_id=owner.id, reason="r1")
    rid = uuid.uuid4()
    # Advance sequence by another activate
    activate_owner_stop(superuser_db, owner_id=owner.id, reason="r2")
    with pytest.raises(KillSwitchError) as ei:
        clear_owner_stop(
            superuser_db,
            owner_id=owner.id,
            founder_ack="founder_ack:stale-attempt",
            clear_request_id=rid,
            expected_sequence=st.sequence,
        )
    assert ei.value.code == "STALE_SEQUENCE"


def test_composed_run_surfaces_block_without_clear(superuser_db):
    owner = _owner(superuser_db, "comp")
    activate_owner_stop(superuser_db, owner_id=owner.id, reason="stop")
    report = run_composed_safe_internal_mainai_run(superuser_db, owner_id=owner.id)
    assert report.phase == "BLOCKED_BY_KILL_SWITCH"
    assert "BOOT_CANNOT_CLEAR_KILL_SWITCH" in report.authority_denials
    assert query_stop_status(superuser_db, owner_id=owner.id)["owner"]["active"] is True
