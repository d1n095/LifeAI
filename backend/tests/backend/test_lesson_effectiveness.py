"""The learning loop's back-edge: applied lesson → what happened to its own guard.

Two things these tests are specifically written to prove, because they are the ways this
feature could be worse than not existing at all:
  1. An unrelated later success never becomes evidence a lesson worked.
  2. An observation never rewrites the lesson it observes.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.mainai_execution.execution_job import _finalize_task_outcome
from app.mainai_execution.lesson_effectiveness import (
    applied_lesson_ids,
    classify_lesson_outcome,
    record_lesson_effectiveness_from_finalize,
)
from app.mainai_execution.lessons import record_lesson
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.lesson_effectiveness import (
    EngineeringLessonEffectiveness,
    LessonEffectivenessAttributionConfidence as Confidence,
    LessonEffectivenessOutcome as Outcome,
)
from app.models.mainai_execution import (
    EngineeringLesson,
    EngineeringLessonSeverity,
    EngineeringLessonStatus,
    MainAITask,
    MainAITaskStatus,
)
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    return user.id


def _lesson(db, *, regression_test: str | None, tag: str = "run_tests") -> EngineeringLesson:
    lesson = record_lesson(
        db,
        problem="known flaky import boundary",
        root_cause="missing durable reference before cleanup",
        affected_component=f"component.{uuid.uuid4().hex[:8]}",
        severity=EngineeringLessonSeverity.medium,
        evidence="structured verification evidence",
        fix="commit the reference before retaining",
        general_rule="retain only after a durable reference exists",
        applies_to=[tag],
        source_type="test",
        source_ref=f"test:{uuid.uuid4()}",
        created_by="test",
        first_seen_at=datetime.utcnow(),
        regression_test=regression_test,
    )
    db.commit()
    return lesson


def _planned_task(db, owner_id) -> MainAITask:
    """A real plan, so `lessons_applied` provenance is written by the real planner rather
    than hand-forged onto the event — the provenance edge is what this module trusts."""
    current_user_id_var.set(str(owner_id))
    db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
    goal = create_goal(
        db,
        owner_id=owner_id,
        title="lesson effectiveness",
        original_instruction="prove the learning loop back-edge",
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
    task.status = MainAITaskStatus.running
    db.commit()
    return task


def _evidence(*, target: str | None, passed: bool, step_passed: bool = True, returncode: int = 0, error=None) -> dict:
    steps = []
    if target is not None:
        detail = {"target": target, "returncode": returncode}
        if error is not None:
            detail["error"] = error
        steps.append({"kind": "targeted_tests", "passed": step_passed, "detail": detail})
    return {"passed": passed, "steps": steps}


def test_planner_records_the_applied_lesson_provenance_this_module_reads(db_session, owner_id):
    lesson = _lesson(db_session, regression_test="tests/backend/test_something.py")
    task = _planned_task(db_session, owner_id)
    assert applied_lesson_ids(db_session, task=task) == [lesson.id]


def test_unrelated_success_is_not_reinforcement(db_session, owner_id):
    """The core causality guard: the lesson's guard did not run, so a passing task says
    nothing about it — never `reinforced`."""
    _lesson(db_session, regression_test="tests/backend/test_the_lessons_own_guard.py")
    task = _planned_task(db_session, owner_id)

    rows = record_lesson_effectiveness_from_finalize(
        db_session,
        task=task,
        evidence=_evidence(target="tests/backend/test_completely_unrelated.py", passed=True),
        passed=True,
    )
    db_session.commit()

    assert [r.outcome for r in rows] == [Outcome.insufficient_evidence.value]
    assert rows[0].attribution_confidence == Confidence.low.value


def test_lesson_guard_passing_on_a_passing_task_is_reinforced(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    lesson = _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    rows = record_lesson_effectiveness_from_finalize(
        db_session, task=task, evidence=_evidence(target=target, passed=True), passed=True
    )
    db_session.commit()

    assert len(rows) == 1
    assert rows[0].outcome == Outcome.reinforced.value
    assert rows[0].attribution_confidence == Confidence.high.value
    assert rows[0].lesson_id == lesson.id
    assert rows[0].verification_target == target
    assert rows[0].owner_id == owner_id


def test_lesson_guard_passing_on_a_failing_task_is_context_specific(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    evidence = _evidence(target=target, passed=False)
    evidence["steps"].append(
        {"kind": "targeted_tests", "passed": False, "detail": {"target": "tests/other.py", "returncode": 1}}
    )
    rows = record_lesson_effectiveness_from_finalize(db_session, task=task, evidence=evidence, passed=False)
    db_session.commit()

    assert rows[0].outcome == Outcome.context_specific.value
    assert rows[0].attribution_confidence == Confidence.medium.value


def test_lesson_guard_failing_weakens_the_lesson(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    rows = record_lesson_effectiveness_from_finalize(
        db_session,
        task=task,
        evidence=_evidence(target=target, passed=False, step_passed=False, returncode=1),
        passed=False,
    )
    db_session.commit()

    assert rows[0].outcome == Outcome.weakened.value
    assert rows[0].evidence["target_step"]["returncode"] == 1


@pytest.mark.parametrize("returncode", [4, 5])
def test_uncollectable_guard_contradicts_the_lesson(db_session, owner_id, returncode):
    """pytest exit 4/5 means the lesson names a target that is not a runnable guard at all —
    a statement about the LESSON, distinct from the guarded code failing."""
    target = "tests/backend/test_does_not_exist.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    rows = record_lesson_effectiveness_from_finalize(
        db_session,
        task=task,
        evidence=_evidence(target=target, passed=False, step_passed=False, returncode=returncode),
        passed=False,
    )
    db_session.commit()

    assert rows[0].outcome == Outcome.contradicted.value


def test_timeout_reaches_no_verdict(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    evidence = _evidence(target=target, passed=False, step_passed=False, returncode=0, error="timeout")
    evidence["steps"][0]["detail"].pop("returncode")
    rows = record_lesson_effectiveness_from_finalize(db_session, task=task, evidence=evidence, passed=False)
    db_session.commit()

    assert rows[0].outcome == Outcome.insufficient_evidence.value


def test_superseded_lesson_is_not_scored(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    lesson = _lesson(db_session, regression_test=target)
    newer = _lesson(db_session, regression_test=target, tag="other_type")
    task = _planned_task(db_session, owner_id)
    # ck_engineering_lessons_superseded_requires_link: the DB will not let one be set without
    # the other, which is exactly why the classifier checks superseded_by before status.
    lesson.superseded_by = newer.id
    lesson.status = EngineeringLessonStatus.superseded
    db_session.commit()

    rows = record_lesson_effectiveness_from_finalize(
        db_session, task=task, evidence=_evidence(target=target, passed=True), passed=True
    )
    db_session.commit()

    assert rows[0].outcome == Outcome.superseded.value


def test_ci_wait_evidence_writes_nothing(db_session, owner_id):
    """No structured steps means no place a lesson's guard could have shown up — silence,
    not a pile of insufficient_evidence rows on every CI-wait finalize."""
    _lesson(db_session, regression_test="tests/backend/test_the_lessons_own_guard.py")
    task = _planned_task(db_session, owner_id)

    rows = record_lesson_effectiveness_from_finalize(
        db_session, task=task, evidence={"ci_wait": {"conclusion": "success"}}, passed=True
    )
    db_session.commit()
    assert rows == []
    assert db_session.query(EngineeringLessonEffectiveness).count() == 0


def test_replayed_finalize_cannot_inflate_a_lessons_evidence(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    for _ in range(3):
        record_lesson_effectiveness_from_finalize(
            db_session, task=task, evidence=_evidence(target=target, passed=True), passed=True
        )
        db_session.commit()

    assert db_session.query(EngineeringLessonEffectiveness).count() == 1


def test_observation_never_mutates_the_lesson_it_observes(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    lesson = _lesson(db_session, regression_test=target)
    before = (lesson.confidence, lesson.status, lesson.evidence, lesson.root_cause)
    task = _planned_task(db_session, owner_id)

    record_lesson_effectiveness_from_finalize(
        db_session,
        task=task,
        evidence=_evidence(target=target, passed=False, step_passed=False, returncode=1),
        passed=False,
    )
    db_session.commit()
    db_session.refresh(lesson)

    assert (lesson.confidence, lesson.status, lesson.evidence, lesson.root_cause) == before


def test_finalize_gate_wires_the_back_edge_in_production(db_session, owner_id):
    """The edge must exist on the real completion gate, not only when called directly —
    otherwise this is another 'code exists, runtime does not' capability."""
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    _finalize_task_outcome(
        db_session, task, passed=True, evidence=_evidence(target=target, passed=True), job_id=None
    )
    db_session.commit()

    rows = db_session.query(EngineeringLessonEffectiveness).all()
    assert [r.outcome for r in rows] == [Outcome.reinforced.value]
    assert rows[0].task_id == task.id


def test_another_owner_cannot_read_these_effectiveness_rows(db_session, superuser_db, make_verified_user, owner_id):
    """The lesson is founder-wide, but the OBSERVATION carries owner-scoped facts (task/goal
    ids, a verification target out of that owner's plan). RLS must scope it to its evidence's
    owner, not to its subject's audience."""
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)
    record_lesson_effectiveness_from_finalize(
        db_session, task=task, evidence=_evidence(target=target, passed=True), passed=True
    )
    db_session.commit()

    # Exists for real, verified RLS-free — so "other owner sees nothing" is unambiguous.
    assert superuser_db.query(EngineeringLessonEffectiveness).count() == 1

    other, _ = make_verified_user()
    current_user_id_var.set(str(other.id))
    db_session.rollback()
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(other.id)})
    assert db_session.query(EngineeringLessonEffectiveness).count() == 0


