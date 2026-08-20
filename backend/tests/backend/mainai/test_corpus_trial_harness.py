"""Life Corpus Trial Harness -- proves the bundled bootstrap corpus, replayed through the
REAL `app.founder_memory`/`app.diagnosis` recording APIs, scores clean on every dimension; and
separately proves each scorer function in `app.corpus_trial.scoring` is a genuine structural
check by feeding it a deliberately corrupted snapshot and confirming it is caught -- so a green
trial report means something, not just "the fixtures were tuned to pass." See docs/
LIFE_CORPUS_TRIAL_HARNESS.md for the full architecture."""

import uuid

from app.corpus_trial import CORPUS, RecordSnapshot, run_trial
from app.corpus_trial.scoring import (
    score_attribution_accuracy,
    score_contradiction_detection,
    score_current_state_reconstruction,
    score_epistemic_distinction,
    score_source_preservation,
    score_supersession_detection,
    score_uncertainty_preservation,
)
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.user import User


def _owner_and_task(db):
    user = User(email=f"corpus-trial-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    goal = MainAIGoal(owner_id=user.id, title="corpus trial", original_instruction="x", created_by="test")
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=user.id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    db.add(plan)
    db.flush()
    task = MainAITask(owner_id=user.id, goal_id=goal.id, plan_id=plan.id, description="t", task_type="repo_edit", status="pending")
    db.add(task)
    db.flush()
    return user, task


# ============================================================================ The bundled
# bootstrap corpus, run end to end through the real recording APIs, must score clean.

def test_the_bootstrap_corpus_scores_clean_on_every_dimension(superuser_db):
    owner, task = _owner_and_task(superuser_db)
    superuser_db.commit()

    report = run_trial(superuser_db, owner_id=owner.id, evidence_task_id=task.id)
    superuser_db.commit()

    assert report.passed, report.dimension_violations
    # Every "record"/"supersede" corpus item produces exactly one distinct row.
    expected_records = sum(1 for item in CORPUS if item.action in ("record", "supersede"))
    assert report.record_count == expected_records
    # All seven scoring dimensions actually ran (nothing silently skipped).
    assert set(report.dimension_violations) == {
        "source_preservation", "attribution_accuracy", "epistemic_distinction", "contradiction_detection",
        "supersession_detection", "uncertainty_preservation", "current_state_reconstruction",
    }


def test_running_the_trial_twice_for_different_owners_never_cross_contaminates(superuser_db):
    owner_a, task_a = _owner_and_task(superuser_db)
    owner_b, task_b = _owner_and_task(superuser_db)
    superuser_db.commit()

    report_a = run_trial(superuser_db, owner_id=owner_a.id, evidence_task_id=task_a.id)
    superuser_db.commit()
    report_b = run_trial(superuser_db, owner_id=owner_b.id, evidence_task_id=task_b.id)
    superuser_db.commit()

    assert report_a.passed and report_b.passed


# ============================================================================ Each scorer is
# a genuine check: prove it catches a deliberately corrupted snapshot, not just a rubber stamp.

def _clean_snapshot(**overrides):
    base = dict(
        system="founder_memory", record_id="r1", text_submitted="original text", text_read_back="original text",
        authority_submitted="founder", authority_read_back="founder", basis_submitted="manual", basis_read_back="manual",
        confidence_submitted=None, confidence_read_back=None, supersedes_id=None, is_current=True, is_contradicted=False,
        contradiction_text_read_back="original text",
    )
    base.update(overrides)
    return RecordSnapshot(**base)


def test_source_preservation_scorer_catches_mutated_text():
    assert score_source_preservation([_clean_snapshot()]) == []
    corrupted = _clean_snapshot(text_read_back="a silently rewritten version")
    assert score_source_preservation([corrupted]) != []


def test_attribution_accuracy_scorer_catches_authority_drift():
    assert score_attribution_accuracy([_clean_snapshot()]) == []
    corrupted = _clean_snapshot(authority_submitted="ai_interpretation", authority_read_back="founder")
    violations = score_attribution_accuracy([corrupted])
    assert violations and "ai_interpretation" in violations[0] and "founder" in violations[0]


def test_epistemic_distinction_scorer_catches_a_collapsed_category():
    snapshots = [_clean_snapshot(record_id="r1", authority_read_back="founder")]
    assert score_epistemic_distinction(snapshots, {"founder"}) == []
    assert score_epistemic_distinction(snapshots, {"founder", "ai_interpretation"}) != []


def test_contradiction_detection_scorer_catches_a_missing_or_deleted_contradiction():
    expected_but_missing = _clean_snapshot(is_contradicted=False, extra={"expected_contradicted": True})
    assert score_contradiction_detection([expected_but_missing]) != []
    looks_deleted = _clean_snapshot(is_contradicted=True, contradiction_text_read_back=None, extra={"expected_contradicted": True})
    assert score_contradiction_detection([looks_deleted]) != []


def test_supersession_detection_scorer_catches_a_mutated_old_row():
    old = _clean_snapshot(record_id="old", text_submitted="original", text_read_back="original")
    new_clean = _clean_snapshot(record_id="new", supersedes_id="old")
    assert score_supersession_detection([old, new_clean]) == []
    old_mutated = _clean_snapshot(record_id="old", text_submitted="original", text_read_back="rewritten in place")
    assert score_supersession_detection([old_mutated, new_clean]) != []
    dangling = _clean_snapshot(record_id="new2", supersedes_id="does-not-exist")
    assert score_supersession_detection([dangling]) != []


def test_uncertainty_preservation_scorer_catches_fabricated_confidence():
    assert score_uncertainty_preservation([_clean_snapshot()]) == []
    fabricated = _clean_snapshot(confidence_submitted=None, confidence_read_back=0.99)
    assert score_uncertainty_preservation([fabricated]) != []
    resolved_unknown = _clean_snapshot(authority_submitted="unknown", authority_read_back="founder")
    assert score_uncertainty_preservation([resolved_unknown]) != []


def test_current_state_reconstruction_scorer_catches_a_stale_row_reported_as_current():
    superseded = _clean_snapshot(record_id="old", is_current=True)  # WRONG: should be False, it's superseded
    superseding = _clean_snapshot(record_id="new", supersedes_id="old", is_current=True)
    violations = score_current_state_reconstruction([superseded, superseding])
    assert violations and "old" in violations[0]

    fixed = _clean_snapshot(record_id="old", is_current=False)
    assert score_current_state_reconstruction([fixed, superseding]) == []
