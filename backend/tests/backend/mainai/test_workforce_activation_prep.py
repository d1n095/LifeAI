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


def test_startup_readiness_blocking_list_does_not_drop_earlier_reasons(superuser_db, monkeypatch):
    """P1 bug (mainai_startup_readiness/__init__.py, PR #234, live on integration tip):
    the final `blocking` list at the READY_FOR_SAFE_INTERNAL_RUN level was OVERWRITTEN
    (not merged) with only ('provider_delegation_safety', 'claude_reviews'), silently
    dropping other real blockers (e.g. spend_controls:unknown) computed earlier in the
    function. Forces spend_controls to genuinely come back unknown (deleting the
    attribute the presence-check imports) to prove it, rather than assuming."""
    import app.provider_spend.service as provider_spend_service

    reset_activation_gates_for_tests()
    monkeypatch.delattr(provider_spend_service, "provider_spend_is_live", raising=False)

    report = evaluate_startup_readiness(claude_reviews_satisfied=None)
    assert report.level == ReadinessLevel.READY_FOR_SAFE_INTERNAL_RUN
    assert any("spend_controls" in b for b in report.blocking), (
        f"spend_controls:unknown must not be silently dropped from report.blocking, got {report.blocking}"
    )
    assert any("provider_delegation_safety" in b for b in report.blocking)
    reset_activation_gates_for_tests()


def test_blocking_migrations_check_is_real_not_hardcoded(superuser_db):
    """P1 bug (mainai_startup_readiness/__init__.py, PR #234): the "blocking_migrations"
    check was a bare ReadinessCheck(..., CheckStatus.healthy, "...verify ops") -- a claim
    with no check ever run. Now backed by a real Alembic single-head structural check."""
    report = evaluate_startup_readiness(claude_reviews_satisfied=None)
    check = next(c for c in report.checks if c.key == "blocking_migrations")
    assert check.status.value == "healthy"
    assert "verify ops" not in check.detail  # old hardcoded placeholder text is gone
    assert "head" in check.detail


def test_activation_commit_status_wires_claude_reviews_from_gates(superuser_db):
    """P1 bug (workforce/activation_commit.py, PR #234, live on integration tip):
    activation_commit_status() hardcoded claude_reviews_satisfied=None when calling
    evaluate_startup_readiness(), which forces the claude_reviews check to
    CheckStatus.unknown and caps readiness.level at READY_FOR_SAFE_INTERNAL_RUN forever
    -- so ready_to_enable_after_claude could never become True regardless of actual
    verified state."""
    from app.workforce.activation_commit import activation_commit_status

    reset_activation_gates_for_tests()
    status_before = activation_commit_status()
    assert status_before["readiness_level"] == "READY_FOR_SAFE_INTERNAL_RUN"

    for key in REQUIRED_ACTIVATION_GATES:
        record_gate_verification(key, status=GateStatus.verified, evidence_ref=f"ev:{key}")
    status_after = activation_commit_status()
    assert status_after["readiness_level"] in (
        "READY_FOR_LOW_RISK_PROVIDER_RUN",
        "READY_FOR_SERIOUS_AUTONOMOUS_RUN",
    ), f"expected a higher readiness level once every gate is verified, got {status_after}"
    reset_activation_gates_for_tests()


def test_department_ledger_requires_durable_evidence_not_one_lucky_success(superuser_db):
    """P1 bug (workforce/department_evidence.py, PR #234, live on integration tip):
    department_capability_ledger() marked a capability "verified" (evidence_proves_
    capability=True) from a single verified success, regardless of how many verified
    failures came with it (rate > 0 is trivially true for ANY nonzero success count).
    Durable evidence requires both a real sample size and a strong success rate."""
    from app.workforce import get_workforce_agent_by_key, record_verified_outcome
    from app.workforce.first_team import bootstrap_first_team

    owner = _owner(superuser_db)
    bootstrap_first_team(superuser_db, owner_id=owner.id)
    superuser_db.commit()
    profile = get_workforce_agent_by_key(superuser_db, owner_id=owner.id, agent_key="dept-research")

    # One lucky success, mostly failures -- must NOT count as proven.
    record_verified_outcome(
        superuser_db, owner_id=owner.id, profile_id=profile.id,
        capability_tag="web_research", success=True, quality_score=0.9,
    )
    for _ in range(4):
        record_verified_outcome(
            superuser_db, owner_id=owner.id, profile_id=profile.id,
            capability_tag="web_research", success=False, quality_score=0.1,
        )
    superuser_db.commit()
    rows = department_capability_ledger(superuser_db, owner_id=owner.id)
    superuser_db.commit()
    research_row = next(r for r in rows if r["agent_key"] == "dept-research")
    assert "web_research" not in research_row["verified_capabilities"], (
        "1 success out of 5 verified trials must not prove the capability"
    )
    assert research_row["evidence_proves_capability"] is False

    # A genuinely durable track record (>=3 trials, strong majority) DOES count --
    # a separate capability_tag so this half of the test doesn't have to first
    # overcome the poor rollup the flaky-evidence half above deliberately built.
    for _ in range(3):
        record_verified_outcome(
            superuser_db, owner_id=owner.id, profile_id=profile.id,
            capability_tag="fact_gathering", success=True, quality_score=0.9,
        )
    superuser_db.commit()
    rows2 = department_capability_ledger(superuser_db, owner_id=owner.id)
    superuser_db.commit()
    research_row2 = next(r for r in rows2 if r["agent_key"] == "dept-research")
    assert "fact_gathering" in research_row2["verified_capabilities"]
    assert research_row2["evidence_proves_capability"] is True
    # The flaky capability from above must still not be verified.
    assert "web_research" not in research_row2["verified_capabilities"]
