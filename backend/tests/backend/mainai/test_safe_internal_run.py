"""First SAFE INTERNAL MainAI run + kill-switch + audit receipt proofs."""

from __future__ import annotations

import uuid

import pytest

from app.models.user import User
from app.workforce import (
    KillSwitchError,
    PROVIDER_INVOKE_ENABLED,
    activation_commit_status,
    assert_provider_invoke_disabled,
    clear_kill_switch_for_recovery,
    get_kill_switch,
    prove_no_reusable_live_authority,
    register_workforce_agent,
    reset_activation_gates_for_tests,
    reset_kill_switch_for_tests,
    resolve_delegation,
    run_first_safe_internal_mainai_run,
    submit_delegation_request,
)


def _owner(db):
    u = User(email=f"safe-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def test_safe_internal_run_end_to_end(superuser_db):
    reset_kill_switch_for_tests(superuser_db)
    reset_activation_gates_for_tests()
    owner = _owner(superuser_db)
    report = run_first_safe_internal_mainai_run(superuser_db, owner_id=owner.id)
    superuser_db.commit()
    d = report.as_dict()
    assert d["provider_invoked"] is False
    assert d["consequential_writes"] is False
    assert d["escalated_to_provider"] is False
    assert d["restart_ok"] is True
    assert d["kill_switch_armed"] is True
    assert d["readiness_level"] == "READY_FOR_SAFE_INTERNAL_RUN"
    receipt = d["task_receipt"]
    assert receipt["selected_department"] == "Research"
    assert receipt["verification"]["final_status"] == "VERIFIED"
    assert "api_key" not in receipt["context_disclosed"]
    assert prove_no_reusable_live_authority(superuser_db, owner_id=owner.id) is True


def test_kill_switch_blocks_further_execution(superuser_db):
    reset_kill_switch_for_tests(superuser_db)
    reset_activation_gates_for_tests()
    owner = _owner(superuser_db)
    run_first_safe_internal_mainai_run(superuser_db, owner_id=owner.id)
    assert get_kill_switch(superuser_db, owner.id).active is True
    b = register_workforce_agent(
        superuser_db,
        owner_id=owner.id,
        agent_key="post-kill",
        name="X",
        role="builder",
        agent_type="CODING",
        capability_tags=["low_risk_classification"],
        status="active",
        trust_zone="LOCAL_INTERNAL",
        allowed_tool_classes=["read_excerpt"],
    )
    v = register_workforce_agent(
        superuser_db,
        owner_id=owner.id,
        agent_key="post-kill-v",
        name="V",
        role="verifier",
        agent_type="VERIFIER",
        capability_tags=["verification"],
        status="active",
        trust_zone="LOCAL_INTERNAL",
    )
    # New assignment while kill switch on — GRANT must now fail closed (the authority-
    # widening race fix: assert_grant_allowed() refuses at broker.resolve_delegation()
    # itself, not just at execution time).
    req = submit_delegation_request(
        superuser_db,
        owner_id=owner.id,
        goal_text="after kill",
        required_capability="low_risk_classification",
        verification_requirement="independent_verifier",
    )
    with pytest.raises(KillSwitchError):
        resolve_delegation(superuser_db, owner_id=owner.id, request=req, verifier_profile_id=v.id)
    # Deliberately NOT rolling back here: nothing in this test has been committed yet (the
    # owner/agents/kill-switch-armed state above all live only in this session's still-open
    # transaction), so a rollback would discard them too, not just the refused grant's
    # partial writes -- assert_grant_allowed() raises a plain Python exception before any
    # DB-level error, so the transaction itself is not aborted and is safe to keep using.

    clear_kill_switch_for_recovery(superuser_db, founder_ack="founder-ack-test", owner_id=owner.id)
    superuser_db.commit()
    assert get_kill_switch(superuser_db, owner.id).active is False


def test_kill_switch_is_owner_scoped_not_cross_owner_dos(superuser_db):
    """P1 bug (workforce/kill_switch.py, PR #234, live on integration tip):
    activate_kill_switch(db, owner_id=OWNER_A, reason=...) correctly revokes only OWNER_A's
    live assignments, but the module-level `_STATE` flag was a single process-global -- so
    assert_not_killed() (no owner argument) raised KillSwitchError for a completely
    unrelated OWNER_B too. Since run_first_safe_internal_mainai_run() unconditionally calls
    activate_kill_switch() at the end of every successful run, ANY single owner completing
    a run silently disabled workforce execution for EVERY other owner sharing the process --
    a real cross-owner denial-of-service via a legitimate, expected code path."""
    from app.workforce.kill_switch import assert_not_killed

    reset_kill_switch_for_tests(superuser_db)
    reset_activation_gates_for_tests()
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    superuser_db.commit()

    run_first_safe_internal_mainai_run(superuser_db, owner_id=owner_a.id)
    superuser_db.commit()

    assert get_kill_switch(superuser_db, owner_a.id).active is True, "owner A's own kill switch must be active"

    with pytest.raises(KillSwitchError):
        assert_not_killed(superuser_db, owner_a.id)

    # The actual bug: owner B was never touched by owner A's kill switch, so owner B's
    # workforce execution must NOT be blocked.
    assert get_kill_switch(superuser_db, owner_b.id).active is False, "owner B must be unaffected by owner A's kill switch"
    assert_not_killed(superuser_db, owner_b.id)  # must NOT raise

    reset_kill_switch_for_tests(superuser_db)


def test_activation_commit_disabled_by_default(superuser_db):
    assert PROVIDER_INVOKE_ENABLED is False
    assert_provider_invoke_disabled()
    status = activation_commit_status()
    assert status["provider_invoke_enabled"] is False
    assert status["ready_to_enable_after_claude"] is False  # gates unknown
