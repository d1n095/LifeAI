"""Integration tests: the lesson -> plan-decision PROVENANCE EDGE (migration 0074) and the
measurable-effectiveness rollup that consumes it — the piece migration 0058's own docstring
named as deliberately-not-built-yet:

    "durable provenance for HOW a lesson altered the later plan (lesson -> changed planning
    decision -> affected step -> execution -> comparable outcome). When it does, these
    observations become one input to it rather than being retrofitted into a claim they
    never supported."

What is proven here, on real PostgreSQL:
  1. planner.create_plan() WRITES the edge: a lesson whose regression target entered a
     task's verification_plan leaves one engineering_lesson_plan_decisions row saying so —
     decision_type, the exact affected step, idempotent against replay.
  2. lesson_effectiveness() counts guard observations ONLY where a decision row proves the
     lesson shaped that task's plan — a guard passing on an unrelated task is not evidence
     about the rule (the exact attribution failure 0058 warned about).
  3. keep_ratio is computed over VERDICTS only; needs_reassessment fires ONLY on
     guard_failed (the one outcome that contradicts the lesson's own claim) — never on
     unusable/not-exercised evidence, which indicts the target path or the evidence, not
     the rule. Protection against overcorrection: the rollup is a SIGNAL — it never mutates
     the lesson (status/confidence stay untouched).
  4. The overcorrection advisory (when it fires) carries the rules' standing in its audit
     detail, so the measurable half of the self-correction loop lands on a real execution
     path.
  5. Owner isolation end to end: another owner's rollup is empty; a cross-owner decision
     row is a database-level IntegrityError via the composite (task_id, owner_id) FK.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

from app.db import migration_engine
from app.mainai_execution import lessons, planner
from app.mainai_execution.lesson_guard_observations import (
    lesson_effectiveness,
    record_lesson_guard_observations_from_finalize,
)
from app.models.lesson_guard_observations import EngineeringLessonGuardObservation
from app.models.lesson_plan_decisions import EngineeringLessonPlanDecision
from app.models.mainai_execution import (
    EngineeringLesson,
    EngineeringLessonSeverity,
    MainAIGoalStatus,
    MainAIPlanStatus,
    MainAITask,
)
from app.models.audit import AuditLog
from app.request_context import current_user_id as current_user_id_var
from app.rls import apply_mainai_execution_privileges

SAFE_TARGET = "tests/backend/test_mainai_execution_planner.py::test_get_goal_raises_for_unknown_id"


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure as test_mainai_execution_planner.py's own fixture."""
    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _lesson(db, *, tags, target=SAFE_TARGET):
    return lessons.record_lesson(
        db,
        problem=f"Recurring failure {uuid.uuid4()}",
        root_cause="root cause under test",
        affected_component=f"test_component_{uuid.uuid4().hex[:8]}",
        severity=EngineeringLessonSeverity.low,
        evidence="effectiveness integration test",
        fix="guard added",
        general_rule="always re-check the guarded behavior",
        applies_to=tags,
        source_type="effectiveness_tests",
        source_ref=f"effectiveness-tests-{uuid.uuid4()}",
        created_by="test",
        first_seen_at=datetime.utcnow(),
        regression_test=target,
    )


def _plan_with_lesson_applied(db, owner_id, lesson, *, extra_spec_steps: list[dict] | None = None) -> MainAITask:
    """A goal whose single run_tests task's plan was shaped by `lesson` — the full real
    create_plan path. `extra_spec_steps` rides IN the spec's own verification_plan (founder-
    specified targets); the lesson layer only ADDS targets the spec didn't already have, so
    a lesson whose target the spec already names is never "applied" and never gets a
    decision row — the honest way to build an observation-without-provenance case."""
    goal = planner.create_goal(
        db, owner_id=owner_id, title=f"Effectiveness goal {uuid.uuid4()}", original_instruction="Do it.", created_by="test"
    )
    plan = planner.create_plan(
        db,
        goal=goal,
        rationale="effectiveness test plan",
        tasks=[planner.PlannedTaskSpec(
            description="Run the suite", task_type="run_tests",
            verification_plan=list(extra_spec_steps or []),
        )],
        created_by="test",
    )
    db.commit()
    assert plan.status == MainAIPlanStatus.active
    task = db.query(MainAITask).filter(MainAITask.plan_id == plan.id).one()
    targets = {s.get("target") for s in task.verification_plan if s.get("kind") == "targeted_tests"}
    assert lesson.regression_test in targets, "the lesson's guard must be in this task's plan"
    return task


