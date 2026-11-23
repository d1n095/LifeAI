"""Tests for app/mainai_execution/correction_intelligence.py — the P3 self-correction
increment: recurrence detection over LifeProblem, deterministic root-cause classification
in the CLOSED MainAIJobErrorCategory vocabulary, guard-observation effectiveness
aggregates (honestly framed, never causal), and the advisory overcorrection gate.

Recurrence/effectiveness run against the real local Postgres (RLS discipline: the RLS
contextvar is re-set per owner before each read). The overcorrection gate is a pure
function — no DB needed.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import text as sa_text

from app.mainai_execution.correction_intelligence import (
    OvercorrectionReport,
    classify_correction_root_cause,
    correction_effectiveness,
    detect_recurring_problems,
    normalize_problem_signature,
    overcorrection_risk,
)
from app.models.lesson_guard_observations import EngineeringLessonGuardObservation
from app.models.mainai_execution import (
    EngineeringLesson,
    EngineeringLessonSeverity,
    MainAIGoal,
    MainAIPlan,
    MainAITask,
)
from app.models.mainai_job import MainAIJobErrorCategory
from app.models.problem_learning import LifeProblem
from app.request_context import current_user_id as current_user_id_var


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_problem(session, owner_id, title, *, days_ago=0, resolved=False):
    _set_rls_user(session, owner_id)
    problem = LifeProblem(
        owner_id=owner_id,
        title=title,
        description="Reproduced in the correction-intelligence test run.",
        idempotency_key=f"test-{uuid.uuid4()}",
        created_at=datetime.utcnow() - timedelta(days=days_ago),
        resolved_at=(datetime.utcnow() - timedelta(days=days_ago)) if resolved else None,
    )
    session.add(problem)
    session.commit()
    return problem


def _make_lesson(session, created_by, problem_text="Retry without backoff exhausts the provider quota"):
    lesson = EngineeringLesson(
        problem=problem_text,
        root_cause="no backoff between attempts",
        affected_component="providers",
        severity=EngineeringLessonSeverity.medium,
        evidence="soak run 3",
        fix="exponential backoff added",
        regression_test="tests/backend/mainai/test_provider_backoff.py",
        general_rule="Retry with exponential backoff instead of fixed tight retries.",
        source_type="founder_correction",
        source_ref="correction-intelligence-tests",
        first_seen_at=datetime.utcnow() - timedelta(days=21),
        created_by=created_by,
        created_at=datetime.utcnow(),
    )
    session.add(lesson)
    session.commit()
    return lesson


def _make_task_fixture(session, owner_id):
    """A real goal → plan → task chain: the guard-observation composite FKs anchor
    (task_id, owner_id) and (goal_id, owner_id) against mainai_tasks/mainai_goals."""
    _set_rls_user(session, owner_id)
    goal = MainAIGoal(owner_id=owner_id, title="T", original_instruction="T", created_by="test")
    session.add(goal)
    session.commit()
    plan = MainAIPlan(goal_id=goal.id, owner_id=owner_id, version=1, rationale="test", created_by="test")
    session.add(plan)
    session.commit()
    task = MainAITask(goal_id=goal.id, plan_id=plan.id, owner_id=owner_id, description="T", task_type="repo_edit")
    session.add(task)
    session.commit()
    return goal, task


def _observe(session, owner_id, lesson, outcome, *, days_ago=0, goal=None, task=None):
    _set_rls_user(session, owner_id)
    observation = EngineeringLessonGuardObservation(
        owner_id=owner_id,
        lesson_id=lesson.id,
        task_id=task.id,
        goal_id=goal.id,
        job_id=None,
        outcome=outcome,
        evidence_strength="direct",
        relevance_reason="lesson applied at plan time",
        guard_target=lesson.regression_test,
        source_ref=f"finalize:{uuid.uuid4()}",
        observed_at=datetime.utcnow() - timedelta(days=days_ago),
        created_at=datetime.utcnow() - timedelta(days=days_ago),
    )
    session.add(observation)
    session.commit()
    return observation


# --- recurrence detection ---------------------------------------------------------------


def test_identical_titles_within_window_are_detected_as_recurring(db_session, make_verified_user):
    user, _ = make_verified_user()
    _make_problem(db_session, user.id, "Import fails on nested zip archives", days_ago=30)
    _make_problem(db_session, user.id, "  import fails on nested zip archives  ", days_ago=10)
    _make_problem(db_session, user.id, "IMPORT FAILS ON NESTED ZIP ARCHIVES", days_ago=2)

    groups = detect_recurring_problems(db_session, user.id, window_days=90, min_occurrences=2)
    assert len(groups) == 1
    group = groups[0]
    assert group.occurrences == 3
    assert len(group.problem_ids) == 3
    assert group.unresolved_last is True


def test_single_occurrence_is_never_a_recurrence(db_session, make_verified_user):
    user, _ = make_verified_user()
    _make_problem(db_session, user.id, "One-off spelling drift in report titles")
    assert detect_recurring_problems(db_session, user.id) == []


def test_recurrence_outside_the_window_does_not_count(db_session, make_verified_user):
    user, _ = make_verified_user()
    _make_problem(db_session, user.id, "Deploy pipeline stalls on registry rate limits", days_ago=200)
    _make_problem(db_session, user.id, "Deploy pipeline stalls on registry rate limits", days_ago=190)
    assert detect_recurring_problems(db_session, user.id, window_days=90) == []


def test_recurrence_scopes_to_owner(db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    _make_problem(db_session, owner_a.id, "Scheduler double-fires at midnight")
    _make_problem(db_session, owner_a.id, "Scheduler double-fires at midnight", days_ago=1)
    _make_problem(db_session, owner_b.id, "Scheduler double-fires at midnight")

    # RLS discipline: the contextvar points at the LAST insert — re-set per read.
    _set_rls_user(db_session, owner_a.id)
    a_groups = detect_recurring_problems(db_session, owner_a.id)
    assert len(a_groups) == 1 and a_groups[0].occurrences == 2
    _set_rls_user(db_session, owner_b.id)
    assert detect_recurring_problems(db_session, owner_b.id) == []


def test_resolved_recurrence_reports_unresolved_last_false(db_session, make_verified_user):
    user, _ = make_verified_user()
    _make_problem(db_session, user.id, "Cron lock races on restart", days_ago=5, resolved=True)
    _make_problem(db_session, user.id, "Cron lock races on restart", days_ago=1, resolved=True)

    groups = detect_recurring_problems(db_session, user.id)
    assert groups[0].unresolved_last is False


# --- root-cause classification ----------------------------------------------------------


def test_recorded_error_category_is_authoritative():
    assert classify_correction_root_cause(error_category="timeout", text="connection refused") == MainAIJobErrorCategory.timeout


def test_free_text_maps_through_the_keyword_table():
    assert classify_correction_root_cause(text="Task timed out after lease expiry") == MainAIJobErrorCategory.timeout
    assert classify_correction_root_cause(text="connection refused while shipping logs") == MainAIJobErrorCategory.transient_io
    assert classify_correction_root_cause(text="Permission denied for storage bucket") == MainAIJobErrorCategory.capability_unavailable


def test_unknown_text_stays_unexpected_never_guessed():
    assert classify_correction_root_cause(text="the moon was full") == MainAIJobErrorCategory.unexpected
    assert classify_correction_root_cause(error_category="gibberish", text="") == MainAIJobErrorCategory.unexpected


# --- correction effectiveness (observation aggregates, never causal) --------------------


def test_effectiveness_aggregates_guard_outcomes_honestly(db_session, make_verified_user):
    user, _ = make_verified_user()
    lesson = _make_lesson(db_session, user.id)
    goal, task = _make_task_fixture(db_session, user.id)
    _observe(db_session, user.id, lesson, "guard_held", days_ago=3, goal=goal, task=task)
    _observe(db_session, user.id, lesson, "guard_held_task_failed_elsewhere", days_ago=2, goal=goal, task=task)
    _observe(db_session, user.id, lesson, "guard_failed", days_ago=1, goal=goal, task=task)
    _observe(db_session, user.id, lesson, "guard_not_exercised", days_ago=1, goal=goal, task=task)

    report = correction_effectiveness(db_session, user.id, lesson)
    assert report.exercised_total == 3
    assert report.held_total == 2
    assert report.failed_total == 1
    assert report.not_exercised_total == 1
    assert report.first_observed_at < report.last_observed_at
    assert "never that the lesson caused" in report.interpretation


def test_effectiveness_recurrence_delta_is_reported_as_an_observation(db_session, make_verified_user):
    user, _ = make_verified_user()
    lesson = _make_lesson(db_session, user.id)  # created now → before-window occurrences count
    _make_problem(db_session, user.id, "retry without backoff exhausts the provider quota", days_ago=20)
    report = correction_effectiveness(db_session, user.id, lesson)
    assert report.recurrence_delta == -1  # one before, zero after — an observation, not a claim

    _make_problem(db_session, user.id, "Retry without backoff exhausts the provider quota", days_ago=0)
    report2 = correction_effectiveness(db_session, user.id, lesson)
    assert report2.recurrence_delta == 0


def test_effectiveness_scopes_observations_to_owner(db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    lesson = _make_lesson(db_session, owner_a.id)  # founder-wide lesson, many owners' evidence
    goal_a, task_a = _make_task_fixture(db_session, owner_a.id)
    goal_b, task_b = _make_task_fixture(db_session, owner_b.id)
    _observe(db_session, owner_a.id, lesson, "guard_held", goal=goal_a, task=task_a)
    _observe(db_session, owner_b.id, lesson, "guard_failed", goal=goal_b, task=task_b)

    _set_rls_user(db_session, owner_a.id)
    report_a = correction_effectiveness(db_session, owner_a.id, lesson)
    assert report_a.held_total == 1 and report_a.failed_total == 0
    _set_rls_user(db_session, owner_b.id)
    report_b = correction_effectiveness(db_session, owner_b.id, lesson)
    assert report_b.held_total == 0 and report_b.failed_total == 1


# --- overcorrection gate (pure function) ------------------------------------------------


def _plan(*targets, lesson_targets=None):
    steps = [{"kind": "targeted_tests", "target": t} for t in targets]
    return steps, set(lesson_targets or [])


def test_balanced_plan_is_acceptable():
    steps, lesson_targets = _plan("tests/test_new_feature.py", "tests/test_regression_guard.py", lesson_targets=["tests/test_regression_guard.py"])
    report = overcorrection_risk(steps, lesson_targets=lesson_targets)
    assert isinstance(report, OvercorrectionReport)
    assert report.acceptable is True
    assert report.risks == []


def test_plan_dominated_by_lesson_guards_is_flagged():
    steps, lesson_targets = _plan(
        "g1", "g2", "g3", "g4", "g5", "g6", "real_check",
        lesson_targets={"g1", "g2", "g3", "g4", "g5", "g6"},
    )
    report = overcorrection_risk(steps, lesson_targets=lesson_targets)
    assert report.acceptable is False
    assert any("review cap" in r for r in report.risks)
    assert any("of the plan" in r for r in report.risks)


def test_duplicate_and_shadowed_guard_targets_are_flagged():
    # Lesson augmentation appended a guard whose target the founder had ALREADY authored —
    # the plan records both, so founder_targets must be passed for the shadow check.
    steps = [{"kind": "targeted_tests", "target": "tests/test_same.py"}]
    report = overcorrection_risk(
        steps + [{"kind": "targeted_tests", "target": "tests/test_same.py"}],
        lesson_targets={"tests/test_same.py"},
        founder_targets={"tests/test_same.py"},
    )
    assert report.acceptable is False
    assert any("duplicate verification target" in r for r in report.risks)
    assert any("identical to founder-authored" in r for r in report.risks)


def test_lesson_guard_for_a_new_target_is_not_a_shadow():
    steps = [{"kind": "targeted_tests", "target": "tests/test_founder_authored.py"}]
    report = overcorrection_risk(
        steps + [{"kind": "targeted_tests", "target": "tests/test_lesson_guard.py"}],
        lesson_targets={"tests/test_lesson_guard.py"},
        founder_targets={"tests/test_founder_authored.py"},
    )
    assert report.acceptable is True


def test_signature_normalization_is_exact_text_not_semantics():
    assert normalize_problem_signature("  A   B\tC ") == "a b c"
    assert normalize_problem_signature("A B C") == normalize_problem_signature("a b c")
    assert normalize_problem_signature("payment flow") != normalize_problem_signature("payment 2.0 flow")
