"""The learning loop's back-edge: applied lesson → what happened to its own guard.

Three things these tests are specifically written to prove, because they are the ways this
feature could be worse than not existing at all:
  1. An unrelated later success never becomes evidence about a lesson.
  2. An observation never rewrites the lesson it observes.
  3. A positive observation claims only that the guard HELD here — the outcome vocabulary
     itself must stay incapable of expressing "the lesson worked", since applying a lesson
     currently only injects its regression_test into the verification plan.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.mainai_execution.execution_job import _finalize_task_outcome
from app.mainai_execution.lesson_guard_observations import (
    applied_lesson_ids,
    classify_guard_outcome,
    record_lesson_guard_observations_from_finalize,
)
from app.mainai_execution.lessons import record_lesson
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.lesson_guard_observations import (
    EngineeringLessonGuardObservation,
    LessonGuardEvidenceStrength as Strength,
    LessonGuardOutcome as Outcome,
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
        title="lesson guard observations",
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


def test_unrelated_success_is_no_evidence_about_the_lesson(db_session, owner_id):
    """The core causality guard: the lesson's guard did not run, so a passing task says
    nothing about it — never a positive observation."""
    _lesson(db_session, regression_test="tests/backend/test_the_lessons_own_guard.py")
    task = _planned_task(db_session, owner_id)

    rows = record_lesson_guard_observations_from_finalize(
        db_session,
        task=task,
        evidence=_evidence(target="tests/backend/test_completely_unrelated.py", passed=True),
        passed=True,
    )
    db_session.commit()

    assert [r.outcome for r in rows] == [Outcome.guard_not_exercised.value]
    assert rows[0].evidence_strength == Strength.none.value


def test_lesson_guard_passing_on_a_passing_task_records_guard_held(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    lesson = _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    rows = record_lesson_guard_observations_from_finalize(
        db_session, task=task, evidence=_evidence(target=target, passed=True), passed=True
    )
    db_session.commit()

    assert len(rows) == 1
    assert rows[0].outcome == Outcome.guard_held.value
    assert rows[0].evidence_strength == Strength.direct.value
    assert rows[0].lesson_id == lesson.id
    assert rows[0].guard_target == target
    assert rows[0].owner_id == owner_id


def test_lesson_guard_passing_on_a_failing_task_is_scoped_to_the_guard(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    evidence = _evidence(target=target, passed=False)
    evidence["steps"].append(
        {"kind": "targeted_tests", "passed": False, "detail": {"target": "tests/other.py", "returncode": 1}}
    )
    rows = record_lesson_guard_observations_from_finalize(db_session, task=task, evidence=evidence, passed=False)
    db_session.commit()

    assert rows[0].outcome == Outcome.guard_held_task_failed_elsewhere.value
    assert rows[0].evidence_strength == Strength.partial.value


def test_lesson_guard_failing_is_recorded_against_the_lesson(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    rows = record_lesson_guard_observations_from_finalize(
        db_session,
        task=task,
        evidence=_evidence(target=target, passed=False, step_passed=False, returncode=1),
        passed=False,
    )
    db_session.commit()

    assert rows[0].outcome == Outcome.guard_failed.value
    assert rows[0].evidence["target_step"]["returncode"] == 1


@pytest.mark.parametrize("returncode", [4, 5])
def test_uncollectable_guard_is_recorded_as_unusable(db_session, owner_id, returncode):
    """pytest exit 4/5 means the lesson names a target that is not a runnable guard at all —
    a statement about the LESSON, distinct from the guarded code failing."""
    target = "tests/backend/test_does_not_exist.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    rows = record_lesson_guard_observations_from_finalize(
        db_session,
        task=task,
        evidence=_evidence(target=target, passed=False, step_passed=False, returncode=returncode),
        passed=False,
    )
    db_session.commit()

    assert rows[0].outcome == Outcome.guard_unusable.value


def test_timeout_reaches_no_verdict(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    evidence = _evidence(target=target, passed=False, step_passed=False, returncode=0, error="timeout")
    evidence["steps"][0]["detail"].pop("returncode")
    rows = record_lesson_guard_observations_from_finalize(db_session, task=task, evidence=evidence, passed=False)
    db_session.commit()

    assert rows[0].outcome == Outcome.guard_not_exercised.value


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

    rows = record_lesson_guard_observations_from_finalize(
        db_session, task=task, evidence=_evidence(target=target, passed=True), passed=True
    )
    db_session.commit()

    assert rows[0].outcome == Outcome.lesson_superseded.value


def test_ci_wait_evidence_writes_nothing(db_session, owner_id):
    """No structured steps means no place a lesson's guard could have shown up — silence,
    not a pile of insufficient_evidence rows on every CI-wait finalize."""
    _lesson(db_session, regression_test="tests/backend/test_the_lessons_own_guard.py")
    task = _planned_task(db_session, owner_id)

    rows = record_lesson_guard_observations_from_finalize(
        db_session, task=task, evidence={"ci_wait": {"conclusion": "success"}}, passed=True
    )
    db_session.commit()
    assert rows == []
    assert db_session.query(EngineeringLessonGuardObservation).count() == 0


def test_replayed_finalize_cannot_inflate_a_lessons_evidence(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    for _ in range(3):
        record_lesson_guard_observations_from_finalize(
            db_session, task=task, evidence=_evidence(target=target, passed=True), passed=True
        )
        db_session.commit()

    assert db_session.query(EngineeringLessonGuardObservation).count() == 1


def test_observation_never_mutates_the_lesson_it_observes(db_session, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    lesson = _lesson(db_session, regression_test=target)
    before = (lesson.confidence, lesson.status, lesson.evidence, lesson.root_cause)
    task = _planned_task(db_session, owner_id)

    record_lesson_guard_observations_from_finalize(
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

    rows = db_session.query(EngineeringLessonGuardObservation).all()
    assert [r.outcome for r in rows] == [Outcome.guard_held.value]
    assert rows[0].task_id == task.id


def test_another_owner_cannot_read_these_observation_rows(db_session, superuser_db, make_verified_user, owner_id):
    """The lesson is founder-wide, but the OBSERVATION carries owner-scoped facts (task/goal
    ids, a verification target out of that owner's plan). RLS must scope it to its evidence's
    owner, not to its subject's audience."""
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)
    record_lesson_guard_observations_from_finalize(
        db_session, task=task, evidence=_evidence(target=target, passed=True), passed=True
    )
    db_session.commit()

    # Exists for real, verified RLS-free — so "other owner sees nothing" is unambiguous.
    assert superuser_db.query(EngineeringLessonGuardObservation).count() == 1

    other, _ = make_verified_user()
    current_user_id_var.set(str(other.id))
    db_session.rollback()
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(other.id)})
    assert db_session.query(EngineeringLessonGuardObservation).count() == 0


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
                INSERT INTO engineering_lesson_guard_observations
                    (id, owner_id, lesson_id, task_id, goal_id, outcome, evidence_strength,
                     relevance_reason, evidence, source_ref)
                VALUES (gen_random_uuid(), :owner, :lesson, :task, :goal, 'guard_held', 'direct',
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


def _job(db, owner_id):
    from app.models.mainai_job import MainAIJob, MainAIJobStatus

    job = MainAIJob(
        owner_id=owner_id,
        job_type="task_execution",
        status=MainAIJobStatus.queued,
        input_refs=[],
        output_refs=[],
        created_by="test",
    )
    db.add(job)
    db.commit()
    return job


def test_deleting_the_referenced_job_keeps_the_observation_and_its_owner(db_session, superuser_db, owner_id):
    """Composite-FK delete attack. `owner_id` is NOT NULL but participates in the composite FK
    to mainai_jobs(id, owner_id); a plain `ON DELETE SET NULL` would try to null BOTH columns
    and the job delete would fail outright on the NOT NULL constraint, making an unrelated
    observation able to block job cleanup. Only `job_id` may be nulled — the observation
    survives, still owned."""
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)
    job = _job(db_session, owner_id)

    record_lesson_guard_observations_from_finalize(
        db_session, task=task, evidence=_evidence(target=target, passed=True), passed=True, job_id=job.id
    )
    db_session.commit()

    superuser_db.execute(sa_text("DELETE FROM mainai_jobs WHERE id = :j"), {"j": str(job.id)})
    superuser_db.commit()

    row = superuser_db.execute(
        sa_text("SELECT owner_id, job_id FROM engineering_lesson_guard_observations WHERE task_id = :t"),
        {"t": str(task.id)},
    ).one()
    assert row[1] is None, "job_id must be nulled when the job is deleted"
    assert str(row[0]) == str(owner_id), "owner_id must survive — the observation is still that owner's"