def _observe(db, owner, task, lesson, outcome, *, strength="direct", reason="test observation"):
    row = EngineeringLessonGuardObservation(
        owner_id=owner.id if hasattr(owner, "id") else owner,
        lesson_id=lesson.id,
        task_id=task.id,
        goal_id=task.goal_id,
        outcome=outcome,
        evidence_strength=strength,
        relevance_reason=reason,
        guard_target=lesson.regression_test,
        evidence={"task_type": task.task_type, "overall_passed": outcome != "guard_failed"},
        source_ref=f"test:{uuid.uuid4()}",
    )
    db.add(row)
    db.commit()
    return row


# --- 1. the edge is written where the change happens -----------------------------------------


def test_plan_decision_written_when_lesson_shapes_the_plan(db_session, owner_id):
    lesson = _lesson(db_session, tags=["run_tests"])
    task = _plan_with_lesson_applied(db_session, owner_id, lesson)

    rows = db_session.query(EngineeringLessonPlanDecision).filter_by(task_id=task.id).all()
    assert len(rows) == 1
    assert rows[0].lesson_id == lesson.id
    assert rows[0].decision_type == "verification_step_added"
    assert rows[0].affected_step == {"kind": "targeted_tests", "target": lesson.regression_test}
    assert rows[0].goal_id == task.goal_id

    # The event-history provenance string is unchanged (defense in depth now has a queryable edge).
    events = db_session.execute(
        sa_text("SELECT detail FROM mainai_task_events WHERE task_id = :id"), {"id": str(task.id)}
    ).fetchall()
    assert any("lessons_applied" in (e[0] or {}) for e in events)


def test_replayed_writer_cannot_inflate_a_lessons_applied_count(db_session, owner_id):
    lesson = _lesson(db_session, tags=["run_tests"])
    task = _plan_with_lesson_applied(db_session, owner_id, lesson)

    written_again = lessons.record_lesson_plan_decisions(
        db_session, task=task, applied_lesson_ids=[lesson.id]
    )
    db_session.commit()
    assert written_again == 1  # attempted
    assert (
        db_session.query(EngineeringLessonPlanDecision).filter_by(task_id=task.id).count() == 1
    ), "ON CONFLICT DO NOTHING: a replayed pass cannot duplicate the decision"


def test_decision_survives_in_its_own_row_after_replan_supersedes_the_plan(db_session, owner_id):
    """A replan supersedes the old plan but never edits history: the first plan's decision
    rows stand; the new plan's lesson application writes its OWN rows for the NEW tasks."""
    lesson = _lesson(db_session, tags=["run_tests"])
    first_task = _plan_with_lesson_applied(db_session, owner_id, lesson)
    from app.models.mainai_execution import MainAIGoal

    goal = db_session.query(MainAIGoal).filter(MainAIGoal.id == first_task.goal_id).one()
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="replan",
        tasks=[planner.PlannedTaskSpec(description="Run again", task_type="run_tests")],
        created_by="test",
    )
    db_session.commit()

    decisions = (
        db_session.query(EngineeringLessonPlanDecision)
        .filter_by(lesson_id=lesson.id)
        .order_by(EngineeringLessonPlanDecision.decided_at)
        .all()
    )
    assert len(decisions) == 2, "each plan the lesson actually shaped has its own decision row"
    assert len({d.task_id for d in decisions}) == 2


# --- 2. observations count ONLY where the lesson shaped the plan ------------------------------


