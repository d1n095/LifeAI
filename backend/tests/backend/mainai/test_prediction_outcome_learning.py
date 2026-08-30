"""Stage L — prediction vs outcome learning tests."""

from __future__ import annotations

import uuid

from app.models.user import User
from app.prediction_learning import (
    PredictionKind,
    analyze_prediction_learning,
    record_prediction,
    score_prediction,
)
from app.self_model import build_self_model


def _owner(db):
    user = User(email=f"pred-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def test_score_effort_miss_and_success_hit(superuser_db):
    owner = _owner(superuser_db)
    under = record_prediction(
        superuser_db,
        owner_id=owner.id,
        kind=PredictionKind.EFFORT,
        subject_ref="goal:demo",
        predicted_value={"hours": 2},
        confidence=0.7,
        heuristic_tags=["optimistic_effort"],
        idempotency_key="l-effort",
    )
    score_prediction(
        superuser_db,
        owner_id=owner.id,
        prediction_id=under.id,
        actual_value={"hours": 8},
    )
    hit = record_prediction(
        superuser_db,
        owner_id=owner.id,
        kind=PredictionKind.PLAN_SUCCESS,
        subject_ref="plan:demo",
        predicted_value={"success": True},
        confidence=0.9,
        heuristic_tags=["stable_pattern"],
        idempotency_key="l-ok",
    )
    score_prediction(
        superuser_db,
        owner_id=owner.id,
        prediction_id=hit.id,
        actual_value={"success": True},
    )
    miss = record_prediction(
        superuser_db,
        owner_id=owner.id,
        kind=PredictionKind.PLAN_SUCCESS,
        subject_ref="plan:demo2",
        predicted_value={"success": True},
        confidence=0.95,
        heuristic_tags=["overconfident"],
        idempotency_key="l-miss",
    )
    score_prediction(
        superuser_db,
        owner_id=owner.id,
        prediction_id=miss.id,
        actual_value={"success": False},
    )
    superuser_db.commit()

    report = analyze_prediction_learning(superuser_db, owner_id=owner.id)
    assert any(s.pattern == "optimistic_effort" for s in report.underestimation)
    assert any(s.pattern == "stable_pattern" for s in report.good_patterns)
    assert any(s.pattern == "high_confidence_miss" for s in report.overconfidence)

    snap = build_self_model(superuser_db, owner_id=owner.id, domain="prediction")
    assert snap.entries