def test_account_erasure_removes_the_observation(db_session, superuser_db, owner_id):
    """Owner erasure, proven rather than assumed -- a new owner-scoped table that erasure does
    not reach is a silent GDPR hole, not a visible failure, so this could not be left to
    inspection.

    Attacking this is what established which mechanism actually applies, and two assumptions
    died on the way. First: `erase_own_mainai_execution_children()` does NOT clear these rows
    via the composite task/goal FKs -- it only clears the append-only children whose triggers
    block direct deletes, and it deletes no goals at all. Second: `erase_account_data()` never
    calls it. What actually reaches these rows is that function's last statement,
    `db.delete(locked_user)`, cascading through `owner_id -> users.id ON DELETE CASCADE` -- so
    that is the edge exercised here.

    The authorized-erasure GUC is set explicitly because the cascade also passes through
    `mainai_task_events`, whose append-only trigger rejects DELETE without it. That is NOT a
    property of this table: probing it uncovered a separate, pre-existing Class-A defect --
    `erase_account_data()` never sets that GUC or calls
    `erase_own_mainai_execution_children()`, so DELETE /api/account currently FAILS outright
    for any owner who has ever run a MainAI goal. That is fixed in its own PR, deliberately not
    smuggled in here. Setting the GUC is exactly what that fix makes the production path do,
    so this test is written against the corrected sequence rather than encoding the bug.

    Two things must hold, and the second could have broken silently: the rows are gone, and the
    user delete SUCCEEDS with this table present -- a RESTRICT-shaped FK here would have made
    every account deletion fail."""
    target = "tests/backend/test_the_lessons_own_guard.py"
    _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    record_lesson_guard_observations_from_finalize(
        db_session, task=task, evidence=_evidence(target=target, passed=True), passed=True
    )
    db_session.commit()
    assert superuser_db.query(EngineeringLessonGuardObservation).count() == 1

    superuser_db.execute(sa_text("SET LOCAL app.mainai_execution_erasure_in_progress = 'on'"))
    superuser_db.execute(sa_text("DELETE FROM users WHERE id = :u"), {"u": str(owner_id)})
    superuser_db.commit()

    superuser_db.expire_all()
    assert superuser_db.query(EngineeringLessonGuardObservation).count() == 0


