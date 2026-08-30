"""Stage D — evidence-backed temporal recap (no model-context invention)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

from app.inspectable_memory import founder_add_memory_note
from app.mainai_execution import planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.request_context import current_user_id as current_user_id_var
from app.temporal_intelligence import (
    RecapWindow,
    TemporalIntelligenceError,
    answer_founder_recap_question,
    build_recap,
    resolve_window,
)


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def test_resolve_window_presets():
    now = datetime(2026, 8, 30, 12, 0, 0)
    day = resolve_window(RecapWindow.DAY, now=now)
    assert day.start == now - timedelta(days=1)
    assert day.end == now
    entire = resolve_window(RecapWindow.ENTIRE_PROJECT, now=now)
    assert entire.start is None and entire.end is None
    with pytest.raises(Exception):
        resolve_window(RecapWindow.CUSTOM, now=now)


def test_day_recap_includes_founder_memory_and_task(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    note, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Ship temporal recap for today",
        note_type="decision",
        idempotency_key=f"temp-{uuid.uuid4()}",
    )
    goal = planner.create_goal(
        db_session,
        owner_id=user.id,
        title="Temporal day goal",
        original_instruction="Prove durable day recap",
        created_by="test",
    )
    plan = planner.create_plan(
        db_session,
        goal=goal,
        rationale="base",
        tasks=[PlannedTaskSpec(description="Build temporal day recap evidence", task_type="read_only_audit")],
        created_by="test",
    )
    db_session.commit()

    report = build_recap(db_session, owner_id=user.id, window=RecapWindow.DAY)
    kinds = {i.kind for i in report.items}
    assert "founder_memory_note" in kinds
    assert "mainai_goal" in kinds or "mainai_task" in kinds
    assert any(i.id == note.id for i in report.items if i.kind == "founder_memory_note")
    assert report.evidence_only is True
    assert report.counts_by_kind


def test_old_memory_excluded_from_hour_window(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    note, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Ancient preference",
        note_type="preference",
        idempotency_key=f"old-{uuid.uuid4()}",
    )
    # Force observed_at into the past beyond hour window.
    note.observed_at = datetime.utcnow() - timedelta(days=3)
    note.created_at = note.observed_at
    db_session.commit()

    report = build_recap(db_session, owner_id=user.id, window=RecapWindow.HOUR)
    assert all(i.id != note.id for i in report.items if i.kind == "founder_memory_note")

    entire = build_recap(db_session, owner_id=user.id, window=RecapWindow.ENTIRE_PROJECT)
    assert any(i.id == note.id for i in entire.items if i.kind == "founder_memory_note")


def test_swedish_question_maps_to_week(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="Weekly recap signal",
        note_type="observation",
        idempotency_key=f"week-{uuid.uuid4()}",
    )
    db_session.commit()
    report = answer_founder_recap_question(
        db_session,
        owner_id=user.id,
        question="vad har hänt senaste veckan?",
    )
    assert report.range.window == RecapWindow.WEEK
    assert any(i.kind == "founder_memory_note" for i in report.items)


def test_repeated_attempt_detection(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    for i in range(3):
        founder_add_memory_note(
            db_session,
            owner_id=user.id,
            content="Retry lease takeover fencing",
            note_type="observation",
            idempotency_key=f"rep-{uuid.uuid4()}",
        )
    db_session.commit()
    report = build_recap(db_session, owner_id=user.id, window=RecapWindow.WEEK)
    assert any(r["count"] >= 3 for r in report.repeated_titles)


def test_custom_window_validation():
    with pytest.raises(TemporalIntelligenceError):
        build_recap(
            None,  # type: ignore[arg-type]
            owner_id=uuid.uuid4(),
            window=RecapWindow.CUSTOM,
        )
