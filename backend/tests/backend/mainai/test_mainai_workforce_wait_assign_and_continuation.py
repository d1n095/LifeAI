"""`app.mainai_workforce.wait_or_assign` + `continuation_policy` + `reservation`. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md."""

from __future__ import annotations

from app.mainai_workforce.continuation_policy import decide_continue_or_handoff
from app.mainai_workforce.reservation import assess_reservation
from app.mainai_workforce.types import WaitOrAssignDecision
from app.mainai_workforce.wait_or_assign import decide_wait_or_assign


def test_best_agent_available_works_now():
    result = decide_wait_or_assign(
        best_agent_available=True, best_agent_eta_seconds=None, best_agent_competency=0.9,
        candidate_agent_available=True, candidate_agent_competency=0.5,
    )
    assert result.decision == WaitOrAssignDecision.WORK_NOW


def test_best_agent_busy_briefly_waits_founder_worked_example():
    """Codex is ideal and 8 minutes from finishing; Claude is idle but requires expensive
    context reload -> WAIT FOR CODEX (the founder's own worked example, verbatim)."""

    result = decide_wait_or_assign(
        best_agent_available=False, best_agent_eta_seconds=8 * 60, best_agent_competency=0.95,
        candidate_agent_available=True, candidate_agent_competency=0.6,
    )
    assert result.decision == WaitOrAssignDecision.WAIT


def test_best_agent_busy_long_assigns_second_best_founder_worked_example():
    """Codex has 3 hours remaining; Claude can safely complete task now -> ASSIGN CLAUDE (the
    founder's own second worked example, verbatim)."""

    result = decide_wait_or_assign(
        best_agent_available=False, best_agent_eta_seconds=3 * 3600, best_agent_competency=0.95,
        candidate_agent_available=True, candidate_agent_competency=0.6, critical_path=True,
    )
    assert result.decision == WaitOrAssignDecision.WORK_NOW


def test_duplication_risk_defers_regardless_of_availability():
    result = decide_wait_or_assign(
        best_agent_available=True, best_agent_eta_seconds=None, best_agent_competency=0.9,
        candidate_agent_available=True, candidate_agent_competency=0.9, duplication_risk=True,
    )
    assert result.decision == WaitOrAssignDecision.DEFER


def test_branch_conflict_risk_defers():
    result = decide_wait_or_assign(
        best_agent_available=True, best_agent_eta_seconds=None, best_agent_competency=0.9,
        candidate_agent_available=True, candidate_agent_competency=0.9, branch_conflict_risk=True,
    )
    assert result.decision == WaitOrAssignDecision.DEFER


def test_high_context_loaded_agent_beats_nominally_stronger_cold_agent():
    result = decide_continue_or_handoff(
        current_agent_context_loaded_relevance=0.9, current_agent_competency=0.7, current_agent_rework_rate=0.05,
        candidate_agent_competency=0.8, candidate_agent_rework_rate=0.05,
        handoff_cost_seconds=600, interruption_cost_seconds=120,
    )
    assert result.decision == WaitOrAssignDecision.CONTINUE_CURRENT_WORK


def test_cheap_agent_with_high_rework_loses_to_expensive_reliable_agent():
    result = decide_continue_or_handoff(
        current_agent_context_loaded_relevance=0.1, current_agent_competency=0.6, current_agent_rework_rate=0.5,
        candidate_agent_competency=0.85, candidate_agent_rework_rate=0.02,
        handoff_cost_seconds=60, interruption_cost_seconds=0,
    )
    assert result.decision == WaitOrAssignDecision.HAND_OFF


def test_reservation_with_evidence_and_within_cap_is_kept():
    result = assess_reservation(upcoming_task_known=True, upcoming_task_requires_this_agent=True, evidence_for_upcoming_need=True, reserved_since_minutes=10)
    assert result.should_remain_reserved is True


def test_reservation_without_evidence_is_not_kept_agent_reserved_is_not_agent_wasted():
    result = assess_reservation(upcoming_task_known=True, upcoming_task_requires_this_agent=True, evidence_for_upcoming_need=False, reserved_since_minutes=10)
    assert result.should_remain_reserved is False


def test_reservation_past_cap_is_never_indefinite():
    result = assess_reservation(upcoming_task_known=True, upcoming_task_requires_this_agent=True, evidence_for_upcoming_need=True, reserved_since_minutes=120, max_reservation_minutes=60)
    assert result.should_remain_reserved is False
