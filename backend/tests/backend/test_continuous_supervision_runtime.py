from datetime import datetime, timedelta, timezone

from app.mainai_execution.supervision_runtime import (
    BlockerEvidence,
    CompletionDecision,
    CompletionFacts,
    CostClassification,
    Liveness,
    classify_liveness,
    evaluate_completion,
    validate_blocker,
    classify_cost,
)


def _facts(**changes):
    values = dict(
        scope_complete=True,
        p0_remaining=0,
        tests_passed=True,
        exact_sha=True,
        clean_worktree=True,
        artifact_frozen=True,
        attempt_current=True,
        lease_current=True,
        dependency_ready=True,
    )
    values.update(changes)
    return CompletionFacts(**values)


def test_completion_evaluator_fails_closed_and_requires_independent_review():
    assert evaluate_completion(_facts(examiner_required=True)) is CompletionDecision.VERIFY_REQUIRED
    assert evaluate_completion(_facts(exact_sha=False)) is CompletionDecision.FAILED
    assert evaluate_completion(_facts(p0_remaining=1)) is CompletionDecision.PARTIAL_CONTINUE
    assert evaluate_completion(_facts(cancelled=True)) is CompletionDecision.CANCELLED
    assert evaluate_completion(_facts()) is CompletionDecision.COMPLETE


def test_blocker_validation_checks_evidence_instead_of_agent_prose():
    assert validate_blocker("pytest missing", BlockerEvidence(local_tools_available=False))[0] is False
    assert validate_blocker("provider unavailable", BlockerEvidence(provider_alternative_available=True))[0] is False
    assert validate_blocker("founder approval", BlockerEvidence(founder_only=True))[0] is True


def test_liveness_distinguishes_slow_progress_from_idle_and_dead_process():
    now = datetime.now(timezone.utc)
    assert classify_liveness(heartbeat_at=now, progress_changed=True, process_alive=True, now=now) is Liveness.HEALTHY
    assert classify_liveness(heartbeat_at=now - timedelta(minutes=10), progress_changed=False, process_alive=True, now=now) is Liveness.IDLE
    assert classify_liveness(heartbeat_at=now, progress_changed=False, process_alive=False, now=now) is Liveness.DISCONNECTED


def test_cost_reconciliation_never_trusts_over_ceiling_reports():
    assert classify_cost(reserved=1, reported=0.1, prompt_tokens=10, completion_tokens=10, unit_price=0.005) is CostClassification.VERIFIED
    assert classify_cost(reserved=1, reported=2) is CostClassification.INVALID
    assert classify_cost(reserved=1, reported=None) is CostClassification.UNCERTAIN
