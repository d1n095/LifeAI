"""Row-Level Security, exercised directly at the database layer through the restricted
runtime role (mainai_app), for `corpus_trial_runs` (migration 0052). Mirrors tests/security/
test_rls_isolation.py's and test_rls_isolation_cognition_foundation.py's own established
pattern exactly -- see the latter's module docstring for why this behavioral proof (not just
a Python-level owner_id filter) matters."""

from sqlalchemy import text

from app.corpus_trial import record_trial_run, run_trial
from app.models.corpus_trial_run import CorpusTrialRun
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask


def _set_rls_user(session, user_id) -> None:
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


def _task_for(session, owner_id):
    goal = MainAIGoal(owner_id=owner_id, title="rls trial task", original_instruction="x", created_by="test")
    session.add(goal)
    session.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    session.add(plan)
    session.flush()
    task = MainAITask(owner_id=owner_id, goal_id=goal.id, plan_id=plan.id, description="t", task_type="repo_edit", status="pending")
    session.add(task)
    session.flush()
    return task


def test_user_never_reads_another_users_corpus_trial_runs(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    task_a = _task_for(db_session, user_a.id)
    db_session.commit()
    _set_rls_user(db_session, user_a.id)
    report_a = run_trial(db_session, owner_id=user_a.id, evidence_task_id=task_a.id)
    record_trial_run(db_session, owner_id=user_a.id, report=report_a, idempotency_key="rls-run-a")
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    task_b = _task_for(db_session, user_b.id)
    db_session.commit()
    _set_rls_user(db_session, user_b.id)
    report_b = run_trial(db_session, owner_id=user_b.id, evidence_task_id=task_b.id)
    record_trial_run(db_session, owner_id=user_b.id, report=report_b, idempotency_key="rls-run-b")
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(CorpusTrialRun).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].owner_id == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(CorpusTrialRun).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].owner_id == user_b.id


def test_cannot_write_a_corpus_trial_run_for_another_user(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    db_session.add(CorpusTrialRun(owner_id=user_b.id, corpus_label="x", record_count=0, passed=True, idempotency_key="rls-cross-write"))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by corpus_trial_runs_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()
