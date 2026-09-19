"""'What Changes My Mind' -- durable per-conclusion ledger for important conclusions. See
docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture decision.

Persisted through the SAME real mechanism `app.resource_intelligence.session_checkpoint`/
`app.mainai_executive.continuity` already use (`FounderMemoryNote` + `supersedes_note_id`
chain via `app.founder_memory.record_founder_memory()`) -- no new table. A later save for the
SAME conclusion supersedes the prior note, never mutates it in place.

ONE SUCCESS != UNIVERSAL RULE. ONE FAILURE != UNIVERSAL RULE. ONE CORRECTION != UNIVERSAL RULE:
this module records a conclusion's own support/reversal conditions; it never itself promotes a
conclusion into a standing policy (that remains `meta_improvement.py`'s own, separately
governed, `BEHAVIORAL_COMPONENTS`-gated path)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.founder_memory import record_founder_memory
from app.models.founder_memory import FounderMemoryNote
from app.mainai_vision.evidence import MindChangeRecord, mind_change_as_dict

MIND_CHANGE_NOTE_TYPE = "observation"
MIND_CHANGE_MARKER = "mainai_vision_mind_change_v1"


def _latest_mind_change_note(db: Session, *, owner_id: uuid.UUID, conclusion: str) -> FounderMemoryNote | None:
    rows = db.execute(
        select(FounderMemoryNote)
        .where(FounderMemoryNote.owner_id == owner_id, FounderMemoryNote.note_type == MIND_CHANGE_NOTE_TYPE, FounderMemoryNote.status == "active")
        .order_by(FounderMemoryNote.observed_at.desc())
    ).scalars()
    for note in rows:
        provenance = note.provenance or {}
        if provenance.get("kind") == MIND_CHANGE_MARKER and provenance.get("conclusion") == conclusion:
            return note
    return None


def save_mind_change_record(db: Session, *, owner_id: uuid.UUID, record: MindChangeRecord) -> FounderMemoryNote:
    """Append-only: a later save for the SAME `conclusion` text supersedes the prior note via
    `supersedes_note_id`, mirroring `session_checkpoint.save_agent_session_checkpoint()`'s own
    exact mechanism."""

    prior = _latest_mind_change_note(db, owner_id=owner_id, conclusion=record.conclusion)
    return record_founder_memory(
        db,
        owner_id=owner_id,
        note_type=MIND_CHANGE_NOTE_TYPE,
        content=f"[mind change] {record.conclusion} (confidence={record.confidence:.2f})",
        idempotency_key=f"mind-change:{uuid.uuid4()}",
        authority="deterministic_source",
        basis="deterministic",
        supersedes_note_id=prior.id if prior is not None else None,
        source="mainai_vision.mind_change",
        provenance={"kind": MIND_CHANGE_MARKER, "conclusion": record.conclusion, "record": mind_change_as_dict(record)},
    )


def load_mind_change_record(db: Session, *, owner_id: uuid.UUID, conclusion: str) -> MindChangeRecord | None:
    note = _latest_mind_change_note(db, owner_id=owner_id, conclusion=conclusion)
    if note is None:
        return None
    data = (note.provenance or {}).get("record")
    if not isinstance(data, dict):
        return None
    return MindChangeRecord(
        conclusion=data["conclusion"], confidence=data["confidence"], support=tuple(data.get("support", [])),
        contradictions=tuple(data.get("contradictions", [])), assumptions=tuple(data.get("assumptions", [])),
        unknowns=tuple(data.get("unknowns", [])), what_strengthens=tuple(data.get("what_strengthens", [])),
        what_weakens=tuple(data.get("what_weakens", [])), what_reverses=tuple(data.get("what_reverses", [])),
    )