def test_owner_id_cannot_cite_another_owners_task(db_session, superuser_db, make_verified_user, owner_id):
    """Composite owner-anchored FK, not a bare one: `owner_id=B` must be structurally unable
    to reference owner A's task — the defect class migration 0056 had to retrofit elsewhere."""
    from sqlalchemy.exc import IntegrityError

    lesson = _lesson(db_session, regression_test="tests/backend/test_the_lessons_own_guard.py")
    task = _planned_task(db_session, owner_id)
    other, _ = make_verified_user()

    with pytest.raises(IntegrityError):
        superuser_db.execute(
            sa_text("""
                INSERT INTO engineering_lesson_effectiveness
                    (id, owner_id, lesson_id, task_id, goal_id, outcome, attribution_confidence,
                     relevance_reason, evidence, source_ref)
                VALUES (gen_random_uuid(), :owner, :lesson, :task, :goal, 'reinforced', 'high',
                        'forged', '{}'::jsonb, :ref)
            """),
            {
                "owner": str(other.id),
                "lesson": str(lesson.id),
                "task": str(task.id),
                "goal": str(task.goal_id),
                "ref": f"forged:{uuid.uuid4()}",
            },
        )
    superuser_db.rollback()


def test_classifier_requires_the_lessons_own_target_not_just_any_passing_step():
    """Pure decision-table guard, no DB: the classifier must key off the lesson's OWN
    regression_test, never 'some targeted_tests step passed'."""

    class _Lesson:
        id = uuid.uuid4()
        superseded_by = None
        status = EngineeringLessonStatus.active
        regression_test = "tests/mine.py"

    evidence = {
        "passed": True,
        "steps": [{"kind": "targeted_tests", "passed": True, "detail": {"target": "tests/theirs.py", "returncode": 0}}],
    }
    outcome, confidence, _reason, _excerpt = classify_lesson_outcome(
        lesson=_Lesson(), evidence=evidence, overall_passed=True, conflicted=False
    )
    assert outcome == Outcome.insufficient_evidence
    assert confidence == Confidence.low
