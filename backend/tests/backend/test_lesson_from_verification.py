"""Bounded verification→record_lesson writer — SIGNAL PRODUCER != TRUTH WRITER."""

import uuid
from datetime import datetime

import pytest

from app.mainai_execution.execution_job import _finalize_task_outcome
from app.mainai_execution.lesson_from_verification import (
    is_structured_verification_evidence,
    maybe_record_lesson_from_exhausted_verification,
)
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.mainai_execution import EngineeringLesson, MainAITask, MainAITaskStatus
from app.request_context import current_user_id as current_user_id_var
from sqlalchemy import text as sa_text


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    return user.id


def _task(db, owner_id, *, attempts: int, max_attempts: int = 2) -> MainAITask:
    current_user_id_var.set(str(owner_id))
    db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
    goal = create_goal(
        db,
        owner_id=owner_id,
        title="lesson writer",
        original_instruction="prove exhausted verification records a lesson",
        created_by="test",
    )
    create_plan(
        db,
        goal=goal,
        rationale="test",
        tasks=[PlannedTaskSpec(description="run tests", task_type="run_tests")],
        created_by="test",
    )
    db.commit()
    task = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    task.attempts = attempts
    task.max_attempts = max_attempts
    task.status = MainAITaskStatus.running
    db.commit()
    return task


def test_rejects_exception_string_evidence():
    assert is_structured_verification_evidence({"error": "ProviderTimeout: blah"}) is False
    assert is_structured_verification_evidence({"passed": False}) is False
    assert is_structured_verification_evidence(None) is False


def test_accepts_verification_result_evidence_shape():
    evidence = {
        "passed": False,
        "steps": [
            {
                "kind": "targeted_tests",
                "passed": False,
                "detail": {"target": "tests/backend/test_x.py", "returncode": 1},
            }
        ],
    }
    assert is_structured_verification_evidence(evidence) is True


def test_exception_path_finalize_does_not_record_lesson(superuser_db, owner_id):
    task = _task(superuser_db, owner_id, attempts=2, max_attempts=2)
    _finalize_task_outcome(superuser_db, task, passed=False, evidence={"error": "boom: provider said no"})
    superuser_db.commit()
    assert task.status == MainAITaskStatus.failed
    assert superuser_db.query(EngineeringLesson).count() == 0


def test_exhausted_structured_verification_records_one_lesson(superuser_db, owner_id):
    task = _task(superuser_db, owner_id, attempts=2, max_attempts=2)
    job_id = uuid.uuid4()
    evidence = {
        "passed": False,
        "steps": [
            {
                "kind": "targeted_tests",
                "passed": False,
                "detail": {
                    "target": "tests/backend/test_x.py",
                    "returncode": 1,
                    "stdout_tail": "FAILED lots of noise that must not become truth",
                },
            }
        ],
        "work_result": {"branch": "cursor/x"},
    }
    _finalize_task_outcome(superuser_db, task, passed=False, evidence=evidence, job_id=job_id)
    superuser_db.commit()
    assert task.status == MainAITaskStatus.failed
    lessons = superuser_db.query(EngineeringLesson).all()
    assert len(lessons) == 1
    lesson = lessons[0]
    assert lesson.source_type == "verification_exhausted"
    assert str(task.id) in lesson.source_ref
    assert str(job_id) in lesson.source_ref
    assert "run_tests" in lesson.applies_to
    assert "FAILED lots of noise" not in lesson.evidence
    assert "tests/backend/test_x.py" in lesson.evidence or "targeted_tests" in lesson.evidence


def test_retryable_failed_does_not_record_lesson(superuser_db, owner_id):
    task = _task(superuser_db, owner_id, attempts=1, max_attempts=3)
    evidence = {
        "passed": False,
        "steps": [{"kind": "targeted_tests", "passed": False, "detail": {"target": "t.py", "returncode": 1}}],
    }
    _finalize_task_outcome(superuser_db, task, passed=False, evidence=evidence)
    superuser_db.commit()
    assert task.status == MainAITaskStatus.retryable_failed
    assert superuser_db.query(EngineeringLesson).count() == 0


def test_idempotent_per_source_ref(superuser_db, owner_id):
    task = _task(superuser_db, owner_id, attempts=2, max_attempts=2)
    evidence = {
        "passed": False,
        "steps": [{"kind": "targeted_tests", "passed": False, "detail": {"target": "t.py", "returncode": 1}}],
    }
    job_id = uuid.uuid4()
    a = maybe_record_lesson_from_exhausted_verification(superuser_db, task=task, evidence=evidence, job_id=job_id)
    b = maybe_record_lesson_from_exhausted_verification(superuser_db, task=task, evidence=evidence, job_id=job_id)
    superuser_db.commit()
    assert a is not None and a.id == b.id
    assert superuser_db.query(EngineeringLesson).count() == 1
