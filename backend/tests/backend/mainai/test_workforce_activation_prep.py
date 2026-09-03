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
    """Blocker lists must accumulate — never overwrite (tip bug in PR #234)."""
    import app.provider_spend.service as provider_spend_service
    from app.mainai_startup_readiness import receipts as readiness_receipts
    from app.mainai_startup_readiness.receipts import CheckStatus, ReadinessCheck

    reset_activation_gates_for_tests()
    monkeypatch.delattr(provider_spend_service, "provider_spend_is_live", raising=False)

    report_blocked = evaluate_startup_readiness(claude_reviews_satisfied=None, db=superuser_db)
    assert any("spend_controls" in b for b in report_blocked.blocking), (
        f"spend_controls must not be silently dropped, got {report_blocked.blocking}"
    )

    real_check = readiness_receipts._check_import

    def _spend_unknown(key: str, import_path: str):
        if key == "spend_controls":
            return ReadinessCheck(key, CheckStatus.unknown, "forced unknown for accumulation test")
        return real_check(key, import_path)

    monkeypatch.setattr(readiness_receipts, "_check_import", _spend_unknown)
    report = evaluate_startup_readiness(claude_reviews_satisfied=None, db=superuser_db)
    assert report.level == ReadinessLevel.READY_FOR_SAFE_INTERNAL_RUN
    assert any("spend_controls" in b for b in report.blocking), (
        f"spend_controls:unknown must not be silently dropped from report.blocking, got {report.blocking}"
    )
    assert any(
        "provider_delegation" in b or "claude_reviews" in b or "activation_gates" in b
        for b in report.blocking
    ), f"provider/claude blockers missing from {report.blocking}"
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


def test_migration_unknown_status_blocks_readiness(superuser_db, monkeypatch):
    """P0 closeout: UNKNOWN SAFETY STATE != SAFE. A migration verification that returns
    unknown (e.g. subprocess/inspection failure, not a clean unhealthy multi-head result)
    must block readiness exactly like unhealthy does -- previously only unhealthy blocked,
    letting an unknown migration state silently proceed to a "ready" level."""
    import app.mainai_startup_readiness.receipts as receipts_mod

    def _fake_unknown(db=None):
        return receipts_mod.ReadinessCheck(
            "blocking_migrations", receipts_mod.CheckStatus.unknown, "simulated verification error"
        )

    monkeypatch.setattr(receipts_mod, "verify_migration_head", _fake_unknown)
    report = evaluate_startup_readiness(claude_reviews_satisfied=None, db=superuser_db)
    assert report.level == ReadinessLevel.BLOCKED, f"expected BLOCKED, got {report.level} blocking={report.blocking}"
    assert any("blocking_migrations" in b for b in report.blocking), report.blocking


def test_kill_switch_unknown_status_blocks_readiness(superuser_db):
    """Same invariant for kill_switch_health: no db session -> unknown -> must block, not
    silently pass through to a ready level."""
    report = evaluate_startup_readiness(claude_reviews_satisfied=None, db=None)
    check = next(c for c in report.checks if c.key == "kill_switch_health")
    assert check.status.value == "unknown"
    assert report.level == ReadinessLevel.BLOCKED, f"expected BLOCKED, got {report.level} blocking={report.blocking}"
    assert any("kill_switch_health" in b for b in report.blocking), report.blocking


def test_multi_head_migration_collision_blocks_overall_readiness(superuser_db, monkeypatch):
    """The exact scenario proven broken tonight: a genuine multi-head Alembic collision
    must not just be individually detected -- it must actually BLOCK the overall readiness
    level, not merely be reported while readiness still reaches a ready tier."""
    import app.mainai_startup_readiness.receipts as receipts_mod

    def _fake_multi_head(db=None):
        return receipts_mod.ReadinessCheck(
            "blocking_migrations",
            receipts_mod.CheckStatus.unhealthy,
            "expected single alembic head, got ['0069', '0070-conflict']",
            evidence={"heads": ["0069", "0070-conflict"]},
        )

    monkeypatch.setattr(receipts_mod, "verify_migration_head", _fake_multi_head)
    report = evaluate_startup_readiness(claude_reviews_satisfied=None, db=superuser_db)
    assert report.level == ReadinessLevel.BLOCKED, f"expected BLOCKED, got {report.level} blocking={report.blocking}"
    assert "blocking_migrations" in report.blocking


def test_activation_commit_status_wires_claude_reviews_from_gates(superuser_db):
    """#240 fix: do not hardcode claude_reviews_satisfied=None.

    Canonical #237 receipts still require durable evidence_ref + spend receipt before
    READY_FOR_LOW_RISK_PROVIDER_RUN. Prove gate wiring + receipt attach, then promotion
    when spend_controls_verified receipt is also present.
    """
    from app.workforce.activation_commit import activation_commit_status
    from app.mainai_startup_readiness import evaluate_startup_readiness

    reset_activation_gates_for_tests()
    status_before = activation_commit_status()
    assert status_before["readiness_level"] in (
        "BLOCKED",
        "READY_FOR_SAFE_INTERNAL_RUN",
    )

    for key in REQUIRED_ACTIVATION_GATES:
        record_gate_verification(key, status=GateStatus.verified, evidence_ref=f"ev:{key}")
    status_after = activation_commit_status()
    assert status_after["gates_allowed"] is True
    assert status_after.get("receipts", {}).get("claude_reviews_evidence_ref")
    # Full provider-tier promotion still needs spend receipt (IMPORTABLE != HEALTHY).
    promoted = evaluate_startup_readiness(
        claude_reviews_satisfied=True,
        db=superuser_db,
        receipts={
            "claude_reviews_evidence_ref": status_after["receipts"]["claude_reviews_evidence_ref"],
            "spend_controls_verified": True,
        },
    )
    assert promoted.level in (
        ReadinessLevel.READY_FOR_LOW_RISK_PROVIDER_RUN,
        ReadinessLevel.READY_FOR_SERIOUS_AUTONOMOUS_RUN,
    ), f"expected provider-tier with spend receipt, got {promoted.level} blocking={promoted.blocking}"
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
