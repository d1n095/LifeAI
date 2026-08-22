"""Life Causal Diagnosis Interface -- proves that a failed step never automatically implies a
code regression, that observation/hypothesis/proven-cause remain genuinely distinct epistemic
stages, and that a diagnosis can only ever be marked `proven_cause` when grounded in real
evidence. See migration 0050's own module docstring and docs/LIFE_CAUSAL_DIAGNOSIS_
INTERFACE.md for the full architecture."""

import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from app.diagnosis import (
    DiagnosisError,
    get_diagnosis,
    list_diagnoses,
    list_unresolved_diagnoses,
    prove_diagnosis_cause,
    record_diagnosis,
    rule_out_diagnosis,
)
from app.intelligence_governance import record_evidence, record_execution
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.user import User


def _owner(db):
    user = User(email=f"diag-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _task(db, owner_id):
    goal = MainAIGoal(owner_id=owner_id, title="diagnosis test", original_instruction="x", created_by="test")
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    db.add(plan)
    db.flush()
    task = MainAITask(owner_id=owner_id, goal_id=goal.id, plan_id=plan.id, description="t", task_type="repo_edit", status="pending")
    db.add(task)
    db.flush()
    return task


def _real_evidence(db, owner_id, task):
    execution = record_execution(db, owner_id=owner_id, task_id=task.id, idempotency_key=f"exec-{uuid.uuid4()}", provider="internal")
    return record_evidence(
        db, owner_id=owner_id, execution_id=execution.id, evidence_kind="ci_log", review_kind="deterministic_tool",
        deterministic=True, payload={"conclusion": "external_503"}, source_type="ci_log", source_ref="github-actions-run-123",
        idempotency_key=f"ev-{uuid.uuid4()}",
    )


# ============================================================================ Requirement:
# a failed step never automatically implies a code regression -- every category is explicit.

def test_record_diagnosis_defaults_to_unknown_category_never_a_guess(superuser_db):
    owner = _owner(superuser_db)
    diagnosis = record_diagnosis(superuser_db, owner_id=owner.id, observation="CI job failed after PR merge attempt.", idempotency_key="diag-1")
    superuser_db.commit()
    assert diagnosis.hypothesis_category == "unknown"
    assert diagnosis.epistemic_stage == "observed"
    assert diagnosis.authority == "unknown"
    assert diagnosis.basis == "unknown"
    assert diagnosis.confidence is None


def test_the_concrete_mission_scenario_pr_green_but_github_api_503_during_merge(superuser_db):
    """The mission's own worked example: 'PR tests green + GitHub API HTTP 503 during merge ->
    external/transient blocker candidate -> preserve code verdict -> retry later.' A CI failure
    reaching this module is recorded as an OBSERVATION first, a HYPOTHESIS second (external
    service failure, not code regression), and never silently treated as proof the code is
    bad."""

    owner = _owner(superuser_db)
    diagnosis = record_diagnosis(
        superuser_db, owner_id=owner.id, observation="PR #87's own tests were green; merge attempt failed with GitHub API HTTP 503.",
        idempotency_key="diag-503", hypothesis_category="external_service_failure",
        hypothesis_reasoning="503 is GitHub's own transient-unavailability status, not a test/code failure signal.",
        epistemic_stage="hypothesis", authority="deterministic_source", basis="deterministic",
    )
    superuser_db.commit()
    assert diagnosis.hypothesis_category == "external_service_failure"
    assert diagnosis.epistemic_stage == "hypothesis"  # NOT proven_cause -- still just a hypothesis
    assert diagnosis.hypothesis_category != "code_regression"


def test_category_rejects_arbitrary_values(superuser_db):
    owner = _owner(superuser_db)
    with pytest.raises(DBAPIError):
        record_diagnosis(superuser_db, owner_id=owner.id, observation="x", idempotency_key="bad-cat", hypothesis_category="definitely_a_bug_trust_me")
    superuser_db.rollback()


# ============================================================================ Requirement:
# observation vs hypothesis vs proven cause stay genuinely distinct.

def test_proven_cause_requires_real_evidence_the_database_itself_refuses_a_guess(superuser_db):
    owner = _owner(superuser_db)
    with pytest.raises(DBAPIError):
        record_diagnosis(superuser_db, owner_id=owner.id, observation="x", idempotency_key="no-evidence", epistemic_stage="proven_cause")
    superuser_db.rollback()


def test_prove_diagnosis_cause_is_the_only_path_to_proven_cause_and_requires_real_evidence(superuser_db):
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    evidence = _real_evidence(superuser_db, owner.id, task)
    superuser_db.commit()

    diagnosis = record_diagnosis(
        superuser_db, owner_id=owner.id, observation="Deploy job timed out.", idempotency_key="diag-prove",
        hypothesis_category="external_service_failure", epistemic_stage="hypothesis",
    )
    superuser_db.commit()
    assert diagnosis.epistemic_stage == "hypothesis"

    proven = prove_diagnosis_cause(superuser_db, owner_id=owner.id, diagnosis_id=diagnosis.id, evidence_id=evidence.id, confidence=0.95)
    superuser_db.commit()
    assert proven.epistemic_stage == "proven_cause"
    assert proven.proven_evidence_id == evidence.id
    assert float(proven.confidence) == pytest.approx(0.95)


def test_rule_out_diagnosis_never_deletes_the_hypothesis_just_marks_it_rejected(superuser_db):
    owner = _owner(superuser_db)
    diagnosis = record_diagnosis(
        superuser_db, owner_id=owner.id, observation="Flaky test failure.", idempotency_key="diag-ruleout",
        hypothesis_category="code_regression", epistemic_stage="hypothesis",
    )
    superuser_db.commit()

    ruled_out = rule_out_diagnosis(superuser_db, owner_id=owner.id, diagnosis_id=diagnosis.id)
    superuser_db.commit()
    assert ruled_out.epistemic_stage == "ruled_out"
    assert ruled_out.observation == "Flaky test failure."  # never rewritten

    fetched = get_diagnosis(superuser_db, owner_id=owner.id, diagnosis_id=diagnosis.id)
    assert fetched is not None  # still durably queryable, not deleted


def test_list_unresolved_diagnoses_excludes_proven_and_ruled_out(superuser_db):
    owner = _owner(superuser_db)
    task = _task(superuser_db, owner.id)
    evidence = _real_evidence(superuser_db, owner.id, task)
    superuser_db.commit()

    observed = record_diagnosis(superuser_db, owner_id=owner.id, observation="a", idempotency_key="u-1")
    hypothesis = record_diagnosis(superuser_db, owner_id=owner.id, observation="b", idempotency_key="u-2", epistemic_stage="hypothesis")
    superuser_db.commit()
    proven = record_diagnosis(superuser_db, owner_id=owner.id, observation="c", idempotency_key="u-3", epistemic_stage="hypothesis")
    superuser_db.commit()
    prove_diagnosis_cause(superuser_db, owner_id=owner.id, diagnosis_id=proven.id, evidence_id=evidence.id)
    ruled = record_diagnosis(superuser_db, owner_id=owner.id, observation="d", idempotency_key="u-4", epistemic_stage="hypothesis")
    superuser_db.commit()
    rule_out_diagnosis(superuser_db, owner_id=owner.id, diagnosis_id=ruled.id)
    superuser_db.commit()

    unresolved = {d.id for d in list_unresolved_diagnoses(superuser_db, owner_id=owner.id)}
    assert unresolved == {observed.id, hypothesis.id}
    assert proven.id not in unresolved
    assert ruled.id not in unresolved


# ============================================================================ Requirement:
# a later correction supersedes an earlier diagnosis while preserving both.

def test_a_later_diagnosis_can_correct_an_earlier_one_while_preserving_both(superuser_db):
    owner = _owner(superuser_db)
    first = record_diagnosis(
        superuser_db, owner_id=owner.id, observation="Timing-dependent test failure observed.", idempotency_key="corr-1",
        hypothesis_category="unknown", epistemic_stage="hypothesis",
    )
    superuser_db.commit()

    corrected = record_diagnosis(
        superuser_db, owner_id=owner.id, observation="Re-investigated: the same failure reproduces deterministically with a fixed seed.",
        idempotency_key="corr-2", hypothesis_category="code_regression", epistemic_stage="hypothesis", supersedes_diagnosis_id=first.id,
    )
    superuser_db.commit()

    assert corrected.supersedes_diagnosis_id == first.id
    superuser_db.refresh(first)
    assert first.observation == "Timing-dependent test failure observed."  # untouched
    assert first.epistemic_stage == "hypothesis"  # superseding does not itself change the old row
    both = {d.id for d in list_diagnoses(superuser_db, owner_id=owner.id)}
    assert {first.id, corrected.id} <= both


def test_superseding_a_diagnosis_belonging_to_another_owner_fails_closed(superuser_db):
    owner_a = _owner(superuser_db)
    owner_b = _owner(superuser_db)
    diagnosis_a = record_diagnosis(superuser_db, owner_id=owner_a.id, observation="A's diagnosis.", idempotency_key="cross-a")
    superuser_db.commit()

    with pytest.raises(DiagnosisError):
        record_diagnosis(superuser_db, owner_id=owner_b.id, observation="B tries to supersede A's diagnosis.", idempotency_key="cross-b", supersedes_diagnosis_id=diagnosis_a.id)


# ============================================================================ Requirement:
# idempotent replay, never a duplicate or a silently-picked winner.

def test_record_diagnosis_is_idempotent_and_rejects_a_reused_key_with_different_fields(superuser_db):
    owner = _owner(superuser_db)
    first = record_diagnosis(superuser_db, owner_id=owner.id, observation="Same observation.", idempotency_key="idem-diag")
    superuser_db.commit()
    replay = record_diagnosis(superuser_db, owner_id=owner.id, observation="Same observation.", idempotency_key="idem-diag")
    assert replay.id == first.id

    with pytest.raises(DiagnosisError):
        record_diagnosis(superuser_db, owner_id=owner.id, observation="A completely different observation.", idempotency_key="idem-diag")


# ============================================================================ Reuse, not
# duplication: the authority vocabulary is migration 0042's own.

def test_authority_reuses_life_problem_vocabulary_and_rejects_foreign_values(superuser_db):
    owner = _owner(superuser_db)
    diagnosis = record_diagnosis(superuser_db, owner_id=owner.id, observation="x", idempotency_key="auth-1", authority="founder")
    superuser_db.commit()
    assert diagnosis.authority == "founder"

    with pytest.raises(DBAPIError):
        record_diagnosis(superuser_db, owner_id=owner.id, observation="y", idempotency_key="auth-2", authority="admin_override")
    superuser_db.rollback()
