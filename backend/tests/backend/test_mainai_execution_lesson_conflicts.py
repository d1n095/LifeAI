"""MainAI V0.3 -- minimal engineering-lesson conflict detection
(app/mainai_execution/lesson_conflicts.py, app/worker.py's
`_resolve_engineering_lesson_conflicts`). Closes the gap EngineeringLessonConfidence's own
docstring names explicitly: "nothing yet computes lesson-vs-lesson contradiction". Before this,
lessons.py's apply_lessons_to_verification_plan() would silently apply every `active` lesson
matching a task_type's tags, including two that actively contradict each other.

Covers:
  - find_conflict_candidate_pairs(): pure, deterministic narrowing -- only `active` lessons
    that share BOTH affected_component and at least one applies_to tag are ever paired; an
    inactive lesson, a different component, or disjoint tags are never paired.
  - detect_conflict(): fail-closed on a malformed/errored AI response (never a proven conflict
    on an unreliable signal).
  - mark_conflict()/resolve_conflicts_among(): a real positive-conflict AI response moves BOTH
    lessons to `disputed`, never picks a winner, and both are excluded from the returned
    still-active list.
  - mark_conflict() also records a `lesson_conflict_detected` MainAITaskEvent (migration 0036)
    on every task whose verification plan actually used the disputed lesson, found via the
    task's own `created` event's `lessons_applied` list (lessons.py) -- a real end-to-end link
    from "founder-wide lesson disputed" back to "these specific tasks relied on it".
  - REQUIRED demo (real production path): two genuinely conflicting lessons, recorded via the
    real record_lesson(), get disputed by the worker's own tick -- no manual call -- and are
    then correctly excluded from a real subsequent apply_lessons_to_verification_plan() call,
    proving the downstream synchronous consumer needed zero changes."""

from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.mainai_execution import lessons, planner
from app.mainai_execution.lesson_conflicts import (
    detect_conflict,
    find_conflict_candidate_pairs,
    mark_conflict,
    resolve_conflicts_among,
)
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import (
    EngineeringLesson,
    EngineeringLessonSeverity,
    EngineeringLessonStatus,
    MainAITask,
    MainAITaskEventType,
)
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var
from app.worker import Worker


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _lesson(db_session, *, component="app.mainai_execution.executor", tags=None, rule="do X", regression_test=None):
    return lessons.record_lesson(
        db_session,
        problem="a problem",
        root_cause="a root cause",
        affected_component=component,
        severity=EngineeringLessonSeverity.medium,
        evidence="evidence",
        fix=rule,
        general_rule=rule,
        applies_to=tags or ["run_tests"],
        source_type="branch_registry_pass",
        source_ref="test",
        created_by="test",
        first_seen_at=datetime.utcnow(),
        regression_test=regression_test,
    )


def _goal(db_session, owner_id):
    return planner.create_goal(db_session, owner_id=owner_id, title="Lesson conflict demo goal", original_instruction="Fix the thing.", created_by="test")


def _events(db_session, task_id) -> list[dict]:
    rows = db_session.execute(
        sa_text("SELECT event_type, detail FROM mainai_task_events WHERE task_id = :id ORDER BY created_at")
    , {"id": str(task_id)}).all()
    return [{"event_type": row[0], "detail": row[1]} for row in rows]


# ---------------------------------------------------------------- A. candidate pairing


def test_find_conflict_candidate_pairs_pairs_matching_component_and_tag(db_session, owner_id):
    a = _lesson(db_session, component="app.foo", tags=["run_tests", "planner"])
    b = _lesson(db_session, component="app.foo", tags=["run_tests"])
    db_session.commit()

    pairs = find_conflict_candidate_pairs(db_session, lessons=[a, b])
    assert pairs == [(a, b)]


def test_find_conflict_candidate_pairs_excludes_different_components(db_session, owner_id):
    a = _lesson(db_session, component="app.foo", tags=["run_tests"])
    b = _lesson(db_session, component="app.bar", tags=["run_tests"])
    db_session.commit()

    assert find_conflict_candidate_pairs(db_session, lessons=[a, b]) == []


