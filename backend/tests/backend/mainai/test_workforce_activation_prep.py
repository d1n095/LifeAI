"""Activation gates, startup readiness, first low-risk delegation scenario."""

from __future__ import annotations

import uuid

import pytest

from app.mainai_startup_readiness import ReadinessLevel, evaluate_startup_readiness
from app.models.user import User
from app.workforce import (
    ActivationGateSet,
    GateStatus,
    ProviderActivationBlocked,
    department_capability_ledger,
    get_activation_gates,
    record_gate_verification,
    require_activation_allowed,
    reset_activation_gates_for_tests,
    run_low_risk_public_text_delegation,
    run_systemic_research_learning_loop,
)
from app.workforce.activation_gates import REQUIRED_ACTIVATION_GATES


def _owner(db):
    u = User(email=f"act-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def test_unknown_gates_fail_closed(superuser_db):
    reset_activation_gates_for_tests()
    d = get_activation_gates().evaluate()
    assert d.allowed is False
    assert len(d.unknown) == len(REQUIRED_ACTIVATION_GATES)
    with pytest.raises(ProviderActivationBlocked):
        require_activation_allowed()
    # verified without evidence_ref rejected
    with pytest.raises(ValueError):
        record_gate_verification("pr_218_consequential_confirmation", status=GateStatus.verified)


def test_all_gates_verified_still_stages_invoke(superuser_db):
    reset_activation_gates_for_tests()
    for key in REQUIRED_ACTIVATION_GATES:
        record_gate_verification(key, status=GateStatus.verified, evidence_ref=f"test:{key}")
    assert get_activation_gates().evaluate().allowed is True
    # Staging still blocks actual invoke path — covered via execute with activate_provider
    from app.workforce import register_workforce_agent, submit_delegation_request, resolve_delegation, execute_workforce_assignment

    owner = _owner(superuser_db)
    b = register_workforce_agent(
        superuser_db,
        owner_id=owner.id,
        agent_key="b",
        name="B",
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
        agent_key="v",
        name="V",
        role="verifier",
        agent_type="VERIFIER",
        capability_tags=["verification"],
        status="active",
        trust_zone="LOCAL_INTERNAL",
    )
    req = submit_delegation_request(
        superuser_db,
        owner_id=owner.id,
        goal_text="x",
        required_capability="low_risk_classification",
        verification_requirement="independent_verifier",
    )
    asg = resolve_delegation(superuser_db, owner_id=owner.id, request=req, verifier_profile_id=v.id)
    with pytest.raises(ProviderActivationBlocked):
        execute_workforce_assignment(
            superuser_db,
            owner_id=owner.id,
            assignment=asg,
            goal_text="x",
            capability="low_risk_classification",
            activate_provider=True,
        )
    reset_activation_gates_for_tests()


def test_startup_readiness_levels_not_one_boolean(superuser_db):
    reset_activation_gates_for_tests()
    report = evaluate_startup_readiness(claude_reviews_satisfied=None, db=superuser_db)
    assert report.level == ReadinessLevel.READY_FOR_SAFE_INTERNAL_RUN
    assert report.as_dict()["invariant"] == "NEVER_COLLAPSE_TO_ONE_BOOLEAN"
    # Claude True alone must NOT unlock provider tier without durable evidence
    for key in REQUIRED_ACTIVATION_GATES:
        record_gate_verification(key, status=GateStatus.verified, evidence_ref=f"ev:{key}")
    report2 = evaluate_startup_readiness(claude_reviews_satisfied=True, db=superuser_db)
    assert report2.level == ReadinessLevel.READY_FOR_SAFE_INTERNAL_RUN
    assert any("claude_reviews" in b for b in report2.blocking)
    # With durable evidence_ref → may reach provider tier
    report3 = evaluate_startup_readiness(
        claude_reviews_satisfied=True,
        db=superuser_db,
        receipts={"claude_reviews_evidence_ref": "claude-pr-review:example"},
    )
    assert report3.level in (
        ReadinessLevel.READY_FOR_LOW_RISK_PROVIDER_RUN,
        ReadinessLevel.READY_FOR_SERIOUS_AUTONOMOUS_RUN,
        ReadinessLevel.READY_FOR_SAFE_INTERNAL_RUN,  # spend may still block
    )
    reset_activation_gates_for_tests()


def test_first_low_risk_delegation_selects_by_evidence(superuser_db):
    owner = _owner(superuser_db)
    record = run_low_risk_public_text_delegation(
        superuser_db,
        owner_id=owner.id,
        public_text="The city library opens at nine on weekdays.",
        activate_provider=False,
    )
    superuser_db.commit()
    assert record.selected_agent_key == "research-strong"
    assert record.selection_explanation.get("used_agent_self_confidence") is False
    assert "api_key" not in record.disclosed_kinds
    assert record.raw_verification_status == "UNVERIFIED"
    assert record.final_verification_status == "VERIFIED"
    assert record.provider_invoked is False
    assert record.outcome == "verified_incorporated"


def test_systemic_loop_learns_without_granting_authority(superuser_db):
    owner = _owner(superuser_db)
    result = run_systemic_research_learning_loop(
        superuser_db,
        owner_id=owner.id,
        public_text="Open-source software documentation excerpt.",
    )
    superuser_db.commit()
    assert result["past_success_grants_authority"] is False
    assert result["selection_still_evidence_based"] is True
    assert result["learned_preference"] == "research-strong"


def test_department_ledger_does_not_fake_promotion(superuser_db):
    owner = _owner(superuser_db)
    rows = department_capability_ledger(superuser_db, owner_id=owner.id)
    superuser_db.commit()
    assert len(rows) == 9
    for row in rows:
        assert row["do_not_promote_without_evidence"] is True
        assert row["evidence_proves_capability"] is False
        assert row["candidate"] is True
