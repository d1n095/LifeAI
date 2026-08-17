"""Life Founder/User Memory -- the deterministic boundary between "the founder said/decided
X" and "Life's own interpretation of a pattern it noticed." See migration 0049's own module
docstring for the full reuse map.

This module NEVER infers `authority` or `basis` -- every call site supplies them explicitly,
the same "caller supplies classifications; this module neither invokes providers nor infers
them" doctrine `app.capability_reality.service`'s own module docstring already establishes for
capability facts. There is no code path here that reads conversation text and decides for
itself that something was the founder's own explicit word versus an inferred pattern -- that
classification is the CALLER's responsibility (e.g. `app.context.resolver`'s own
`INTENT_EXPLICIT_MEMORY`/`INTENT_CORRECTION` classifications, which this module's callers may
use to choose `authority="founder"` for an explicit statement), never this module's.

`record_founder_memory()` ALWAYS inserts a new row -- `content` is never rewritten on an
existing row. Superseding an earlier note (`supersedes_note_id`) flips the OLD row's own
`status` to `superseded` in the SAME call, but the old row's `content`/`authority`/`basis`
themselves are never touched -- both the old and the new note remain durably queryable,
preserving full history rather than overwriting it. Idempotent by construction: replaying the
same `idempotency_key` with the SAME field values returns the existing row; replaying it with
DIFFERENT values is a caller bug and raises, never silently picks a winner.

Hard rule, structural, not just documented: no function here reads, infers, or writes anything
about emotional or psychological state. There is no parameter, no `note_type` value, and no
vocabulary anywhere in this module for it -- see `app.context.resolver`'s own identical
constraint, which this module inherits by simply never having the capability at all."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.founder_memory import FounderMemoryNote


class FounderMemoryError(ValueError):
    pass


def _same(row: FounderMemoryNote, values: dict[str, Any]) -> FounderMemoryNote:
    differing = [key for key, value in values.items() if getattr(row, key) != value]
    if differing:
        raise FounderMemoryError(f"idempotency key reused with different fields: {', '.join(sorted(differing))}")
    return row


def record_founder_memory(
    db: Session,
    *,
    owner_id: uuid.UUID,
    note_type: str,
    content: str,
    idempotency_key: str,
    authority: str = "unknown",
    basis: str = "unknown",
    confidence: float | None = None,
    source: str | None = None,
    supersedes_note_id: uuid.UUID | None = None,
    valid_from: datetime | None = None,
    provenance: dict[str, Any] | None = None,
) -> FounderMemoryNote:
    """Records ONE founder/user memory note. When `supersedes_note_id` is supplied, the
    referenced note (which MUST already belong to the same owner, checked with a row lock to
    close a concurrent-supersede race) has its own `status` flipped to `superseded` -- its
    `content` is never touched. Fails closed (`FounderMemoryError`) if the superseded note
    does not exist or belongs to another owner; never silently creates an orphaned supersession
    pointer."""

    old = None
    if supersedes_note_id is not None:
        old = db.execute(
            select(FounderMemoryNote).where(FounderMemoryNote.id == supersedes_note_id, FounderMemoryNote.owner_id == owner_id).with_for_update()
        ).scalar_one_or_none()
        if old is None:
            raise FounderMemoryError("superseded note is missing or belongs to another owner")

    values: dict[str, Any] = dict(
        note_type=note_type, content=content, authority=authority, basis=basis, confidence=confidence,
        source=source, supersedes_note_id=supersedes_note_id, valid_from=valid_from, provenance=provenance or {},
    )
    existing = db.execute(
        select(FounderMemoryNote).where(FounderMemoryNote.owner_id == owner_id, FounderMemoryNote.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)

    if old is not None and old.status == "active":
        old.status = "superseded"

    row = FounderMemoryNote(owner_id=owner_id, idempotency_key=idempotency_key, status="active", **values)
    db.add(row)
    db.flush()
    return row


def mark_founder_memory_disputed(db: Session, *, owner_id: uuid.UUID, note_id: uuid.UUID) -> FounderMemoryNote:
    """The explicit "this note's own truth is now in question" transition -- e.g. the founder
    later contradicts something recorded as `active`, but there is not yet a clear replacement
    note to supersede it with. Never deletes or rewrites `content`; only `status` changes."""

    note = db.execute(select(FounderMemoryNote).where(FounderMemoryNote.id == note_id, FounderMemoryNote.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    if note is None:
        raise FounderMemoryError("note is missing or belongs to another owner")
    note.status = "disputed"
    db.flush()
    return note


def get_founder_memory(db: Session, *, owner_id: uuid.UUID, note_id: uuid.UUID) -> FounderMemoryNote | None:
    return db.execute(select(FounderMemoryNote).where(FounderMemoryNote.id == note_id, FounderMemoryNote.owner_id == owner_id)).scalar_one_or_none()


def list_founder_memory(
    db: Session, *, owner_id: uuid.UUID, note_type: str | None = None, status: str | None = None, authority: str | None = None
) -> list[FounderMemoryNote]:
    stmt = select(FounderMemoryNote).where(FounderMemoryNote.owner_id == owner_id)
    if note_type is not None:
        stmt = stmt.where(FounderMemoryNote.note_type == note_type)
    if status is not None:
        stmt = stmt.where(FounderMemoryNote.status == status)
    if authority is not None:
        stmt = stmt.where(FounderMemoryNote.authority == authority)
    return list(db.execute(stmt.order_by(FounderMemoryNote.observed_at)).scalars().all())
