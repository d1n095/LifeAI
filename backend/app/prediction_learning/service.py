"""Stage L — prediction vs outcome learning (feeds self-model)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.prediction_learning import PredictionRecord
from app.self_model import record_failed_capability, record_proven_capability


class PredictionKind(str, Enum):
    EFFORT = "effort"
    RISK = "risk"
    EXPECTED_RESULT = "expected_result"
    LIKELY_BLOCKER = "likely_blocker"
    PLAN_SUCCESS = "plan_success"


@dataclass
class LearningSignal:
    pattern: str
    count: int
    examples: list[str] = field(default_factory=list)


@dataclass
class PredictionLearningReport:
    overconfidence: list[LearningSignal] = field(default_factory=list)
    underestimation: list[LearningSignal] = field(default_factory=list)
    bad_heuristics: list[LearningSignal] = field(default_factory=list)
    good_patterns: list[LearningSignal] = field(default_factory=list)


class PredictionLearningError(ValueError):
    pass


def record_prediction(
    db: Session,
    *,
    owner_id: uuid.UUID,
    kind: PredictionKind | str,
    subject_ref: str,
    predicted_value: dict,
    idempotency_key: str,
    confidence: float = 0.5,
    heuristic_tags: list[str] | None = None,
) -> PredictionRecord:
    kind_v = kind.value if isinstance(kind, PredictionKind) else str(kind)
    if kind_v not in {k.value for k in PredictionKind}:
        raise PredictionLearningError(f"unsupported kind: {kind_v}")
    existing = db.execute(
        select(PredictionRecord).where(
            PredictionRecord.owner_id == owner_id,
            PredictionRecord.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing:
        return existing
    row = PredictionRecord(
        owner_id=owner_id,
        kind=kind_v,
        subject_ref=subject_ref,
        predicted_value=dict(predicted_value),
        confidence=confidence,
        heuristic_tags=list(heuristic_tags or []),
        idempotency_key=idempotency_key,
    )
    db.add(row)
    db.flush()
    return row


def score_prediction(
    db: Session,
    *,
    owner_id: uuid.UUID,
    prediction_id: uuid.UUID,
    actual_value: dict,
) -> PredictionRecord:
    row = db.execute(
        select(PredictionRecord)
        .where(PredictionRecord.id == prediction_id, PredictionRecord.owner_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise PredictionLearningError("prediction missing")
    pred = row.predicted_value or {}
    delta: dict = {"kind": row.kind}
    for key in ("hours", "risk_score", "success_probability"):
        if key in pred and key in actual_value:
            try:
                p = float(pred[key])
                a = float(actual_value[key])
                delta[key] = {"predicted": p, "actual": a, "error": a - p}
            except (TypeError, ValueError):
                continue
    if "blocker" in pred or "blocker" in actual_value:
        delta["blocker_match"] = pred.get("blocker") == actual_value.get("blocker")
    if "success" in pred or "success" in actual_value:
        delta["success_match"] = bool(pred.get("success")) == bool(actual_value.get("success"))

    row.actual_value = dict(actual_value)
    row.outcome_delta = delta
    row.status = "scored"
    row.scored_at = datetime.utcnow()
    db.flush()

    cap_key = f"prediction.{row.kind}"
    accurate = False
    if "success_match" in delta:
        accurate = bool(delta["success_match"])
    elif "hours" in delta and abs(delta["hours"]["error"]) <= max(1.0, 0.25 * float(pred.get("hours", 1))):
        accurate = True
    elif "blocker_match" in delta:
        accurate = bool(delta["blocker_match"])

    if accurate:
        from app.intelligence_governance.service import record_evidence, record_execution
        from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask

        # ONE canonical prove path: durable evidence FK required (same rule as Stage E / #213).
        goal = MainAIGoal(
            owner_id=owner_id,
            title=f"prediction-score:{row.id}",
            original_instruction="score_prediction",
            created_by="prediction_learning",
            completed_at=None,
        )
        db.add(goal)
        db.flush()
        plan = MainAIPlan(
            owner_id=owner_id, goal_id=goal.id, version=1, rationale="prediction_score", created_by="prediction_learning"
        )
        db.add(plan)
        db.flush()
        task = MainAITask(
            owner_id=owner_id,
            goal_id=goal.id,
            plan_id=plan.id,
            task_type="repo_edit",
            description=f"score prediction {row.id}",
            status="pending",
            risk_level="low",
        )
        db.add(task)
        db.flush()
        execution = record_execution(
            db,
            owner_id=owner_id,
            task_id=task.id,
            idempotency_key=f"pred-exec:{row.id}",
            provider="internal",
        )
        evidence = record_evidence(
            db,
            owner_id=owner_id,
            execution_id=execution.id,
            evidence_kind="prediction_outcome",
            payload={
                "prediction_id": str(row.id),
                "kind": row.kind,
                "predicted": pred,
                "actual": actual_value,
                "delta": delta,
                "accurate": True,
            },
            source_type="prediction_learning",
            source_ref=f"prediction_records:{row.id}",
            idempotency_key=f"pred-ev:{row.id}",
            deterministic=True,
        )
        record_proven_capability(
            db,
            owner_id=owner_id,
            capability_key=cap_key,
            domain="prediction",
            verification_evidence_id=evidence.id,
            status_reason=f"scored_prediction:{row.id}",
        )
    else:
        record_failed_capability(
            db,
            owner_id=owner_id,
            capability_key=cap_key,
            domain="prediction",
            reason=f"missed_prediction:{row.id}",
            demote_from_verified=True,
        )
    return row


def analyze_prediction_learning(db: Session, *, owner_id: uuid.UUID) -> PredictionLearningReport:
    rows = list(
        db.execute(
            select(PredictionRecord).where(
                PredictionRecord.owner_id == owner_id,
                PredictionRecord.status == "scored",
            )
        ).scalars().all()
    )
    over: dict[str, list[str]] = {}
    under: dict[str, list[str]] = {}
    bad: dict[str, list[str]] = {}
    good: dict[str, list[str]] = {}

    for row in rows:
        delta = row.outcome_delta or {}
        tags = row.heuristic_tags or ["untagged"]
        if row.kind == PredictionKind.EFFORT.value and "hours" in delta:
            err = delta["hours"]["error"]
            bucket = under if err > 0 else over if err < 0 else good
            for tag in tags:
                bucket.setdefault(tag, []).append(str(row.id))
        if row.kind == PredictionKind.PLAN_SUCCESS.value and "success_match" in delta:
            bucket = good if delta["success_match"] else bad
            for tag in tags:
                bucket.setdefault(tag, []).append(str(row.id))
        if row.confidence >= 0.85 and delta.get("success_match") is False:
            over.setdefault("high_confidence_miss", []).append(str(row.id))

    def _signals(m: dict[str, list[str]]) -> list[LearningSignal]:
        return [LearningSignal(pattern=k, count=len(v), examples=v[:5]) for k, v in m.items() if v]

    return PredictionLearningReport(
        overconfidence=_signals(over),
        underestimation=_signals(under),
        bad_heuristics=_signals(bad),
        good_patterns=_signals(good),
    )
