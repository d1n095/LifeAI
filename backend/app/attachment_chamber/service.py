"""Attachment Chamber -- state orchestration (Milestone 1).

Owns the only public mutation surface for AttachmentIdentity/QuarantineState. Every
transition is validated against QUARANTINE_TRANSITIONS and recorded as a hash-chained,
append-only AttachmentEvent -- same discipline as app.guardian's ContainmentReceipt chain.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.attachment_chamber.types import (
    AttachmentEvent,
    AttachmentIdentity,
    AttachmentSource,
    InvalidQuarantineTransitionError,
    MalformedSnapshotError,
    NotFoundError,
    OwnerMismatchError,
    QUARANTINE_TRANSITIONS,
    QuarantineState,
    TERMINAL_QUARANTINE_STATES,
    TerminalQuarantineStateError,
)

_GENESIS_HASH = "0" * 64


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AttachmentChamberState:
    """Per-process/test in-memory registry. Real deployments would back this with the same
    bounded-local-directory pattern app.storage.local_fs already uses for blob bytes; this
    foundation stage keeps identity/event bookkeeping in memory, matching every sibling V2
    package's own foundation-stage scope (Guardian/Sentinel/etc. are all in-memory too)."""

    _identities: dict[uuid.UUID, AttachmentIdentity] = field(default_factory=dict)
    _events: list[AttachmentEvent] = field(default_factory=list)

    def identities_snapshot(self) -> tuple[AttachmentIdentity, ...]:
        return tuple(self._identities.values())


def new_chamber_state() -> AttachmentChamberState:
    return AttachmentChamberState()


def _identity(state: AttachmentChamberState, *, owner_id: uuid.UUID, attachment_id: uuid.UUID) -> AttachmentIdentity:
    row = state._identities.get(attachment_id)
    if row is None:
        raise NotFoundError(f"attachment {attachment_id} not found")
    if row.owner_id != owner_id:
        raise OwnerMismatchError(f"attachment {attachment_id} belongs to another owner")
    return row


# --- Hash-chained events. ----------------------------------------------------------------


