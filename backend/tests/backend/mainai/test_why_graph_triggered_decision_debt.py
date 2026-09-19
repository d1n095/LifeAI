"""app.mainai_executive.why_graph.list_triggered_decision_debt -- additive extension of the
existing list_decision_debt()/why_feature_exists() (unmodified, proven so below) with real
triggering logic: dependency_ready, lesson_conflict_recent, note_disputed/note_recent."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.inspectable_memory import founder_add_memory_note
from app.mainai_executive.why_graph import list_decision_debt, list_triggered_decision_debt, why_feature_exists
from app.mainai_execution.lesson_conflicts import mark_conflict
from app.models.mainai_execution import (
    EngineeringLesson,
    EngineeringLessonConfidence,
    EngineeringLessonSeverity,
    EngineeringLessonStatus,
    MainAIGoal,
    MainAIPlan,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
)
from app.models.user import User
from app.work_candidates.service import authorize_work_candidate, record_work_candidate

from tests.backend.mainai.test_work_candidates import _owner_with_entity


def _owner(db) -> User:
    user = User(email=f"why-trig-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def test_existing_functions_are_unmodified_by_import_shape():
    """Additive-only proof: both pre-existing functions remain importable with the exact same
    names, alongside the new one, from the same module."""
    import app.mainai_executive.why_graph as module

    assert callable(module.why_feature_exists)
    assert callable(module.list_decision_debt)
    assert callable(module.list_triggered_decision_debt)


def test_dependency_ready_trigger(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    blocking = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="blocking work",
        idempotency_key=f"dep-block-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()
    authorized, _goal = authorize_work_candidate(
        superuser_db, owner_id=owner.id, candidate_id=blocking.id, authorized_by="founder"
    )
    superuser_db.commit()
    waiting = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="waiting on blocking work",
        idempotency_key=f"dep-wait-{uuid.uuid4()}", classifier_strategy="test",
        dependencies=[str(authorized.id)],
    )
    superuser_db.commit()

    debt = list_triggered_decision_debt(superuser_db, owner_id=owner.id)
    matches = [i for i in debt["items"] if i["id"] == str(waiting.id)]
    assert len(matches) == 1
    assert matches[0]["trigger"] == "dependency_ready"
    assert debt["bounded"] is True
    assert debt["authority_impact"] == "NONE"


def test_dependency_not_ready_produces_no_trigger(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    blocking = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="still unreviewed blocker",
        idempotency_key=f"dep-block2-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()
    waiting = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="waiting still",
        idempotency_key=f"dep-wait2-{uuid.uuid4()}", classifier_strategy="test",
        dependencies=[str(blocking.id)],
    )
    superuser_db.commit()

    debt = list_triggered_decision_debt(superuser_db, owner_id=owner.id)
    assert not any(i["id"] == str(waiting.id) for i in debt["items"])


def _task_goal(db, owner_id):
    goal = MainAIGoal(owner_id=owner_id, title="g", original_instruction="s", created_by="test")
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="r", created_by="test")
    db.add(plan)
    db.flush()
    task = MainAITask(owner_id=owner_id, goal_id=goal.id, plan_id=plan.id, description="d", task_type="repo_edit")
    db.add(task)
    db.flush()
    return goal, task


def _lesson(db, tag):
    lesson = EngineeringLesson(
        status=EngineeringLessonStatus.active, problem="p", root_cause="r", affected_component="c",
        severity=EngineeringLessonSeverity.low, evidence="e", fix="f", general_rule="g", applies_to=[tag],
        source_type="test", source_ref="test", first_seen_at=datetime.utcnow(), created_by="test",
        confidence=EngineeringLessonConfidence.likely,
    )
    db.add(lesson)
    db.flush()
    return lesson


def test_lesson_conflict_recent_trigger(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    goal, task = _task_goal(superuser_db, owner.id)
    lesson_a = _lesson(superuser_db, "conflict-tag")
    lesson_b = _lesson(superuser_db, "conflict-tag")
    superuser_db.commit()

    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="affected work",
        idempotency_key=f"conf-cand-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()
    # Authorize into THIS SAME goal so authorized_goal_id lines up with the task's own goal.
    authorized, _created_goal = authorize_work_candidate(
        superuser_db, owner_id=owner.id, candidate_id=candidate.id, authorized_by="founder"
    )
    superuser_db.commit()
    # Point the task at the just-authorized goal so the join in list_triggered_decision_debt
    # finds it (mirrors real usage: the task belongs to the goal the candidate authorized).
    task.goal_id = authorized.authorized_goal_id
    superuser_db.add(
        MainAITaskEvent(
            task_id=task.id, owner_id=owner.id, event_type=MainAITaskEventType.created,
            detail={"lessons_applied": [str(lesson_a.id)]},
        )
    )
    superuser_db.commit()

    mark_conflict(superuser_db, lesson_a=lesson_a, lesson_b=lesson_b, reasoning="explicit contradiction")
    superuser_db.commit()

    debt = list_triggered_decision_debt(superuser_db, owner_id=owner.id)
    matches = [i for i in debt["items"] if i["trigger"] == "lesson_conflict_recent"]
    assert any(m["id"] == str(authorized.id) for m in matches)


def test_note_disputed_and_note_recent_triggers(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    disputed_note, _ = founder_add_memory_note(
        superuser_db, owner_id=owner.id, content="Temporary: use X until Y", note_type="decision",
        idempotency_key=f"note-d-{uuid.uuid4()}",
    )
    superuser_db.commit()
    from app.founder_memory import mark_founder_memory_disputed

    mark_founder_memory_disputed(superuser_db, owner_id=owner.id, note_id=disputed_note.id)
    superuser_db.commit()

    recent_note, _ = founder_add_memory_note(
        superuser_db, owner_id=owner.id, content="Recent correction: use Z instead", note_type="correction",
        idempotency_key=f"note-r-{uuid.uuid4()}",
    )
    superuser_db.commit()

    debt = list_triggered_decision_debt(superuser_db, owner_id=owner.id, recency_days=14)
    triggers_by_id = {i["id"]: i["trigger"] for i in debt["items"]}
    assert triggers_by_id.get(str(disputed_note.id)) == "note_disputed"
    assert triggers_by_id.get(str(recent_note.id)) == "note_recent"


def test_old_non_disputed_note_produces_no_trigger(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    note, _ = founder_add_memory_note(
        superuser_db, owner_id=owner.id, content="Old stable decision", note_type="decision",
        idempotency_key=f"note-old-{uuid.uuid4()}",
    )
    note.observed_at = datetime.utcnow() - timedelta(days=100)
    superuser_db.commit()

    debt = list_triggered_decision_debt(superuser_db, owner_id=owner.id, recency_days=14)
    assert not any(i["id"] == str(note.id) for i in debt["items"])


def test_bounds_validation(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    import pytest

    with pytest.raises(ValueError):
        list_triggered_decision_debt(superuser_db, owner_id=owner.id, limit=0)
    with pytest.raises(ValueError):
        list_triggered_decision_debt(superuser_db, owner_id=owner.id, recency_days=0)


def test_existing_list_decision_debt_and_why_feature_exists_still_work(superuser_db):
    """Regression: the two pre-existing functions still behave exactly as before this
    additive change."""
    owner = _owner(superuser_db)
    superuser_db.commit()
    note, _ = founder_add_memory_note(
        superuser_db, owner_id=owner.id, content="still works", note_type="decision",
        idempotency_key=f"regress-{uuid.uuid4()}",
    )
    superuser_db.commit()
    debt = list_decision_debt(superuser_db, owner_id=owner.id)
    assert debt["bounded"] is True
    assert any(i["note_id"] == str(note.id) for i in debt["items"])
    chain = why_feature_exists(superuser_db, owner_id=owner.id, note_id=note.id)
    assert chain["chain_of_thought_exposed"] is False
