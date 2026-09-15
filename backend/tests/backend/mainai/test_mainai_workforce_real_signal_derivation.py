"""`app.mainai_workforce.signal_derivation` + `real_state_decisions` -- Part C (derive
workforce signals from real system state) and Part D (real cross-component composition). See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md."""

from __future__ import annotations

import uuid

import pytest

from app.capability_reality.service import record_capability_observation
from app.mainai_cognitive_ops.types import AgentState
from app.mainai_workforce.mastery_ledger import get_or_create_mastery_record, record_observation
from app.mainai_workforce.real_state_decisions import (
    NEUTRAL_COMPETENCY_ESTIMATE,
    decide_continue_or_handoff_from_real_state,
    decide_wait_or_assign_from_real_state,
)
from app.mainai_workforce.signal_derivation import (
    derive_competency_signal,
    derive_context_loaded_relevance_signal,
    derive_quota_uncertainty_signal,
    resolve_signal,
)
from app.mainai_workforce.types import SignalEnvelope, SignalOrigin, WaitOrAssignDecision
from app.models.user import User


@pytest.fixture
def owner_id(superuser_db):
    user = User(email=f"wfsig-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(user)
    superuser_db.flush()
    superuser_db.commit()
    return user.id


# ---------------------------------------------------------------- derive_competency_signal


def test_recent_strong_performance_derives_high_competency_from_mastery_ledger(superuser_db, owner_id):
    mastery = get_or_create_mastery_record(superuser_db, owner_id=owner_id, capability_key="pg_migration", task_class="debug", external_teacher="codex", idempotency_key=f"m-{uuid.uuid4()}")
    for _ in range(9):
        record_observation(superuser_db, owner_id=owner_id, mastery_id=mastery["id"], examiner_pass=True)
    record_observation(superuser_db, owner_id=owner_id, mastery_id=mastery["id"], examiner_pass=False)
    superuser_db.commit()

    signal = derive_competency_signal(superuser_db, owner_id=owner_id, capability_key="pg_migration", task_class="debug", external_teacher="codex", agent_id=uuid.uuid4())
    assert signal.origin == SignalOrigin.DERIVED
    assert signal.value == pytest.approx(0.9)


def test_repeated_examiner_failures_derive_low_competency(superuser_db, owner_id):
    mastery = get_or_create_mastery_record(superuser_db, owner_id=owner_id, capability_key="pg_migration", task_class="debug", external_teacher="codex", idempotency_key=f"m-{uuid.uuid4()}")
    for _ in range(8):
        record_observation(superuser_db, owner_id=owner_id, mastery_id=mastery["id"], examiner_pass=False)
    for _ in range(2):
        record_observation(superuser_db, owner_id=owner_id, mastery_id=mastery["id"], examiner_pass=True)
    superuser_db.commit()

    signal = derive_competency_signal(superuser_db, owner_id=owner_id, capability_key="pg_migration", task_class="debug", external_teacher="codex", agent_id=uuid.uuid4())
    assert signal.value == pytest.approx(0.2)


def test_sparse_mastery_history_falls_through_to_capability_reality(superuser_db, owner_id):
    """Zero examined observations -- the mastery-ledger record EXISTS but has no
    examiner-verified evidence yet, so this must NOT be treated as competency=0; it falls
    through to the next real signal (capability_reality)."""

    get_or_create_mastery_record(superuser_db, owner_id=owner_id, capability_key="rare_capability", task_class="debug", external_teacher="codex", idempotency_key=f"m-{uuid.uuid4()}")
    record_capability_observation(superuser_db, owner_id=owner_id, capability_key="rare_capability", domain="backend", status="verified_available", confidence=0.75)
    superuser_db.commit()

    signal = derive_competency_signal(superuser_db, owner_id=owner_id, capability_key="rare_capability", task_class="debug", external_teacher="codex", agent_id=uuid.uuid4())
    assert signal.origin == SignalOrigin.DERIVED
    assert signal.value == pytest.approx(0.75)
    assert "capability_reality" in signal.source


def test_no_real_signal_anywhere_is_honestly_unknown_never_fabricated_zero(superuser_db, owner_id):
    signal = derive_competency_signal(superuser_db, owner_id=owner_id, capability_key="never_seen_capability", task_class="debug")
    assert signal.origin == SignalOrigin.UNKNOWN
    assert signal.value is None


def test_missing_quota_is_unknown_never_zero_or_unlimited(superuser_db, owner_id):
    signal = derive_quota_uncertainty_signal(superuser_db, owner_id=owner_id, goal_id=uuid.uuid4())
    assert signal.origin == SignalOrigin.UNKNOWN
    assert signal.value is None


def test_context_loaded_relevance_derived_from_real_agent_state_ownership():
    owning_agent = AgentState(agent_id="a1", idle=True, current_program="goal-42")
    other_agent = AgentState(agent_id="a2", idle=True, current_program="goal-99")

    owning_signal = derive_context_loaded_relevance_signal(agent_state=owning_agent, candidate_program_id="goal-42")
    other_signal = derive_context_loaded_relevance_signal(agent_state=other_agent, candidate_program_id="goal-42")
    unknown_signal = derive_context_loaded_relevance_signal(agent_state=None, candidate_program_id="goal-42")

    assert owning_signal.value == pytest.approx(0.9)
    assert other_signal.value == pytest.approx(0.1)
    assert unknown_signal.origin == SignalOrigin.UNKNOWN


# ---------------------------------------------------------------- resolve_signal


def test_real_signal_always_wins_over_override_stronger_evidence_protected():
    real = SignalEnvelope(value=0.9, origin=SignalOrigin.DERIVED, source="mastery_ledger", note="9/10 examiner passes")
    value, origin, _ = resolve_signal(real, override=0.1)
    assert value == 0.9
    assert origin == SignalOrigin.DERIVED


def test_override_used_only_when_no_real_signal_exists():
    unknown = SignalEnvelope(value=None, origin=SignalOrigin.UNKNOWN, source="none", note="no data")
    value, origin, _ = resolve_signal(unknown, override=0.7)
    assert value == 0.7
    assert origin == SignalOrigin.CALLER_SUPPLIED


def test_no_signal_no_override_is_unknown():
    unknown = SignalEnvelope(value=None, origin=SignalOrigin.UNKNOWN, source="none", note="no data")
    value, origin, _ = resolve_signal(unknown, override=None)
    assert value is None
    assert origin == SignalOrigin.UNKNOWN


# ---------------------------------------------------------------- real composition (§D)


def test_best_agent_busy_but_almost_finished_waits_using_real_derived_competency(superuser_db, owner_id):
    strong = get_or_create_mastery_record(superuser_db, owner_id=owner_id, capability_key="pg_migration", task_class="debug", external_teacher="codex", idempotency_key=f"m-{uuid.uuid4()}")
    for _ in range(10):
        record_observation(superuser_db, owner_id=owner_id, mastery_id=strong["id"], examiner_pass=True)
    superuser_db.commit()

    decision = decide_wait_or_assign_from_real_state(
        superuser_db, owner_id=owner_id, capability_key="pg_migration", task_class="debug",
        best_agent_id=uuid.uuid4(), candidate_agent_id=uuid.uuid4(), best_agent_available=False,
        best_agent_eta_seconds=8 * 60, best_agent_teacher="codex", candidate_agent_competency_override=0.6,
    )
    assert decision.result.decision == WaitOrAssignDecision.WAIT
    assert decision.signals.best_agent_competency.origin == SignalOrigin.DERIVED


def test_weaker_agent_immediately_available_is_assigned_when_best_eta_is_long(superuser_db, owner_id):
    strong = get_or_create_mastery_record(superuser_db, owner_id=owner_id, capability_key="pg_migration", task_class="debug", external_teacher="codex", idempotency_key=f"m-{uuid.uuid4()}")
    for _ in range(10):
        record_observation(superuser_db, owner_id=owner_id, mastery_id=strong["id"], examiner_pass=True)
    superuser_db.commit()

    decision = decide_wait_or_assign_from_real_state(
        superuser_db, owner_id=owner_id, capability_key="pg_migration", task_class="debug",
        best_agent_id=uuid.uuid4(), candidate_agent_id=uuid.uuid4(), best_agent_available=False,
        best_agent_eta_seconds=3 * 3600, best_agent_teacher="codex", candidate_agent_competency_override=0.6,
        critical_path=True,
    )
    assert decision.result.decision == WaitOrAssignDecision.WORK_NOW


def test_no_sufficiently_supported_best_choice_falls_back_to_neutral_estimate_for_both(superuser_db, owner_id):
    decision = decide_wait_or_assign_from_real_state(
        superuser_db, owner_id=owner_id, capability_key="brand_new_capability", task_class="debug",
        best_agent_id=uuid.uuid4(), candidate_agent_id=uuid.uuid4(), best_agent_available=False,
        best_agent_eta_seconds=5 * 60,
    )
    assert decision.signals.best_agent_competency.origin == SignalOrigin.ESTIMATED
    assert decision.signals.best_agent_competency.value == NEUTRAL_COMPETENCY_ESTIMATE
    assert decision.signals.candidate_agent_competency.value == NEUTRAL_COMPETENCY_ESTIMATE


def test_high_handoff_and_interruption_cost_keeps_continuation(superuser_db, owner_id):
    current_agent_id, candidate_agent_id = uuid.uuid4(), uuid.uuid4()
    current_state = AgentState(agent_id=str(current_agent_id), busy=True, current_program="goal-1")

    decision = decide_continue_or_handoff_from_real_state(
        superuser_db, owner_id=owner_id, capability_key="unseen_capability", task_class="debug",
        current_agent_id=current_agent_id, candidate_agent_id=candidate_agent_id,
        current_agent_state=current_state, candidate_program_id="goal-1",
        handoff_cost_seconds=3600, interruption_cost_seconds=1800,
        current_agent_competency_override=0.6, candidate_agent_competency_override=0.65,
    )
    assert decision.result.decision == WaitOrAssignDecision.CONTINUE_CURRENT_WORK
    assert decision.signals.context_loaded_relevance.value == pytest.approx(0.9)


def test_same_agent_already_owns_relevant_program_context_favors_continuation(superuser_db, owner_id):
    current_agent_id, candidate_agent_id = uuid.uuid4(), uuid.uuid4()
    current_state = AgentState(agent_id=str(current_agent_id), busy=True, current_program="goal-7")

    decision = decide_continue_or_handoff_from_real_state(
        superuser_db, owner_id=owner_id, capability_key="unseen_capability_2", task_class="debug",
        current_agent_id=current_agent_id, candidate_agent_id=candidate_agent_id,
        current_agent_state=current_state, candidate_program_id="goal-7",
        handoff_cost_seconds=60, interruption_cost_seconds=0,
        current_agent_competency_override=0.6, candidate_agent_competency_override=0.62,
    )
    assert decision.result.decision == WaitOrAssignDecision.CONTINUE_CURRENT_WORK
