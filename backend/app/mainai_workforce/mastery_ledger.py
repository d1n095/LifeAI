"""Capability Mastery Ledger -- durable, auditable per-(capability, task-class, teacher)
autonomy-stage store (migration 0075). See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

A DIFFERENT axis from `app.capability_reality` (which answers "can Life invoke this at all
right now" -- a binary-ish availability/verification state per capability_key). This ledger
answers "how far along the external-dependency -> local-default autonomy ladder is THIS
task-class, relative to THIS external teacher" -- genuinely orthogonal, composed with (not
duplicating) `capability_reality` via a shared `capability_key` where useful.

STAGE CHANGE MUST HAVE A REASON, mirroring `mainai_research.research_ledger`'s CONFIDENCE
CHANGE MUST HAVE A REASON doctrine exactly: `promote()`/`demote()` both require a non-empty
`reason` and always append an event before updating the live row."""

from __future__ import annotations

import json
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.mainai_workforce.types import AutonomyStage, WorkforceError


def get_or_create_mastery_record(
    db: Session, *, owner_id: uuid.UUID, capability_key: str, task_class: str, external_teacher: str, idempotency_key: str,
) -> dict:
    existing = db.execute(
        text(
            "SELECT * FROM mainai_workforce_capability_mastery WHERE owner_id=:o AND capability_key=:c AND task_class=:t AND external_teacher=:e"
        ),
        {"o": owner_id, "c": capability_key, "t": task_class, "e": external_teacher},
    ).mappings().first()
    if existing:
        return dict(existing)
    row_id = uuid.uuid4()
    db.execute(
        text(
            "INSERT INTO mainai_workforce_capability_mastery(id, owner_id, capability_key, task_class, external_teacher, idempotency_key) "
            "VALUES (:id, :o, :c, :t, :e, :k)"
        ),
        {"id": row_id, "o": owner_id, "c": capability_key, "t": task_class, "e": external_teacher, "k": idempotency_key},
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_workforce_capability_mastery WHERE id=:id"), {"id": row_id}).mappings().one())


def record_observation(
    db: Session, *, owner_id: uuid.UUID, mastery_id: uuid.UUID, task_diversity_delta: int = 0,
    local_practice: bool = False, local_success: bool | None = None, examiner_pass: bool | None = None,
    rework: bool = False, failure_mode: str | None = None,
) -> dict:
    """AGENT OUTPUT != LEARNING. Records ONE observation -- never itself a promotion/demotion
    (see `promote()`/`demote()` for those, which are separate, explicitly-reasoned calls)."""

    row = db.execute(
        text("SELECT * FROM mainai_workforce_capability_mastery WHERE id=:id AND owner_id=:o FOR UPDATE"),
        {"id": mastery_id, "o": owner_id},
    ).mappings().first()
    if row is None:
        raise WorkforceError("mastery_id is missing or belongs to another owner")

    failure_modes = list(row["common_failure_modes"] or [])
    if failure_mode is not None and failure_mode not in failure_modes:
        failure_modes.append(failure_mode)

    db.execute(
        text(
            """UPDATE mainai_workforce_capability_mastery SET
                observation_count = observation_count + 1,
                distinct_task_diversity = distinct_task_diversity + :diversity_delta,
                local_practice_count = local_practice_count + CASE WHEN :practice THEN 1 ELSE 0 END,
                local_success_count = local_success_count + CASE WHEN :success THEN 1 ELSE 0 END,
                examiner_pass_count = examiner_pass_count + CASE WHEN :exam_pass THEN 1 ELSE 0 END,
                examiner_fail_count = examiner_fail_count + CASE WHEN :exam_fail THEN 1 ELSE 0 END,
                rework_count = rework_count + CASE WHEN :rework THEN 1 ELSE 0 END,
                common_failure_modes = CAST(:failure_modes AS jsonb),
                updated_at = clock_timestamp()
            WHERE id=:id AND owner_id=:o"""
        ),
        {
            "diversity_delta": task_diversity_delta, "practice": local_practice,
            "success": bool(local_success), "exam_pass": bool(examiner_pass),
            "exam_fail": examiner_pass is False, "rework": rework,
            "failure_modes": json.dumps(failure_modes), "id": mastery_id, "o": owner_id,
        },
    )
    db.execute(
        text(
            "INSERT INTO mainai_workforce_mastery_events(owner_id, capability_mastery_id, event_type, reason, detail) "
            "VALUES (:o, :mid, 'observation', :reason, CAST(:detail AS jsonb))"
        ),
        {
            "o": owner_id, "mid": mastery_id, "reason": "routine observation recorded",
            "detail": json.dumps({"local_practice": local_practice, "local_success": local_success, "examiner_pass": examiner_pass, "rework": rework, "failure_mode": failure_mode}),
        },
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_workforce_capability_mastery WHERE id=:id"), {"id": mastery_id}).mappings().one())


def promote(db: Session, *, owner_id: uuid.UUID, mastery_id: uuid.UUID, new_stage: AutonomyStage, reason: str) -> dict:
    if not reason.strip():
        raise WorkforceError("a stage promotion requires a non-empty reason")
    row = db.execute(
        text("SELECT * FROM mainai_workforce_capability_mastery WHERE id=:id AND owner_id=:o FOR UPDATE"),
        {"id": mastery_id, "o": owner_id},
    ).mappings().first()
    if row is None:
        raise WorkforceError("mastery_id is missing or belongs to another owner")
    if int(new_stage) <= row["stage"]:
        raise WorkforceError(f"promote() requires new_stage ({int(new_stage)}) > current stage ({row['stage']}); use demote() to lower it")

    db.execute(
        text("INSERT INTO mainai_workforce_mastery_events(owner_id, capability_mastery_id, event_type, previous_stage, new_stage, reason) VALUES (:o, :mid, 'promotion', :prev, :new, :reason)"),
        {"o": owner_id, "mid": mastery_id, "prev": row["stage"], "new": int(new_stage), "reason": reason},
    )
    db.execute(
        text("UPDATE mainai_workforce_capability_mastery SET stage=:s, updated_at=clock_timestamp() WHERE id=:id AND owner_id=:o"),
        {"s": int(new_stage), "id": mastery_id, "o": owner_id},
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_workforce_capability_mastery WHERE id=:id"), {"id": mastery_id}).mappings().one())


def demote(db: Session, *, owner_id: uuid.UUID, mastery_id: uuid.UUID, new_stage: AutonomyStage, reason: str) -> dict:
    if not reason.strip():
        raise WorkforceError("a stage demotion requires a non-empty reason")
    row = db.execute(
        text("SELECT * FROM mainai_workforce_capability_mastery WHERE id=:id AND owner_id=:o FOR UPDATE"),
        {"id": mastery_id, "o": owner_id},
    ).mappings().first()
    if row is None:
        raise WorkforceError("mastery_id is missing or belongs to another owner")
    if int(new_stage) >= row["stage"]:
        raise WorkforceError(f"demote() requires new_stage ({int(new_stage)}) < current stage ({row['stage']}); use promote() to raise it")

    db.execute(
        text("INSERT INTO mainai_workforce_mastery_events(owner_id, capability_mastery_id, event_type, previous_stage, new_stage, reason) VALUES (:o, :mid, 'demotion', :prev, :new, :reason)"),
        {"o": owner_id, "mid": mastery_id, "prev": row["stage"], "new": int(new_stage), "reason": reason},
    )
    db.execute(
        text("UPDATE mainai_workforce_capability_mastery SET stage=:s, updated_at=clock_timestamp() WHERE id=:id AND owner_id=:o"),
        {"s": int(new_stage), "id": mastery_id, "o": owner_id},
    )
    db.flush()
    return dict(db.execute(text("SELECT * FROM mainai_workforce_capability_mastery WHERE id=:id"), {"id": mastery_id}).mappings().one())


def list_mastery_events(db: Session, *, owner_id: uuid.UUID, mastery_id: uuid.UUID) -> list[dict]:
    rows = db.execute(
        text("SELECT * FROM mainai_workforce_mastery_events WHERE owner_id=:o AND capability_mastery_id=:m ORDER BY recorded_at"),
        {"o": owner_id, "m": mastery_id},
    ).mappings().all()
    return [dict(r) for r in rows]


def get_mastery_record(db: Session, *, owner_id: uuid.UUID, mastery_id: uuid.UUID) -> dict | None:
    row = db.execute(
        text("SELECT * FROM mainai_workforce_capability_mastery WHERE id=:id AND owner_id=:o"), {"id": mastery_id, "o": owner_id},
    ).mappings().first()
    return dict(row) if row else None