def test_effectiveness_counts_observations_only_where_a_decision_exists(db_session, owner_id):
    shaped_lesson = _lesson(db_session, tags=["run_tests"])
    # Its target rides IN the spec plan, so the lesson layer skips it: no decision row, even
    # though this task's plan genuinely runs that target — the honest "unrelated evidence" case.
    unshaped_lesson = _lesson(db_session, tags=["run_tests"], target="tests/backend/test_lesson_effectiveness_integration.py")
    task = _plan_with_lesson_applied(
        db_session, owner_id, shaped_lesson,
        extra_spec_steps=[{"kind": "targeted_tests", "target": unshaped_lesson.regression_test}],
    )

    # unshaped_lesson's guard "held" on this task — but NO decision row proves it ever
    # shaped this plan. Direct observation insertion stands in for a finalize that saw the
    # target for an unrelated reason.
    _observe(db_session, owner_id, task, unshaped_lesson, "guard_held")

    standing = lesson_effectiveness(db_session, owner_id=owner_id)
    assert all(s.lesson_id != unshaped_lesson.id for s in standing), (
        "a lesson with no plan-decision edge has NO effectiveness standing from this task"
    )

    # Now the shaped lesson's finalize: its guard held on the task it actually shaped.
    _observe(db_session, owner_id, task, shaped_lesson, "guard_held")
    standing = {s.lesson_id: s for s in lesson_effectiveness(db_session, owner_id=owner_id)}
    entry = standing[shaped_lesson.id]
    assert entry.applied_count == 1
    assert entry.guard_held == 1 and entry.guard_failed == 0
    assert entry.keep_ratio == 1.0
    assert entry.needs_reassessment is False


def test_keep_ratio_counts_verdicts_only_and_reassessment_needs_a_real_failure(db_session, owner_id):
    lesson = _lesson(db_session, tags=["run_tests"])
    task = _plan_with_lesson_applied(db_session, owner_id, lesson)
    _observe(db_session, owner_id, task, lesson, "guard_unusable")
    _observe(db_session, owner_id, task, lesson, "guard_not_exercised")

    entry = lesson_effectiveness(db_session, owner_id=owner_id)[0]
    assert entry.applied_count == 1
    assert entry.guard_unusable == 1 and entry.guard_not_exercised == 1
    assert entry.keep_ratio is None, "no verdict was ever reached — no ratio may be invented"
    assert entry.needs_reassessment is False, "unusable/not-exercised evidence indicts the path, not the rule"

    _observe(db_session, owner_id, task, lesson, "guard_failed")
    entry = lesson_effectiveness(db_session, owner_id=owner_id)[0]
    assert entry.guard_failed == 1
    assert entry.needs_reassessment is True, "the lesson's own claim was contradicted where it was applied"


def test_held_plus_failed_are_both_verdicts_in_the_ratio(db_session, owner_id):
    lesson = _lesson(db_session, tags=["run_tests"])
    task = _plan_with_lesson_applied(db_session, owner_id, lesson)
    _observe(db_session, owner_id, task, lesson, "guard_held")
    _observe(db_session, owner_id, task, lesson, "guard_held_task_failed_elsewhere")
    _observe(db_session, owner_id, task, lesson, "guard_failed")

    entry = lesson_effectiveness(db_session, owner_id=owner_id)[0]
    assert entry.guard_held == 2 and entry.guard_failed == 1
    assert entry.keep_ratio == pytest.approx(2 / 3)


def test_needs_reassessment_is_a_signal_and_never_mutates_the_lesson(db_session, owner_id):
    lesson = _lesson(db_session, tags=["run_tests"])
    task = _plan_with_lesson_applied(db_session, owner_id, lesson)
    _observe(db_session, owner_id, task, lesson, "guard_failed")
    before_status, before_confidence = lesson.status, lesson.confidence

    lesson_effectiveness(db_session, owner_id=owner_id)
    db_session.refresh(lesson)
    assert lesson.status == before_status and lesson.confidence == before_confidence, (
        "the rollup is a SIGNAL PRODUCER, never a truth writer — it must not touch the lesson"
    )


# --- 3. the advisory carries the rules' standing ----------------------------------------------


