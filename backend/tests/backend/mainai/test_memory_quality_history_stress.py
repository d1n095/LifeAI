"""Stage H — synthetic history stress proving CURRENT/SUPERSEDED/DUPLICATE/… answers."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.memory_quality import answer_history_quality_queries, seed_synthetic_history
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def test_history_stress_answers(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    seed = seed_synthetic_history(db_session, owner_id=user.id)
    db_session.commit()
    assert seed.note_ids["superseded_mongo"]
    answers = answer_history_quality_queries(db_session, owner_id=user.id)

    assert any("Postgres for sessions" in c for c in answers.current)
    assert any("MongoDB" in s for s in answers.superseded)
    assert answers.duplicate  # alias or collapsed duplicate evidence
    assert any("Maybe switch providers" in u for u in answers.unresolved)
    assert answers.affected_work
    assert answers.recent_changes
    assert answers.historical_evolution