def test_find_conflict_candidate_pairs_excludes_disjoint_tags(db_session, owner_id):
    a = _lesson(db_session, component="app.foo", tags=["run_tests"])
    b = _lesson(db_session, component="app.foo", tags=["repo_edit"])
    db_session.commit()

    assert find_conflict_candidate_pairs(db_session, lessons=[a, b]) == []


def test_find_conflict_candidate_pairs_excludes_a_non_active_lesson(db_session, owner_id):
    a = _lesson(db_session, component="app.foo", tags=["run_tests"])
    b = _lesson(db_session, component="app.foo", tags=["run_tests"])
    b.status = EngineeringLessonStatus.disputed
    db_session.commit()

    assert find_conflict_candidate_pairs(db_session, lessons=[a, b]) == []


# ---------------------------------------------------------------- B. detect_conflict fail-closed


@pytest.mark.asyncio
async def test_detect_conflict_fails_closed_on_malformed_ai_response(db_session, owner_id, monkeypatch):
    a = _lesson(db_session, component="app.foo", tags=["run_tests"])
    b = _lesson(db_session, component="app.foo", tags=["run_tests"])
    db_session.commit()

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="not json at all", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)

    is_conflict, reasoning = await detect_conflict(db_session, lesson_a=a, lesson_b=b)
    assert is_conflict is False
    assert reasoning == ""