def test_overcorrection_advisory_detail_carries_lesson_standing(db_session, owner_id):
    # The FAILING lesson is tagged run_tests: it must actually be applied to a real plan
    # (the create_plan path) so its decision + observation rows exist — the standing the
    # advisory reports comes from the 0074 edge, not from thin air. The other five
    # dominating lessons only ride in the advisory's applied list (same shape as the
    # pre-existing advisory tests), so dummy targets are fine for them.
    failing = _lesson(db_session, tags=["run_tests"])
    dom_lessons = [failing] + [
        _lesson(db_session, tags=["repo_edit"], target=f"tests/backend/test_dummy_{uuid.uuid4().hex[:8]}.py")
        for _ in range(5)
    ]

    task = _plan_with_lesson_applied(db_session, owner_id, failing)
    _observe(db_session, owner_id, task, failing, "guard_failed")

    spec_plan = [{"kind": "targeted_tests", "target": "tests/backend/test_real_work.py"}]
    augmented = spec_plan + [{"kind": "targeted_tests", "target": l.regression_test} for l in dom_lessons]

    lessons.record_lesson_guard_advisory(
        db_session,
        owner_id=owner_id,
        goal_id=task.goal_id,
        task_type="repo_edit",
        spec_verification_plan=spec_plan,
        augmented_verification_plan=augmented,
        applied_lesson_ids=[l.id for l in dom_lessons],
    )
    db_session.commit()

    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "lesson_guard_overcorrection_advisory")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert row is not None
    assert "NEEDS REASSESSMENT" in row.detail, "the audit reader must see the failing rule's standing"
    assert str(failing.id) in row.detail


# --- 4. owner isolation ------------------------------------------------------------------------


def test_effectiveness_rollup_scopes_to_the_owner(db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    _set_rls_user(db_session, owner_a.id)
    lesson = _lesson(db_session, tags=["run_tests"])
    task = _plan_with_lesson_applied(db_session, owner_a.id, lesson)
    _observe(db_session, owner_a, task, lesson, "guard_held")

    _set_rls_user(db_session, owner_b.id)
    assert lesson_effectiveness(db_session, owner_id=owner_b.id) == []
    _set_rls_user(db_session, owner_a.id)
    assert len(lesson_effectiveness(db_session, owner_id=owner_a.id)) == 1


def test_composite_fk_rejects_a_cross_owner_decision_row(db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    _set_rls_user(db_session, owner_a.id)  # goal/task creation runs under owner A's RLS GUC
    lesson = _lesson(db_session, tags=["run_tests"])
    task = _plan_with_lesson_applied(db_session, owner_a.id, lesson)

    owner_b, _ = make_verified_user()
    _set_rls_user(db_session, owner_b.id)
    forged = EngineeringLessonPlanDecision(
        owner_id=owner_b.id, lesson_id=lesson.id, task_id=task.id, goal_id=task.goal_id,
        decision_type="verification_step_added", affected_step={},
    )
    db_session.add(forged)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_finalize_writer_and_the_edge_stay_consistent(db_session, owner_id):
    """The production finalize path (record_lesson_guard_observations_from_finalize) reads
    applied_lesson_ids from the created event — the same lesson set the 0074 edge recorded
    at plan time. After a finalize, the rollup must join BOTH halves for that task."""
    lesson = _lesson(db_session, tags=["run_tests"])
    task = _plan_with_lesson_applied(db_session, owner_id, lesson)

    evidence = {
        "steps": [
            # classify_guard_outcome's step shape: the target lives under step["detail"],
            # the verdict in step["passed"] (see _target_step/classify_guard_outcome).
            {
                "kind": "targeted_tests",
                "passed": True,
                "detail": {"target": lesson.regression_test, "returncode": 0},
            },
        ],
        "passed": True,
    }
    written = record_lesson_guard_observations_from_finalize(
        db_session, task=task, evidence=evidence, passed=True, job_id=None
    )
    db_session.commit()
    assert len(written) == 1 and written[0].outcome == "guard_held"

    entry = lesson_effectiveness(db_session, owner_id=owner_id)[0]
    assert entry.applied_count == 1 and entry.guard_held == 1 and entry.keep_ratio == 1.0
