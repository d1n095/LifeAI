"""Durable executive-session continuity across process kill / machine restart.

Uses founder_memory notes as the durable store — no new table.
PROCESS MEMORY != AUTHORITY. ORM SESSION MEMORY != AUTHORITY.
Recovery reads only durable checkpoints; never invents continuation.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.founder_memory import record_founder_memory
from app.mainai_executive.types import ContinuityCheckpoint, ExecutivePhase, HorizonItem, PlanningHorizon
from app.models.founder_memory import FounderMemoryNote

CHECKPOINT_NOTE_TYPE = "observation"
CHECKPOINT_MARKER = "mainai_executive_continuity_v1"


def _horizon_to_dict(item: HorizonItem) -> dict[str, Any]:
    return {
        "horizon": item.horizon.value,
        "title": item.title,
        "rationale": item.rationale,
        "confidence": item.confidence,
        "dependencies": list(item.dependencies),
        "authorized": False,  # always False on write — FUTURE PLAN != AUTHORITY
        "provenance": dict(item.provenance),
    }


def checkpoint_to_dict(cp: ContinuityCheckpoint) -> dict[str, Any]:
    return {
        "marker": CHECKPOINT_MARKER,
        "session_id": cp.session_id,
        "phase": cp.phase.value,
        "founder_request": cp.founder_request,
        "context_set_id": cp.context_set_id,
        "note_id": cp.note_id,
        "source_entity_id": cp.source_entity_id,
        "work_candidate_ids": list(cp.work_candidate_ids),
        "lesson_ids": list(cp.lesson_ids),
        "horizon_items": list(cp.horizon_items),
        "staffing_action": cp.staffing_action,
        "workforce_request_id": cp.workforce_request_id,
        "completed": list(cp.completed),
        "uncertain": list(cp.uncertain),
        "remaining": list(cp.remaining),
        "authority_still_valid": bool(cp.authority_still_valid),
        "authority_notes": list(cp.authority_notes),
        "interruption_point": cp.interruption_point,
        "provenance": dict(cp.provenance),
    }


def checkpoint_from_dict(data: dict[str, Any]) -> ContinuityCheckpoint:
    if data.get("marker") != CHECKPOINT_MARKER:
        raise ValueError("not an executive continuity checkpoint")
    return ContinuityCheckpoint(
        session_id=str(data["session_id"]),
        phase=ExecutivePhase(data["phase"]),
        founder_request=str(data.get("founder_request") or ""),
        context_set_id=data.get("context_set_id"),
        note_id=data.get("note_id"),
        source_entity_id=data.get("source_entity_id"),
        work_candidate_ids=list(data.get("work_candidate_ids") or []),
        lesson_ids=list(data.get("lesson_ids") or []),
        horizon_items=list(data.get("horizon_items") or []),
        staffing_action=data.get("staffing_action"),
        workforce_request_id=data.get("workforce_request_id"),
        completed=list(data.get("completed") or []),
        uncertain=list(data.get("uncertain") or []),
        remaining=list(data.get("remaining") or []),
        authority_still_valid=bool(data.get("authority_still_valid")),
        authority_notes=list(data.get("authority_notes") or []),
        interruption_point=data.get("interruption_point"),
        provenance=dict(data.get("provenance") or {}),
    )


def save_continuity_checkpoint(
    db: Session,
    *,
    owner_id: uuid.UUID,
    checkpoint: ContinuityCheckpoint,
) -> FounderMemoryNote:
    """Append-only checkpoint. Supersedes prior checkpoint for same session via provenance link only."""
    payload = checkpoint_to_dict(checkpoint)
    prior = load_continuity_checkpoint(db, owner_id=owner_id, session_id=checkpoint.session_id)
    supersedes = None
    if prior is not None:
        # Find the note that held the prior checkpoint for provenance chaining.
        prior_note = _latest_checkpoint_note(db, owner_id=owner_id, session_id=checkpoint.session_id)
        if prior_note is not None:
            supersedes = prior_note.id

    content = (
        f"[executive continuity] session={checkpoint.session_id} "
        f"phase={checkpoint.phase.value} "
        f"completed={len(checkpoint.completed)} remaining={len(checkpoint.remaining)}"
    )
    return record_founder_memory(
        db,
        owner_id=owner_id,
        note_type=CHECKPOINT_NOTE_TYPE,
        content=content,
        idempotency_key=f"exec-cont:{checkpoint.session_id}:{uuid.uuid4()}",
        authority="deterministic_source",
        basis="deterministic",
        supersedes_note_id=supersedes,
        source="mainai_executive.continuity",
        provenance={
            "kind": CHECKPOINT_MARKER,
            "session_id": checkpoint.session_id,
            "checkpoint": payload,
            # Explicit denials persisted with every checkpoint.
            "authority_claims": [],
            "future_plan_is_not_authority": True,
        },
    )


def _latest_checkpoint_note(
    db: Session, *, owner_id: uuid.UUID, session_id: str
) -> FounderMemoryNote | None:
    rows = db.execute(
        select(FounderMemoryNote)
        .where(
            FounderMemoryNote.owner_id == owner_id,
            FounderMemoryNote.note_type == CHECKPOINT_NOTE_TYPE,
            FounderMemoryNote.status == "active",
        )
        .order_by(FounderMemoryNote.observed_at.desc())
    ).scalars()
    for note in rows:
        prov = note.provenance or {}
        if prov.get("kind") == CHECKPOINT_MARKER and prov.get("session_id") == session_id:
            return note
    return None


def load_continuity_checkpoint(
    db: Session, *, owner_id: uuid.UUID, session_id: str
) -> ContinuityCheckpoint | None:
    note = _latest_checkpoint_note(db, owner_id=owner_id, session_id=session_id)
    if note is None:
        return None
    raw = (note.provenance or {}).get("checkpoint")
    if not isinstance(raw, dict):
        return None
    return checkpoint_from_dict(raw)


def build_checkpoint_from_cycle(
    *,
    session_id: str,
    phase: ExecutivePhase,
    founder_request: str,
    context_set_id: uuid.UUID | None,
    note_id: uuid.UUID | None,
    source_entity_id: uuid.UUID | None,
    work_candidate_ids: list[uuid.UUID],
    lesson_ids: list[uuid.UUID],
    horizon_items: list[HorizonItem],
    staffing_action: str | None,
    workforce_request_id: uuid.UUID | None,
    completed: list[str],
    uncertain: list[str],
    remaining: list[str],
    interruption_point: str | None = None,
) -> ContinuityCheckpoint:
    return ContinuityCheckpoint(
        session_id=session_id,
        phase=phase,
        founder_request=founder_request,
        context_set_id=str(context_set_id) if context_set_id else None,
        note_id=str(note_id) if note_id else None,
        source_entity_id=str(source_entity_id) if source_entity_id else None,
        work_candidate_ids=[str(x) for x in work_candidate_ids],
        lesson_ids=[str(x) for x in lesson_ids],
        horizon_items=[_horizon_to_dict(h) for h in horizon_items],
        staffing_action=staffing_action,
        workforce_request_id=str(workforce_request_id) if workforce_request_id else None,
        completed=list(completed),
        uncertain=list(uncertain),
        remaining=list(remaining),
        # Executive cycle never holds execution authority by itself.
        authority_still_valid=False,
        authority_notes=[
            "EXECUTIVE_CYCLE_HAS_NO_EXECUTION_AUTHORITY",
            "FUTURE_PLAN_IS_NOT_FUTURE_AUTHORITY",
            "MEMORY_IS_NOT_AUTHORITY",
        ],
        interruption_point=interruption_point,
        provenance={"via": "mainai_executive"},
    )


def resume_summary(checkpoint: ContinuityCheckpoint) -> dict[str, Any]:
    """Founder-facing recovery view — no hallucinated continuation."""
    return {
        "session_id": checkpoint.session_id,
        "was_doing": checkpoint.founder_request,
        "phase": checkpoint.phase.value,
        "completed": list(checkpoint.completed),
        "uncertain": list(checkpoint.uncertain),
        "remaining": list(checkpoint.remaining),
        "authority_still_exists": checkpoint.authority_still_valid,
        "authority_expired_or_absent": not checkpoint.authority_still_valid,
        "authority_notes": list(checkpoint.authority_notes),
        "interruption_point": checkpoint.interruption_point,
        "horizon_plan_count": len(checkpoint.horizon_items),
        "needs_founder_confirmation": True,  # always until separate authorize path
        "hallucinated_continuation": False,
    }