@pytest.mark.asyncio
async def test_detect_conflict_fails_closed_when_the_ai_call_raises(db_session, owner_id, monkeypatch):
    a = _lesson(db_session, component="app.foo", tags=["run_tests"])
    b = _lesson(db_session, component="app.foo", tags=["run_tests"])
    db_session.commit()

    async def _raising_chat(self, messages, model, **kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(OpenAIProvider, "chat", _raising_chat)

    is_conflict, reasoning = await detect_conflict(db_session, lesson_a=a, lesson_b=b)
    assert is_conflict is False


# ---------------------------------------------------------------- C. mark_conflict / resolve_conflicts_among


def test_mark_conflict_moves_both_lessons_to_disputed_and_picks_no_winner(db_session, owner_id):
    a = _lesson(db_session, component="app.foo", tags=["run_tests"])
    b = _lesson(db_session, component="app.foo", tags=["run_tests"])
    db_session.commit()

    mark_conflict(db_session, lesson_a=a, lesson_b=b, reasoning="they disagree")
    db_session.commit()

    a = db_session.get(EngineeringLesson, a.id)
    b = db_session.get(EngineeringLesson, b.id)
    assert a.status == EngineeringLessonStatus.disputed
    assert b.status == EngineeringLessonStatus.disputed


def test_mark_conflict_records_lesson_conflict_detected_on_tasks_that_actually_applied_the_lesson(db_session, owner_id):
    """A real create_plan() call applies `a` to a real task (matching regression_test target,
    matching task_type tag) -- proving the `lessons_applied` link mark_conflict() relies on is
    the same one lessons.py/planner.py already write, not a new one invented for this test. `b`
    is tagged for a DIFFERENT task_type ("repo_edit") so lookup_lessons() never applies it to
    this "run_tests" task -- isolating the assertion to exactly the task that actually used `a`."""
    a = _lesson(db_session, component="app.foo", tags=["run_tests"], regression_test="tests/a.py::test_a")
    b = _lesson(db_session, component="app.foo", tags=["repo_edit"], regression_test="tests/b.py::test_b")
    db_session.commit()

    goal = _goal(db_session, owner_id)
    planner.create_plan(
        db_session, goal=goal, rationale="uses lesson a", tasks=[PlannedTaskSpec(description="run tests", task_type="run_tests")], created_by="test"
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()

    created_detail = next(e["detail"] for e in _events(db_session, task.id) if e["event_type"] == "created")
    assert str(a.id) in created_detail["lessons_applied"]

    mark_conflict(db_session, lesson_a=a, lesson_b=b, reasoning="they disagree")
    db_session.commit()

    events = _events(db_session, task.id)
    conflict_events = [e for e in events if e["event_type"] == MainAITaskEventType.lesson_conflict_detected.value]
    assert len(conflict_events) == 1
    assert conflict_events[0]["detail"]["lesson_id"] == str(a.id)
    assert conflict_events[0]["detail"]["conflicting_lesson_id"] == str(b.id)


@pytest.mark.asyncio
async def test_resolve_conflicts_among_disputes_a_real_positive_conflict_and_excludes_it_from_the_result(db_session, owner_id, monkeypatch):
    a = _lesson(db_session, component="app.foo", tags=["run_tests"], rule="always do X")
    b = _lesson(db_session, component="app.foo", tags=["run_tests"], rule="never do X")
    unrelated = _lesson(db_session, component="app.bar", tags=["repo_edit"], rule="unrelated")
    db_session.commit()

    async def _fake_chat(self, messages, model, **kwargs):
        import json

        return ChatResult(content=json.dumps({"conflict": True, "reasoning": "X vs never X"}), provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)

    still_active = await resolve_conflicts_among(db_session, lessons=[a, b, unrelated])
    db_session.commit()

    assert {lesson.id for lesson in still_active} == {unrelated.id}
    a = db_session.get(EngineeringLesson, a.id)
    b = db_session.get(EngineeringLesson, b.id)
    assert a.status == EngineeringLessonStatus.disputed
    assert b.status == EngineeringLessonStatus.disputed


@pytest.mark.asyncio
async def test_resolve_conflicts_among_leaves_non_conflicting_lessons_active(db_session, owner_id, monkeypatch):
    a = _lesson(db_session, component="app.foo", tags=["run_tests"])
    b = _lesson(db_session, component="app.foo", tags=["run_tests"])
    db_session.commit()

    async def _fake_chat(self, messages, model, **kwargs):
        import json

        return ChatResult(content=json.dumps({"conflict": False, "reasoning": "complementary"}), provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)

    still_active = await resolve_conflicts_among(db_session, lessons=[a, b])
    db_session.commit()

    assert {lesson.id for lesson in still_active} == {a.id, b.id}


# ---------------------------------------------------------------- D. REQUIRED demo: real worker tick -> real planner consumer


@pytest.mark.asyncio
async def test_demo_worker_tick_disputes_conflicting_lessons_and_planner_no_longer_applies_them(db_session, owner_id, monkeypatch):
    """REQUIRED demo: two genuinely conflicting lessons, recorded via the real record_lesson(),
    get disputed by the worker's OWN tick (no manual resolve_conflicts_among() call) -- and a
    real subsequent apply_lessons_to_verification_plan() call (the exact function planner.py's
    create_plan() calls synchronously) no longer applies either of them. lessons.py/planner.py
    themselves are untouched by V0.3; this demo proves that's safe."""
    a = _lesson(db_session, component="app.mainai_execution.verify", tags=["run_tests"], rule="always retry on flake", regression_test="tests/a.py::test_a")
    b = _lesson(db_session, component="app.mainai_execution.verify", tags=["run_tests"], rule="never retry on flake", regression_test="tests/b.py::test_b")
    db_session.commit()

    async def _fake_chat(self, messages, model, **kwargs):
        import json

        return ChatResult(content=json.dumps({"conflict": True, "reasoning": "always vs never retry"}), provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)

    worker = Worker()
    await worker._resolve_engineering_lesson_conflicts(db_session)

    db_session.expire_all()
    a = db_session.get(EngineeringLesson, a.id)
    b = db_session.get(EngineeringLesson, b.id)
    assert a.status == EngineeringLessonStatus.disputed
    assert b.status == EngineeringLessonStatus.disputed

    augmented, applied_ids = lessons.apply_lessons_to_verification_plan(db_session, task_type="run_tests", verification_plan=[])
    assert augmented == []
    assert applied_ids == []
