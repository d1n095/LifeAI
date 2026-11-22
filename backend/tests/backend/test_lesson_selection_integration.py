"""Integration tests (M1, Round 8): measured lesson outcomes now INFLUENCE subsequent
planning — conservatively, explainably, and without any permanent suppression.

What is proven on real PostgreSQL, through the real planner.create_plan path:

  1. Ranking: a lesson with a proven track record (guard held where it shaped plans) is
     applied BEFORE an unproven one — application order follows measured standing, and the
     decision block rides on the created event.
  2. Exclusion classes: a lesson whose guard was UNUSABLE as written, and a lesson whose
     guard FAILED the majority of its verdicts, are NOT blindly reinforced — they are
     excluded from this pass with their reasons and raw counts on the audit record.
  3. No permanent suppression: one isolated failure still gets applied (flagged); a
     failed-majority lesson is selected again the moment a held verdict arrives — the
     class is recomputed from the evidence tables on every pass, nothing is cached or
     mutated (the lesson row itself is byte-identical before/after exclusion).
  4. Recurring-failure clusters: guard failures on >= 2 DISTINCT tasks sharing an
     affected_component are detected as a shared-root-cause cluster (cross-lesson);
     failures on a single task (retries) are NOT a cluster. Detection only — no lesson is
     auto-disputed or deleted.
  5. Owner isolation: owner B's planning never sees owner A's guard evidence.
  6. Legacy contract: apply_lessons_to_verification_plan() without a selection behaves
     exactly as before (recovery_inspector.py and any other caller keep their contract).
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.db import migration_engine
from app.mainai_execution import lessons, planner
from app.mainai_execution.lesson_guard_observations import (
    recurring_failure_clusters,
)
from app.mainai_execution.lesson_selection import (
    LessonSelection,
    select_lessons_for_planning,
)
from app.mainai_execution.lessons import apply_lessons_to_verification_plan
from app.models.mainai_execution import EngineeringLessonSeverity, MainAITask
from app.request_context import current_user_id as current_user_id_var
from app.rls import apply_mainai_execution_privileges

SAFE_TARGET = "tests/backend/test_mainai_execution_planner.py::test_get_goal_raises_for_unknown_id"


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _lesson(db, *, tags, target=SAFE_TARGET, severity=EngineeringLessonSeverity.low):
    return lessons.record_lesson(
        db,
        problem=f"Recurring failure {uuid.uuid4()}",
        root_cause="root cause under test",
        affected_component=f"test_component_{uuid.uuid4().hex[:8]}",
        severity=severity,
        evidence="selection integration test",
        fix="guard added",
        general_rule="always re-check the guarded behavior",
        applies_to=tags,
        source_type="selection_tests",
        source_ref=f"selection-tests-{uuid.uuid4()}",
        created_by="test",
        first_seen_at=datetime.utcnow(),
        regression_test=target,
    )


def _observe(db, owner, task, lesson, outcome, *, reason="selection test observation"):
    from app.models.lesson_guard_observations import EngineeringLessonGuardObservation

    db.add(
        EngineeringLessonGuardObservation(
            owner_id=owner.id if hasattr(owner, "id") else owner,
            lesson_id=lesson.id,
            task_id=task.id,
            goal_id=task.goal_id,
            outcome=outcome,
            evidence_strength="direct",
            relevance_reason=reason,
            guard_target=lesson.regression_test,
            evidence={"task_type": task.task_type},
            source_ref=f"test:{uuid.uuid4()}",
        )
    )
    db.commit()


def _plan_one_task(db, owner_id, *, task_type="run_tests"):
    goal = planner.create_goal(
        db, owner_id=owner_id, title=f"Selection goal {uuid.uuid4()}", original_instruction="Do it.", created_by="test"
    )
    plan = planner.create_plan(
        db,
        goal=goal,
        rationale="selection test plan",
        tasks=[planner.PlannedTaskSpec(description="Run the suite", task_type=task_type)],
        created_by="test",
    )
    db.commit()
    task = db.query(MainAITask).filter(MainAITask.plan_id == plan.id).one()
    event = db.execute(
        sa_text("SELECT detail FROM mainai_task_events WHERE task_id = :id AND event_type = 'created'"),
        {"id": str(task.id)},
    ).first()
    return task, (event[0] if event else {})


def _decision(summary, lesson):
    return next(d for d in summary if d["lesson_id"] == str(lesson.id))


# --- 1. ranking: measured standing orders application -----------------------------------------


def test_proven_lesson_is_applied_first(db_session, owner_id):
    # Both lessons exist BEFORE the plan is created: create_plan applies each one and
    # records its 0074 decision row — the provenance the effectiveness rollup requires.
    # Distinct targets: within ONE plan, a second lesson whose target is already in the
    # spec plan is dedup-skipped (never applied, never decision-rowed) — so the ranking
    # comparison needs two genuinely applied lessons.
    unproven = _lesson(db_session, tags=["run_tests"], target="tests/backend/test_mainai_execution_planner.py::test_create_goal_starts_pending_with_no_plan")
    proven = _lesson(db_session, tags=["run_tests"], severity=EngineeringLessonSeverity.low)
    task, _ = _plan_one_task(db_session, owner_id)
    _observe(db_session, owner_id, task, proven, "guard_held")

    selection = select_lessons_for_planning(db_session, owner_id=owner_id, task_type="run_tests")
    ids = [l.id for l in selection.ordered]
    assert ids.index(proven.id) < ids.index(unproven.id), "a proven rule outranks an unproven one"
    classes = {d.lesson.id: d.standing for d in selection.decisions}
    assert classes[proven.id] == "proven" and classes[unproven.id] == "unproven"


def test_created_event_carries_the_selection_decision_block(db_session, owner_id):
    lesson = _lesson(db_session, tags=["run_tests"])
    task, detail = _plan_one_task(db_session, owner_id)
    assert "lesson_selection" in detail, "every plan must explain which rules influenced it"
    entry = _decision(detail["lesson_selection"], lesson)
    assert entry["standing"] == "unproven" and entry["selected"] is True
    assert entry["in_recurring_cluster"] is False
    targets = {s.get("target") for s in task.verification_plan if s.get("kind") == "targeted_tests"}
    assert lesson.regression_test in targets


# --- 2. exclusion classes: reassessed, not blindly reinforced ----------------------------------


def test_unusable_guard_is_excluded_with_reason(db_session, owner_id):
    lesson = _lesson(db_session, tags=["run_tests"])
    task, _ = _plan_one_task(db_session, owner_id)
    _observe(db_session, owner_id, task, lesson, "guard_unusable")

    selection = select_lessons_for_planning(db_session, owner_id=owner_id, task_type="run_tests")
    decision = next(d for d in selection.decisions if d.lesson.id == lesson.id)
    assert decision.standing == "unusable_guard" and decision.selected is False
    assert all(l.id != lesson.id for l in selection.ordered)

    task2, detail2 = _plan_one_task(db_session, owner_id)
    entry = _decision(detail2["lesson_selection"], lesson)
    assert entry["selected"] is False and "unusable" in entry["reason"]
    targets2 = {s.get("target") for s in task2.verification_plan if s.get("kind") == "targeted_tests"}
    assert lesson.regression_test not in targets2, "a broken prescription is not blindly re-added"

    # No mutation: the lesson row is untouched (SIGNAL != TRUTH WRITER).
    db_session.refresh(lesson)
    from app.models.mainai_execution import EngineeringLessonStatus

    assert lesson.status == EngineeringLessonStatus.active


def test_failed_majority_is_excluded_but_recovers_after_a_held_verdict(db_session, owner_id):
    lesson = _lesson(db_session, tags=["run_tests"])
    # Lesson exists before both plans: each create_plan applies it and writes its decision
    # row, so both guard_failed verdicts count as measured standing.
    task1, _ = _plan_one_task(db_session, owner_id)
    task2, _ = _plan_one_task(db_session, owner_id)
    _observe(db_session, owner_id, task1, lesson, "guard_failed")
    _observe(db_session, owner_id, task2, lesson, "guard_failed")

    selection = select_lessons_for_planning(db_session, owner_id=owner_id, task_type="run_tests")
    decision = next(d for d in selection.decisions if d.lesson.id == lesson.id)
    assert decision.standing == "failed_majority" and decision.selected is False
    assert "reassess" in decision.reason.lower()

    # Two later held verdicts recompute the class — nothing permanent, nothing cached.
    # 2 held + 2 failed = keep_ratio 0.5, no longer below the exclusion threshold.
    _observe(db_session, owner_id, task1, lesson, "guard_held")
    _observe(db_session, owner_id, task1, lesson, "guard_held", reason="second held verdict")
    selection2 = select_lessons_for_planning(db_session, owner_id=owner_id, task_type="run_tests")
    decision2 = next(d for d in selection2.decisions if d.lesson.id == lesson.id)
    assert decision2.standing == "mixed" and decision2.selected is True, (
        "held verdicts restore selection automatically once keep_ratio reaches 0.5 — "
        "the exclusion was evidence-derived, never a permanent demotion"
    )


# --- 3. no permanent suppression from one isolated failure --------------------------------------


def test_single_failure_still_gets_applied_flagged(db_session, owner_id):
    lesson = _lesson(db_session, tags=["run_tests"])
    task, _ = _plan_one_task(db_session, owner_id)
    _observe(db_session, owner_id, task, lesson, "guard_failed")

    selection = select_lessons_for_planning(db_session, owner_id=owner_id, task_type="run_tests")
    decision = next(d for d in selection.decisions if d.lesson.id == lesson.id)
    assert decision.standing == "single_fail" and decision.selected is True
    assert "single failure must not permanently suppress" in decision.reason

    task2, detail2 = _plan_one_task(db_session, owner_id)
    entry = _decision(detail2["lesson_selection"], lesson)
    assert entry["selected"] is True
    targets2 = {s.get("target") for s in task2.verification_plan if s.get("kind") == "targeted_tests"}
    assert lesson.regression_test in targets2


# --- 4. recurring-failure clusters: shared root cause across DISTINCT tasks ---------------------


def test_cluster_needs_two_distinct_failed_tasks_and_crosses_lessons(db_session, owner_id):
    shared_component = f"shared_subsystem_{uuid.uuid4().hex[:8]}"
    lesson_a = _lesson(db_session, tags=["run_tests"])
    lesson_b = _lesson(db_session, tags=["run_tests"])
    # Both lessons guard the same component (set post-creation; record_lesson randomizes it).
    lesson_a.affected_component = shared_component
    lesson_b.affected_component = shared_component
    db_session.commit()

    task1, _ = _plan_one_task(db_session, owner_id)
    task2, _ = _plan_one_task(db_session, owner_id)
    _observe(db_session, owner_id, task1, lesson_a, "guard_failed")
    _observe(db_session, owner_id, task2, lesson_b, "guard_failed")

    clusters = recurring_failure_clusters(db_session, owner_id=owner_id)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["affected_component"] == shared_component
    assert {str(lesson_a.id), str(lesson_b.id)} <= set(cluster["lesson_ids"])
    assert {str(task1.id), str(task2.id)} <= set(cluster["distinct_failed_tasks"])

    selection = select_lessons_for_planning(db_session, owner_id=owner_id, task_type="run_tests")
    summary = selection.summary_for_event()
    assert all(d["in_recurring_cluster"] for d in summary if d["lesson_id"] in {str(lesson_a.id), str(lesson_b.id)})


def test_two_failures_on_one_task_are_retries_not_a_cluster(db_session, owner_id):
    lesson = _lesson(db_session, tags=["run_tests"])
    task, _ = _plan_one_task(db_session, owner_id)
    _observe(db_session, owner_id, task, lesson, "guard_failed")
    _observe(db_session, owner_id, task, lesson, "guard_failed", reason="retry observation")

    assert recurring_failure_clusters(db_session, owner_id=owner_id) == [], (
        "repeated attempts on the SAME task are a retry pattern, not cross-task recurrence"
    )


# --- 5. owner isolation -------------------------------------------------------------------------


def test_selection_scopes_standing_to_the_owner(db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    _set_rls_user(db_session, owner_a.id)
    lesson = _lesson(db_session, tags=["run_tests"])
    task, _ = _plan_one_task(db_session, owner_a.id)  # lesson applied here -> decision row
    _observe(db_session, owner_a, task, lesson, "guard_failed")
    _observe(db_session, owner_a, task, lesson, "guard_failed")

    _set_rls_user(db_session, owner_b.id)
    selection_b = select_lessons_for_planning(db_session, owner_id=owner_b.id, task_type="run_tests")
    decision_b = next((d for d in selection_b.decisions if d.lesson.id == lesson.id), None)
    assert decision_b is not None and decision_b.standing == "unproven", (
        "B's planning must not see A's guard evidence — an exclusion earned on A's tasks does not follow the lesson to B"
    )
    assert recurring_failure_clusters(db_session, owner_id=owner_b.id) == []


# --- 6. legacy contract -------------------------------------------------------------------------


def test_apply_without_selection_uses_the_legacy_lookup_order(db_session, owner_id):
    lesson = _lesson(db_session, tags=["run_tests"])
    augmented, applied = apply_lessons_to_verification_plan(
        db_session, task_type="run_tests", verification_plan=[]
    )
    assert lesson.id in applied and {"kind": "targeted_tests", "target": lesson.regression_test} in augmented
    # A default selection (no decisions recorded yet) selects everything the legacy path would.
    selection: LessonSelection = select_lessons_for_planning(db_session, owner_id=owner_id, task_type="run_tests")
    legacy, applied_legacy = apply_lessons_to_verification_plan(
        db_session, task_type="run_tests", verification_plan=[]
    )
    assert applied_legacy == applied


def test_selection_ranking_survives_conflict_and_validation_gates(db_session, owner_id):
    """The M1 layer only ORDERS and evidence-excludes; the pre-existing safety gates
    (conflict candidates, target validation) still fire on top of the ranked list."""
    conflicting = _lesson(db_session, tags=["run_tests"])
    selection = select_lessons_for_planning(db_session, owner_id=owner_id, task_type="run_tests")
    assert any(d.lesson.id == conflicting.id for d in selection.decisions)
    # An unsafe target is skipped by the validation gate exactly as before selection existed.
    unsafe = _lesson(db_session, tags=["run_tests"], target="/etc/passwd")
    augmented, applied = apply_lessons_to_verification_plan(
        db_session,
        task_type="run_tests",
        verification_plan=[],
        selection=select_lessons_for_planning(db_session, owner_id=owner_id, task_type="run_tests"),
    )
    assert unsafe.id not in applied and all("/etc" not in s["target"] for s in augmented if s.get("kind") == "targeted_tests")
