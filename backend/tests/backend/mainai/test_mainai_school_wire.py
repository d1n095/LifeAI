"""School wire + evidence hierarchy + composed safe internal (no API)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.mainai_executive import (
    ExecutivePhase,
    run_composed_safe_internal_mainai_run,
    run_executive_cycle,
)
from app.mainai_school import INVARIANTS, reset_metrics_for_tests
from app.mainai_school.evidence import (
    EvidenceItem,
    EvidenceRank,
    resolve_local_vs_teacher,
)
from app.mainai_school.teacher_ledger import (
    adjudicate_multi_teacher,
    plan_teacher_consultation,
    record_teacher_outcome,
    reset_teacher_ledger_for_tests,
    teacher_diversity_ok,
)
from app.mainai_school.types import LocalAttempt, TeacherCritique
from app.mainai_school.cycle import run_learning_cycle
from app.models.user import User
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True)
def _reset():
    reset_metrics_for_tests()
    reset_teacher_ledger_for_tests()


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _owner(db):
    u = User(email=f"wire-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    current_user_id_var.set(str(u.id))
    db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(u.id)})
    return u


def test_invariants_no_answer_key():
    assert "TEACHER_IS_NOT_TRUTH" in INVARIANTS
    assert "NO_SINGLE_API_ANSWER_KEY" in INVARIANTS
    assert "MODEL_CONSENSUS_IS_NOT_TRUTH" in INVARIANTS


def test_executive_wires_school_local_first(superuser_db):
    owner = _owner(superuser_db)
    result = run_executive_cycle(
        superuser_db,
        owner_id=owner.id,
        founder_request="Classify public library hours notice",
        session_id=f"wire-{uuid.uuid4()}",
        need_capability="low_risk_classification",
        run_workforce_dry=True,
    )
    assert result.phase == ExecutivePhase.CONTINUE
    assert result.school_path is not None
    assert result.school_path["local_attempt_first"] is True
    assert result.school_path["teacher_invoked"] is False
    assert result.school_path["provider_as_default_execution"] is False
    assert result.workforce_dry_run["provider_invoked"] is False
    assert result.workforce_dry_run.get("local_attempt_first") is True
    assert "TEACHER_IS_NOT_TRUTH" in result.authority_denials
    assert "EXTERNAL_MODEL_IS_NOT_MAINAI" in result.authority_denials


def test_composed_safe_internal_executive_school(superuser_db):
    owner = _owner(superuser_db)
    report = run_composed_safe_internal_mainai_run(
        superuser_db,
        owner_id=owner.id,
        founder_request="Public notice: museum open weekends.",
    )
    d = report.as_dict()
    assert d["provider_invoked"] is False
    assert d["api_dependency_required"] is False
    assert d["school_wired"] is True
    assert d["local_attempt_first"] is True
    assert d["teacher_invoked"] is False
    assert d["restart_ok"] is True
    assert d["offline_ok"] is True


def test_teacher_wrong_local_right_deterministic():
    evidence = [
        EvidenceItem(
            rank=EvidenceRank.DETERMINISTIC_TEST,
            summary="pytest passed for local claim",
            supports_local=True,
        ),
        EvidenceItem(
            rank=EvidenceRank.MODEL_OPINION,
            summary="teacher said B",
            supports_local=False,
        ),
    ]
    v = resolve_local_vs_teacher(
        local_claim="A",
        teacher_claim="B",
        evidence=evidence,
    )
    assert v.winner == "local"
    assert v.teacher_overruled is True
    record_teacher_outcome(
        teacher_id="claude-sim",
        domain="coding",
        later_verified_correct=False,
        local_was_right_teacher_wrong=True,
    )


def test_three_teachers_agree_but_primary_disproves():
    evidence = [
        EvidenceItem(
            rank=EvidenceRank.PRIMARY_SOURCE,
            summary="statute says otherwise",
            supports_local=True,
        )
    ]
    out = adjudicate_multi_teacher(
        local_claim="legal_position_A",
        teacher_positions=[
            {"teacher_id": "claude-1", "answer": "B"},
            {"teacher_id": "openai-1", "answer": "B"},
            {"teacher_id": "grok-1", "answer": "B"},
        ],
        evidence=evidence,
    )
    assert out["majority_vote_used"] is False
    assert out["three_models_agree_is_not_automatically_true"] is True
    assert out["verdict"]["winner"] == "local"
    assert out["verdict"]["teacher_overruled"] is True


def test_model_consensus_alone_unresolved():
    evidence = [
        EvidenceItem(
            rank=EvidenceRank.MODEL_OPINION,
            summary="all models say B",
            supports_local=False,
        )
    ]
    v = resolve_local_vs_teacher(
        local_claim="A",
        teacher_claim="B",
        evidence=evidence,
        teachers_agree=True,
    )
    assert v.winner == "unresolved"
    assert "model_consensus" in v.reason


def test_teacher_diversity_and_cost_policy():
    d = teacher_diversity_ok(["claude-a", "claude-b"])
    assert d["same_model_is_not_independent"] is True
    plan = plan_teacher_consultation(
        risk="high",
        domain="security",
        uncertain=True,
        high_value=True,
        available_teachers=[
            {"id": "claude-1", "free_tier": False, "cost": 2.0, "domain_score": 0.8, "privacy_ok": True},
            {"id": "openai-1", "free_tier": True, "remaining_quota": 10, "cost": 0.0, "domain_score": 0.7, "privacy_ok": True},
            {"id": "grok-1", "free_tier": True, "remaining_quota": 5, "cost": 0.0, "domain_score": 0.6, "privacy_ok": True},
        ],
    )
    assert plan.mode == "multi"
    assert plan.notes["majority_vote_forbidden"] is True
    assert plan.notes["free_is_not_trustworthy"] is True


def test_supervised_school_scenario_simulated_teacher(superuser_db):
    owner = _owner(superuser_db)
    local = LocalAttempt(
        domain="research",
        task_class="low_risk_classification",
        attempt_summary="classified as public_notice",
        success=False,
        confidence=0.3,
    )
    teacher = TeacherCritique(
        teacher_id="sim-teacher",
        domain="research",
        critique_summary="prefer label informational_public_text",
        claimed_correct=True,
        trusted=False,
    )
    # Simulated teacher path — no provider.
    result = run_learning_cycle(
        superuser_db,
        owner_id=owner.id,
        local=local,
        teacher=teacher,
        root_cause="coarse label taxonomy",
        general_rule="Use informational_public_text for library/museum notices",
        run_exam=True,
        exam_passed=True,
        exam_score=0.9,
        prior_exam_passes=0,
        new_or_hard_domain=True,
    )
    assert result.teacher_used is True
    assert result.weight_training_ran is False
    assert result.authority_widened is False
    assert result.exam is not None
    assert result.exam.passed is True