def test_deleting_the_lesson_removes_its_observations(db_session, superuser_db, owner_id):
    target = "tests/backend/test_the_lessons_own_guard.py"
    lesson = _lesson(db_session, regression_test=target)
    task = _planned_task(db_session, owner_id)

    record_lesson_guard_observations_from_finalize(
        db_session, task=task, evidence=_evidence(target=target, passed=True), passed=True
    )
    db_session.commit()

    superuser_db.execute(sa_text("DELETE FROM engineering_lessons WHERE id = :l"), {"l": str(lesson.id)})
    superuser_db.commit()
    assert superuser_db.query(EngineeringLessonGuardObservation).count() == 0


def test_job_fk_nulls_only_the_job_column(superuser_db):
    """Pins the mechanism the test above only observes the effect of. `confdelsetcols` is what
    makes the difference between a working job delete and one that dies on owner_id's NOT NULL
    constraint, and the SQLAlchemy model cannot render a column list -- so nothing but this
    assertion would notice if a later autogenerated migration reverted it to a plain SET NULL."""
    conname, deltype, setcols = superuser_db.execute(
        sa_text("""
            SELECT conname, confdeltype, confdelsetcols FROM pg_constraint
            WHERE conname = 'fk_engineering_lesson_guard_observations_job_owner'
        """)
    ).one()
    assert deltype == "n", "the job reference must be SET NULL, not CASCADE/RESTRICT"
    job_col = superuser_db.execute(
        sa_text("""
            SELECT attnum FROM pg_attribute
            WHERE attrelid = 'engineering_lesson_guard_observations'::regclass AND attname = 'job_id'
        """)
    ).scalar_one()
    assert setcols == [job_col], (
        f"{conname} must null ONLY job_id; an empty column list means SET NULL also targets the "
        "NOT NULL owner_id and every mainai_jobs delete fails"
    )


def test_no_outcome_value_can_express_a_causal_effectiveness_claim():
    """The vocabulary itself is the guard rail. Anything reading these rows without reading the
    module docstring -- a future aggregator, a founder-facing surface, MainAI reasoning over its
    own history -- sees only what was observed about the guard. If someone later adds an outcome
    named for a verdict on the lesson, this fails and forces the provenance edge to be built
    first (lesson -> changed planning decision -> execution -> comparable outcome)."""
    from app.models.lesson_guard_observations import LessonGuardOutcome

    forbidden = {"effective", "reinforced", "worked", "helped", "caused", "improved", "successful"}
    for value in LessonGuardOutcome:
        assert not (forbidden & set(value.value.split("_"))), (
            f"{value.value!r} asserts something about the LESSON that a passing regression guard "
            "does not prove; name the observation, not a verdict"
        )


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
    outcome, strength, _reason, _excerpt = classify_guard_outcome(
        lesson=_Lesson(), evidence=evidence, overall_passed=True, conflicted=False
    )
    assert outcome == Outcome.guard_not_exercised
    assert strength == Strength.none
