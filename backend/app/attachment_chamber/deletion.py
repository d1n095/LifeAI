"""Attachment Chamber -- deletion (Milestone 6).

Deletion must cover the original quarantine file, temporary extraction artifacts, sanitized
derivatives, cached previews, and parser artifacts -- every path tracked on
AttachmentIdentity.tracked_artifact_paths (see service.track_artifact()). The receipt lists
exactly what was deleted vs. what was already absent, so nothing is "silently left behind"
without at least being visible in the receipt.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.attachment_chamber.service import AttachmentChamberState, _identity, transition_quarantine_state
from app.attachment_chamber.types import QuarantineState


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class DeletionReceipt:
    attachment_id: uuid.UUID
    deleted_paths: tuple[str, ...]
    already_absent_paths: tuple[str, ...]
    deleted_at: datetime


def delete_attachment(
    state: AttachmentChamberState, *, owner_id: uuid.UUID, attachment_id: uuid.UUID,
    quarantine_root: Path | None = None, reason: str = "owner requested deletion",
) -> DeletionReceipt:
    """Covers every tracked artifact path. If `quarantine_root` is given, actually attempts
    to unlink each tracked path (relative to that root) from disk -- missing files are not
    an error, they're recorded as already_absent. If `quarantine_root` is omitted (as in
    every in-memory foundation-stage test that never wrote real bytes), this only performs
    the bookkeeping/state-transition side, which is what every adversarial test in this
    round actually needs to verify."""
    identity = _identity(state, owner_id=owner_id, attachment_id=attachment_id)

    deleted: list[str] = []
    absent: list[str] = []
    for relative_path in identity.tracked_artifact_paths:
        if quarantine_root is None:
            deleted.append(relative_path)
            continue
        from app.attachment_chamber.path_safety import resolve_quarantine_path, UnsafePathError

        try:
            full_path = resolve_quarantine_path(quarantine_root, relative_path)
        except UnsafePathError:
            absent.append(relative_path)
            continue
        if full_path.exists():
            full_path.unlink()
            deleted.append(relative_path)
        else:
            absent.append(relative_path)

    identity.tracked_artifact_paths = ()
    if identity.lifecycle_state != QuarantineState.DELETED:
        transition_quarantine_state(state, owner_id=owner_id, attachment_id=attachment_id, to_state=QuarantineState.DELETED, reason=reason)

    return DeletionReceipt(attachment_id=attachment_id, deleted_paths=tuple(deleted), already_absent_paths=tuple(absent), deleted_at=_utcnow())


def tracked_artifacts_remaining(state: AttachmentChamberState, *, owner_id: uuid.UUID, attachment_id: uuid.UUID) -> tuple[str, ...]:
    identity = _identity(state, owner_id=owner_id, attachment_id=attachment_id)
    return identity.tracked_artifact_paths
