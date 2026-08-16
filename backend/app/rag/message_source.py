"""S1C (docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md, migration 0037,
docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8/§8): race-safe, crash-safe find-or-create for
`MemorySourceUnit`/`MessageSourceUnit` rows -- the exact same SAVEPOINT pattern
app/rag/memory_source.py already established for `DocumentSourceUnit`, applied to `Message`
instead. See that module's docstring for why a SAVEPOINT (not a single upsert CTE) is the
correct, race-safe primitive here.

The one real difference from the document case: `source_role` is not always `unknown` here --
a message's `role` column is genuine, known authorship recorded at write time
(app/routers/chat.py), so it maps directly (see `source_role_for_message_role`) rather than
defaulting to unknown. migration 0037's `trg_msgsu_validate_fields` independently re-verifies
this same mapping at the database level -- application code and the trigger can never silently
disagree, mirroring how `source_identity_key` is computed identically in both places for
document sources.
"""

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.conversation import MessageRole
from app.models.memory_source_unit import (
    MemorySourceUnit,
    MessageSourceUnit,
    OccurredAtBasis,
    SnapshotStatus,
    SourceKind,
    SourceRole,
)

CONTENT_HASH_VERSION = "sha256-utf8-v1"

_ROLE_TO_SOURCE_ROLE = {
    MessageRole.user: SourceRole.founder,
    MessageRole.assistant: SourceRole.assistant,
    MessageRole.system: SourceRole.system,
}


def source_role_for_message_role(role: MessageRole) -> SourceRole:
    """The single source of truth application code uses for this mapping -- migration 0037's
    trg_msgsu_validate_fields hardcodes the identical mapping in SQL as an independent,
    database-level check, not a duplicate that could drift silently."""
    return _ROLE_TO_SOURCE_ROLE[role]


def compute_content_hash(content_text: str | None) -> tuple[str | None, str | None]:
    if content_text is None:
        return None, None
    return hashlib.sha256(content_text.encode("utf-8")).hexdigest(), CONTENT_HASH_VERSION


def message_source_identity_key(message_id: uuid.UUID) -> str:
    return f"message:{message_id}"


class MessageSourceIdentityConflict(RuntimeError):
    """Mirrors app/rag/memory_source.py's MemorySourceIdentityConflict -- raised when a
    source_identity_key collision is found but the existing row doesn't actually match the
    requested message. The caller must stop and investigate, never silently attach to the
    wrong source."""


@dataclass
class MessageSourceLocator:
    owner_id: uuid.UUID
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    role: MessageRole
    observed_at: datetime
    content_text: str
    snapshot_status: SnapshotStatus = SnapshotStatus.exact

    @property
    def identity_key(self) -> str:
        return message_source_identity_key(self.message_id)

    @property
    def source_role(self) -> SourceRole:
        return source_role_for_message_role(self.role)


def get_or_create_message_source_unit(db: Session, locator: MessageSourceLocator) -> uuid.UUID:
    """Returns the `memory_source_units.id` for `locator`, creating the parent+subtype pair
    together (one SAVEPOINT-scoped unit of work) the first time this message is seen for this
    owner. Idempotent: calling this again for the same message_id returns the same id."""
    content_hash, content_hash_version = compute_content_hash(locator.content_text)
    savepoint = db.begin_nested()
    try:
        msu = MemorySourceUnit(
            owner_id=locator.owner_id,
            source_kind=SourceKind.message,
            source_identity_key=locator.identity_key,
            source_role=locator.source_role,
            observed_at=locator.observed_at,
            occurred_at=None,
            occurred_at_basis=OccurredAtBasis.unknown,
            content_text=locator.content_text,
            content_hash=content_hash,
            content_hash_version=content_hash_version,
            snapshot_status=locator.snapshot_status,
        )
        db.add(msu)
        db.flush()
        db.add(
            MessageSourceUnit(
                memory_source_id=msu.id,
                owner_id=locator.owner_id,
                source_kind=SourceKind.message,
                message_id=locator.message_id,
                conversation_id=locator.conversation_id,
            )
        )
        db.flush()
        savepoint.commit()
        return msu.id
    except IntegrityError as exc:
        constraint_name = getattr(getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", None)
        savepoint.rollback()
        if constraint_name != "uq_msu_owner_identity":
            raise

    existing = (
        db.query(MemorySourceUnit, MessageSourceUnit)
        .join(MessageSourceUnit, MessageSourceUnit.memory_source_id == MemorySourceUnit.id)
        .filter(
            MemorySourceUnit.owner_id == locator.owner_id,
            MemorySourceUnit.source_identity_key == locator.identity_key,
        )
        .one_or_none()
    )
    if existing is None:
        raise MessageSourceIdentityConflict(
            f"uq_msu_owner_identity fired for identity_key={locator.identity_key!r} but no matching "
            f"memory_source_units row is visible — likely a concurrent, not-yet-committed writer; retry"
        )

    existing_msu, existing_msgsu = existing
    if existing_msgsu.message_id != locator.message_id or existing_msgsu.conversation_id != locator.conversation_id:
        raise MessageSourceIdentityConflict(
            f"memory_source_units {existing_msu.id}: source_identity_key={locator.identity_key!r} "
            f"matched but locator differs (existing message_id={existing_msgsu.message_id}, "
            f"conversation_id={existing_msgsu.conversation_id} vs. requested "
            f"message_id={locator.message_id}, conversation_id={locator.conversation_id})"
        )
    if existing_msu.lifecycle_status.value != "active":
        raise MessageSourceIdentityConflict(
            f"memory_source_units {existing_msu.id}: source_identity_key={locator.identity_key!r} "
            f"matched an existing source, but its lifecycle_status is "
            f"{existing_msu.lifecycle_status.value!r}, not 'active' — refusing to silently reuse a "
            f"revoked or purged source"
        )
    if existing_msu.content_hash != content_hash:
        raise MessageSourceIdentityConflict(
            f"memory_source_units {existing_msu.id}: content_hash mismatch on lookup "
            f"(expected {content_hash!r}, found {existing_msu.content_hash!r})"
        )

    return existing_msu.id
