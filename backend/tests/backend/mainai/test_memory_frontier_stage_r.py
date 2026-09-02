"""Stage R — self-improvement ROI / complexity budget."""

from __future__ import annotations

import uuid

from app.models.user import User
from app.self_improvement_roi import evaluate_self_improvement_roi, record_roi


def test_stage_r_roi_resists_unpaid_complexity(superuser_db):
    owner = User(email=f"r-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    decision = evaluate_self_improvement_roi(
        metrics_before={"failure_rate": 0.1, "founder_workload": 5, "maintenance_complexity": 1},
        metrics_after={"failure_rate": 0.09, "founder_workload": 4.8, "maintenance_complexity": 5},
        complexity_cost=3.0,
    )
    assert decision.complexity_pays_for_itself is False
    assert decision.recommendation in {"resist_add", "revert", "observe"}
    row = record_roi(
        superuser_db,
        owner_id=owner.id,
        change_ref="add-extra-architecture",
        metrics_before={"failure_rate": 0.2, "retrieval_quality": 0.5},
        metrics_after={"failure_rate": 0.05, "retrieval_quality": 0.9},
        complexity_cost=0.2,
        idempotency_key="r1",
    )
    superuser_db.commit()
    assert row.recommendation in {"keep", "observe"}
