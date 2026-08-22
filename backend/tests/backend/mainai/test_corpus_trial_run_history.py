"""Life Corpus Trial Run History -- proves `record_trial_run()` durably snapshots a
`TrialReport` without touching the underlying provenance rows the trial exercised, is
idempotent, and that `corpus_trial_runs` is genuinely append-only (a direct UPDATE/DELETE is
rejected by the DB trigger even for a superuser session, not just hidden by RLS). See
migration 0052's own module docstring and docs/LIFE_CORPUS_TRIAL_HARNESS.md."""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InternalError

from app.corpus_trial import CorpusTrialRunError, list_trial_runs, record_trial_run, run_trial
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask
from app.models.user import User


def _owner_and_task(db):
    user = User(email=f"trial-run-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    goal = MainAIGoal(owner_id=user.id, title="trial run history", original_instruction="x", created_by="test")
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=user.id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    db.add(plan)
    db.flush()
    task = MainAITask(owner_id=user.id, goal_id=goal.id, plan_id=plan.id, description="t", task_type="repo_edit", status="pending")
    db.add(task)
    db.flush()
    return user, task


def test_recording_a_real_trial_run_snapshots_the_report_faithfully(superuser_db):
    owner, task = _owner_and_task(superuser_db)
    superuser_db.commit()

    report = run_trial(superuser_db, owner_id=owner.id, evidence_task_id=task.id)
    superuser_db.commit()

    run = record_trial_run(superuser_db, owner_id=owner.id, report=report, idempotency_key="run-1")
    superuser_db.commit()

    assert run.passed == report.passed
    assert run.record_count == report.record_count
    assert run.dimension_summary == report.summary
    assert run.corpus_label == "bootstrap"


def test_record_trial_run_is_idempotent_and_rejects_a_reused_key_with_a_different_summary(superuser_db):
    owner, task = _owner_and_task(superuser_db)
    superuser_db.commit()
    report = run_trial(superuser_db, owner_id=owner.id, evidence_task_id=task.id)
    superuser_db.commit()

    first = record_trial_run(superuser_db, owner_id=owner.id, report=report, idempotency_key="idem-run")
    superuser_db.commit()
    replay = record_trial_run(superuser_db, owner_id=owner.id, report=report, idempotency_key="idem-run")
    assert replay.id == first.id

    from app.corpus_trial.harness import TrialReport

    different = TrialReport(dimension_violations={"source_preservation": ["fake violation"]}, record_count=report.record_count)
    with pytest.raises(CorpusTrialRunError):
        record_trial_run(superuser_db, owner_id=owner.id, report=different, idempotency_key="idem-run")


def test_list_trial_runs_orders_by_run_at_and_filters_by_corpus_label(superuser_db):
    owner, task = _owner_and_task(superuser_db)
    superuser_db.commit()
    report = run_trial(superuser_db, owner_id=owner.id, evidence_task_id=task.id)
    superuser_db.commit()

    record_trial_run(superuser_db, owner_id=owner.id, report=report, idempotency_key="list-1", corpus_label="bootstrap")
    superuser_db.commit()
    record_trial_run(superuser_db, owner_id=owner.id, report=report, idempotency_key="list-2", corpus_label="adversarial_v2")
    superuser_db.commit()

    bootstrap_only = list_trial_runs(superuser_db, owner_id=owner.id, corpus_label="bootstrap")
    assert len(bootstrap_only) == 1
    assert bootstrap_only[0].corpus_label == "bootstrap"

    everything = list_trial_runs(superuser_db, owner_id=owner.id)
    assert len(everything) == 2


def test_corpus_trial_runs_is_genuinely_append_only_not_just_rls_hidden(superuser_db):
    """A direct UPDATE/DELETE must be rejected by the DB trigger even for a superuser session
    (RLS-bypassing) -- proves the append-only guarantee is structural, not merely a runtime
    role's revoked GRANT that a superuser or the erasure function's own owning role could
    still bypass through ordinary DML."""

    owner, task = _owner_and_task(superuser_db)
    superuser_db.commit()
    report = run_trial(superuser_db, owner_id=owner.id, evidence_task_id=task.id)
    superuser_db.commit()
    run = record_trial_run(superuser_db, owner_id=owner.id, report=report, idempotency_key="append-only-1")
    superuser_db.commit()

    with pytest.raises((DBAPIError, InternalError)):
        superuser_db.execute(text("UPDATE corpus_trial_runs SET corpus_label = 'tampered' WHERE id = :id"), {"id": str(run.id)})
        superuser_db.commit()
    superuser_db.rollback()

    with pytest.raises((DBAPIError, InternalError)):
        superuser_db.execute(text("DELETE FROM corpus_trial_runs WHERE id = :id"), {"id": str(run.id)})
        superuser_db.commit()
    superuser_db.rollback()
