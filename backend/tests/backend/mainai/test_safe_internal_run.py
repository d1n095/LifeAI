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
    execute_workforce_assignment,
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
    reset_kill_switch_for_tests()
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
    reset_kill_switch_for_tests()
    reset_activation_gates_for_tests()
    owner = _owner(superuser_db)
    run_first_safe_internal_mainai_run(superuser_db, owner_id=owner.id)
    assert get_kill_switch().active is True
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
    # New assignment while kill switch on — execute must fail closed
    req = submit_delegation_request(
        superuser_db,
        owner_id=owner.id,
        goal_text="after kill",
        required_capability="low_risk_classification",
        verification_requirement="independent_verifier",
    )
    # Selector may fail if no matching agent with evidence — register capability already
    asg = resolve_delegation(superuser_db, owner_id=owner.id, request=req, verifier_profile_id=v.id)
    with pytest.raises(KillSwitchError):
        execute_workforce_assignment(
            superuser_db,
            owner_id=owner.id,
            assignment=asg,
            goal_text="after kill",
            capability="low_risk_classification",
        )
    clear_kill_switch_for_recovery(founder_ack="founder-ack-test")
    assert get_kill_switch().active is False


def test_activation_commit_disabled_by_default(superuser_db):
    assert PROVIDER_INVOKE_ENABLED is False
    assert_provider_invoke_disabled()
    status = activation_commit_status()
    assert status["provider_invoke_enabled"] is False
    assert status["ready_to_enable_after_claude"] is False  # gates unknown