def _event_hash(event: AttachmentEvent) -> str:
    """Covers every field a tamperer might want to rewrite after the fact -- from/to state,
    detail, timestamps -- not just identifiers. Mirrors app.guardian.service._receipt_hash's
    own documented reasoning for why a narrower hash would be a real bug."""
    payload = {
        "event_id": str(event.event_id),
        "attachment_id": str(event.attachment_id),
        "event_type": event.event_type,
        "from_state": event.from_state.value if event.from_state else None,
        "to_state": event.to_state.value if event.to_state else None,
        "detail": event.detail,
        "prev_hash": event.prev_hash,
        "created_at": event.created_at.isoformat(),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _record_event(state: AttachmentChamberState, *, attachment_id: uuid.UUID, event_type: str, from_state, to_state, detail: dict) -> AttachmentEvent:
    prev_hash = state._events[-1].this_hash if state._events else _GENESIS_HASH
    event = AttachmentEvent(
        event_id=uuid.uuid4(), attachment_id=attachment_id, event_type=event_type,
        from_state=from_state, to_state=to_state, detail=detail, prev_hash=prev_hash,
    )
    event.this_hash = _event_hash(event)
    state._events.append(event)
    return event


def verify_receipt_chain_intact(state: AttachmentChamberState) -> bool:
    prev = _GENESIS_HASH
    for event in state._events:
        if event.prev_hash != prev:
            return False
        if _event_hash(event) != event.this_hash:
            return False
        prev = event.this_hash
    return True


def events_for_attachment(state: AttachmentChamberState, *, owner_id: uuid.UUID, attachment_id: uuid.UUID) -> tuple[AttachmentEvent, ...]:
    _identity(state, owner_id=owner_id, attachment_id=attachment_id)  # owner-scoping check
    return tuple(e for e in state._events if e.attachment_id == attachment_id)


# --- Identity lifecycle. -------------------------------------------------------------------


def receive_attachment(
    state: AttachmentChamberState, *, owner_id: uuid.UUID, source: AttachmentSource,
    size_bytes: int, declared_mime: str | None, extension: str, content_hash: str,
    original_filename: str, normalized_filename: str, quarantine_location: str,
    parent_archive_id: uuid.UUID | None = None, provenance: str = "top-level upload",
) -> AttachmentIdentity:
    """FILE RECEIVED != FILE TRUSTED: always starts at RECEIVED, never anywhere implying
    safety. This function does NOT itself write any bytes to disk -- callers are expected to
    have already placed the file at `quarantine_location` (see path_safety.py for how that
    location must be derived) before recording its identity here."""
    identity = AttachmentIdentity(
        attachment_id=uuid.uuid4(), owner_id=owner_id, source=source, ingest_timestamp=_utcnow(),
        size_bytes=size_bytes, declared_mime=declared_mime, extension=extension, content_hash=content_hash,
        original_filename=original_filename, normalized_filename=normalized_filename,
        quarantine_location=quarantine_location, parent_archive_id=parent_archive_id, provenance=provenance,
        tracked_artifact_paths=(quarantine_location,),
    )
    state._identities[identity.attachment_id] = identity
    _record_event(state, attachment_id=identity.attachment_id, event_type="received", from_state=None, to_state=QuarantineState.RECEIVED, detail={"source": source.value})
    return identity


def transition_quarantine_state(
    state: AttachmentChamberState, *, owner_id: uuid.UUID, attachment_id: uuid.UUID,
    to_state: QuarantineState, reason: str,
) -> AttachmentIdentity:
    identity = _identity(state, owner_id=owner_id, attachment_id=attachment_id)
    old = identity.lifecycle_state
    if old == to_state:
        return identity  # same-state no-op, no event -- mirrors LIFE_INTENT_TRANSITIONS' own convention
    if old in TERMINAL_QUARANTINE_STATES:
        raise TerminalQuarantineStateError(f"attachment {attachment_id} is already terminal ({old.value}); cannot transition to {to_state.value}")
    if to_state not in QUARANTINE_TRANSITIONS.get(old, set()):
        raise InvalidQuarantineTransitionError(f"cannot transition attachment {attachment_id} from {old.value} to {to_state.value}")
    identity.lifecycle_state = to_state
    identity.updated_at = _utcnow()
    _record_event(state, attachment_id=attachment_id, event_type="state_changed", from_state=old, to_state=to_state, detail={"reason": reason})
    return identity


def track_artifact(state: AttachmentChamberState, *, owner_id: uuid.UUID, attachment_id: uuid.UUID, path: str) -> AttachmentIdentity:
    """Registers a new on-disk artifact (a sanitized derivative, a cached preview, a parser
    scratch file) as belonging to this attachment, so deletion has something concrete to
    iterate -- see deletion.py."""
    identity = _identity(state, owner_id=owner_id, attachment_id=attachment_id)
    if path not in identity.tracked_artifact_paths:
        identity.tracked_artifact_paths = (*identity.tracked_artifact_paths, path)
    return identity


# --- Snapshot round-trip. -----------------------------------------------------------------


def _identity_to_dict(identity: AttachmentIdentity) -> dict:
    return {
        "attachment_id": str(identity.attachment_id), "owner_id": str(identity.owner_id),
        "source": identity.source.value, "ingest_timestamp": identity.ingest_timestamp.isoformat(),
        "size_bytes": identity.size_bytes, "declared_mime": identity.declared_mime,
        "detected_mime": identity.detected_mime, "extension": identity.extension,
        "content_hash": identity.content_hash,
        "parent_archive_id": str(identity.parent_archive_id) if identity.parent_archive_id else None,
        "original_filename": identity.original_filename, "normalized_filename": identity.normalized_filename,
        "quarantine_location": identity.quarantine_location, "lifecycle_state": identity.lifecycle_state.value,
        "sanitization_status": identity.sanitization_status, "provenance": identity.provenance,
        "tracked_artifact_paths": list(identity.tracked_artifact_paths),
        "updated_at": identity.updated_at.isoformat(),
    }


def _identity_from_dict(d: dict) -> AttachmentIdentity:
    return AttachmentIdentity(
        attachment_id=uuid.UUID(d["attachment_id"]), owner_id=uuid.UUID(d["owner_id"]),
        source=AttachmentSource(d["source"]), ingest_timestamp=datetime.fromisoformat(d["ingest_timestamp"]),
        size_bytes=d["size_bytes"], declared_mime=d.get("declared_mime"), detected_mime=d.get("detected_mime"),
        extension=d["extension"], content_hash=d["content_hash"],
        parent_archive_id=uuid.UUID(d["parent_archive_id"]) if d.get("parent_archive_id") else None,
        original_filename=d["original_filename"], normalized_filename=d["normalized_filename"],
        quarantine_location=d["quarantine_location"], lifecycle_state=QuarantineState(d["lifecycle_state"]),
        sanitization_status=d.get("sanitization_status", "not_sanitized"), provenance=d.get("provenance", "top-level upload"),
        tracked_artifact_paths=tuple(d.get("tracked_artifact_paths", [])),
        updated_at=datetime.fromisoformat(d["updated_at"]),
    )


_REQUIRED_SNAPSHOT_KEYS = {
    "attachment_id", "owner_id", "source", "ingest_timestamp", "size_bytes", "extension",
    "content_hash", "original_filename", "normalized_filename", "quarantine_location",
    "lifecycle_state", "updated_at",
}


def to_snapshot(identity: AttachmentIdentity) -> dict:
    return _identity_to_dict(identity)


def from_snapshot(snapshot: dict) -> AttachmentIdentity:
    missing = _REQUIRED_SNAPSHOT_KEYS - snapshot.keys()
    if missing:
        raise MalformedSnapshotError(f"attachment snapshot missing required keys: {sorted(missing)}")
    try:
        return _identity_from_dict(snapshot)
    except (KeyError, ValueError, TypeError) as exc:
        raise MalformedSnapshotError(f"attachment snapshot is malformed: {exc}") from exc
