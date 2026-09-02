"""Stage R — self-improvement ROI / complexity budget."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.self_improvement_roi import SelfImprovementROIRecord


@dataclass
class ROIDecision:
    net_roi: float
    recommendation: str
    rationale: str
    complexity_pays_for_itself: bool


def evaluate_self_improvement_roi(
    *,
    metrics_before: dict,
    metrics_after: dict,
    complexity_cost: float,
) -> ROIDecision:
    keys = (
        "failure_rate",
        "founder_workload",
        "provider_token_cost",
        "retrieval_quality",
        "latency_ms",
        "autonomous_completion",
        "maintenance_complexity",
    )
    score = 0.0
    for key in keys:
        b = float(metrics_before.get(key, 0) or 0)
        a = float(metrics_after.get(key, 0) or 0)
        if key in {"retrieval_quality", "autonomous_completion"}:
            score += a - b
        else:
            score += b - a
    net = score - float(complexity_cost)
    if net > 0.5:
        rec, rationale = "keep", "Measured benefit exceeds complexity cost."
    elif net < -0.5:
        rec, rationale = ("resist_add" if complexity_cost > 0 else "revert"), "Complexity does not pay for itself."
    else:
        rec, rationale = "observe", "ROI inconclusive — keep observing."
    return ROIDecision(net_roi=net, recommendation=rec, rationale=rationale, complexity_pays_for_itself=net > 0)


def record_roi(
    db: Session,
    *,
    owner_id: uuid.UUID,
    change_ref: str,
    metrics_before: dict,
    metrics_after: dict,
    complexity_cost: float,
    idempotency_key: str,
) -> SelfImprovementROIRecord:
    existing = db.execute(
        select(SelfImprovementROIRecord).where(
            SelfImprovementROIRecord.owner_id == owner_id,
            SelfImprovementROIRecord.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    decision = evaluate_self_improvement_roi(
        metrics_before=metrics_before, metrics_after=metrics_after, complexity_cost=complexity_cost
    )
    row = SelfImprovementROIRecord(
        owner_id=owner_id,
        change_ref=change_ref,
        metrics_before=dict(metrics_before),
        metrics_after=dict(metrics_after),
        complexity_cost=complexity_cost,
        net_roi=decision.net_roi,
        recommendation=decision.recommendation,
        rationale=decision.rationale,
        idempotency_key=idempotency_key,
    )
    savepoint = db.begin_nested()
    try:
        db.add(row)
        db.flush()
        savepoint.commit()
        return row
    except IntegrityError as exc:
        constraint_name = getattr(getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", None)
        savepoint.rollback()
        if constraint_name != "uq_self_improvement_roi_idem":
            raise
        existing = db.execute(
            select(SelfImprovementROIRecord).where(
                SelfImprovementROIRecord.owner_id == owner_id,
                SelfImprovementROIRecord.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()
        if existing is None:
            raise
        return existing
