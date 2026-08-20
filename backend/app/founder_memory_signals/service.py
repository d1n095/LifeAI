"""Life Candidate Learning Signals -- the staging layer between a live signal producer
(currently `app.context.resolver`, wired into `app/routers/chat.py`) and `app.founder_memory`'s
own trusted truth. See migration 0053's own module docstring and docs/LIFE_FOUNDER_MEMORY.md's
"Candidate learning signals" section for the full architecture.

Hard rule, structural not just documented: `record_candidate_signal()` NEVER writes to
`founder_memory_notes`, directly or indirectly. The ONLY function in this module that can
create a `FounderMemoryNote` is `promote_candidate_signal()`, and it ALWAYS requires the
caller to supply `authority`/`basis` explicitly -- the signal's own `classifier_confidence`
(a fact about a heuristic's certainty in ITS OWN classification) is never silently copied into
the note's `authority` (a fact about who/what asserted the note's content). A `classifier_
confidence="high"` correction-marker match is still, at most, `authority="ai_interpretation"`
or `authority="inferred_pattern"` unless a human reviewer explicitly asserts otherwise --
promotion is where that judgment call belongs, never automatic."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.founder_memory.service import record_founder_memory
from app.models.candidate_learning_signal import CandidateLearningSignal


class CandidateLearningSignalError(ValueError):
    pass


def _same(row: CandidateLearningSignal, values: dict[str, Any]) -> CandidateLearningSignal:
    differing = [key for key, value in values.items() if getattr(row, key) != value]
    if differing:
        raise CandidateLearningSignalError(f"idempotency key reused with different fields: {', '.join(sorted(differing))}")
    return row


def record_candidate_signal(
    db: Session,
    *,
    owner_id: uuid.UUID,
    signal_kind: str,
    idempotency_key: str,
    source_type: str = "message",
    source_message_id: uuid.UUID | None = None,
    classifier_strategy: str = "unknown",
    classifier_confidence: str = "unknown",
    classifier_reasoning: str | None = None,
    provenance: dict[str, Any] | None = None,
) -> CandidateLearningSignal:
    """Records ONE candidate signal -- never a claim about the world, only a claim that a
    signal producer noticed something. Safe to call from a live, observational hot path (see
    `app/routers/chat.py`'s own `resolve_context()` integration): this function never raises
    for "the signal turned out to be noise" -- that judgment happens later, explicitly, via
    `dismiss_candidate_signal()`/`promote_candidate_signal()`, never here."""

    values: dict[str, Any] = dict(
        source_type=source_type, source_message_id=source_message_id, signal_kind=signal_kind,
        classifier_strategy=classifier_strategy, classifier_confidence=classifier_confidence,
        classifier_reasoning=classifier_reasoning, provenance=provenance or {},
    )
    existing = db.execute(
        select(CandidateLearningSignal).where(CandidateLearningSignal.owner_id == owner_id, CandidateLearningSignal.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)

    row = CandidateLearningSignal(owner_id=owner_id, idempotency_key=idempotency_key, status="unreviewed", **values)
    db.add(row)
    db.flush()
    return row


def dismiss_candidate_signal(db: Session, *, owner_id: uuid.UUID, signal_id: uuid.UUID, reason: str) -> CandidateLearningSignal:
    """An explicit "this signal was noise, not worth promoting" outcome -- never deletes the
    row, so the same non-signal is not re-surfaced for review indefinitely without a durable
    record that it was already considered."""

    row = db.execute(
        select(CandidateLearningSignal).where(CandidateLearningSignal.id == signal_id, CandidateLearningSignal.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise CandidateLearningSignalError("candidate signal is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise CandidateLearningSignalError(f"candidate signal is already {row.status}, not unreviewed")
    row.status = "dismissed"
    row.dismissed_reason = reason
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def promote_candidate_signal(
    db: Session,
    *,
    owner_id: uuid.UUID,
    signal_id: uuid.UUID,
    note_type: str,
    content: str,
    authority: str,
    basis: str,
    note_idempotency_key: str,
    confidence: float | None = None,
) -> tuple[CandidateLearningSignal, Any]:
    """The ONLY path from a candidate signal to real founder knowledge -- SIGNAL PRODUCER !=
    TRUTH WRITER enforced here, not just in the schema's own CHECK constraint. `authority`/
    `basis` are ALWAYS the caller's own explicit assertion (this function has no default that
    reads them off the signal itself) -- promoting a `classifier_confidence="high"` signal
    does not imply `authority="founder"`; a reviewer who confirms the founder really did mean
    it passes `authority="founder"` themselves, deliberately, the same way `record_founder_
    memory()` already requires everywhere else. `content` is likewise always caller-supplied,
    never auto-derived from the source message's raw text -- a reviewer may summarize, quote
    verbatim, or add context; either way it is a deliberate, reviewed act of writing, not a
    copy."""

    row = db.execute(
        select(CandidateLearningSignal).where(CandidateLearningSignal.id == signal_id, CandidateLearningSignal.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise CandidateLearningSignalError("candidate signal is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise CandidateLearningSignalError(f"candidate signal is already {row.status}, not unreviewed")

    note = record_founder_memory(
        db, owner_id=owner_id, note_type=note_type, content=content, idempotency_key=note_idempotency_key,
        authority=authority, basis=basis, confidence=confidence,
        provenance={"promoted_from_candidate_signal_id": str(row.id)},
    )
    row.status = "promoted"
    row.promoted_to_note_id = note.id
    row.updated_at = datetime.utcnow()
    db.flush()
    return row, note


def get_candidate_signal(db: Session, *, owner_id: uuid.UUID, signal_id: uuid.UUID) -> CandidateLearningSignal | None:
    return db.execute(select(CandidateLearningSignal).where(CandidateLearningSignal.id == signal_id, CandidateLearningSignal.owner_id == owner_id)).scalar_one_or_none()


def list_candidate_signals(db: Session, *, owner_id: uuid.UUID, status: str | None = None, signal_kind: str | None = None) -> list[CandidateLearningSignal]:
    stmt = select(CandidateLearningSignal).where(CandidateLearningSignal.owner_id == owner_id)
    if status is not None:
        stmt = stmt.where(CandidateLearningSignal.status == status)
    if signal_kind is not None:
        stmt = stmt.where(CandidateLearningSignal.signal_kind == signal_kind)
    return list(db.execute(stmt.order_by(CandidateLearningSignal.observed_at)).scalars().all())


def list_unreviewed_candidate_signals(db: Session, *, owner_id: uuid.UUID) -> list[CandidateLearningSignal]:
    return list_candidate_signals(db, owner_id=owner_id, status="unreviewed")
