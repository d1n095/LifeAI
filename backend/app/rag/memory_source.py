"""S1A (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8): race-safe, crash-safe find-or-create
for `MemorySourceUnit`/`DocumentSourceUnit` rows. Used by both the deterministic backfill of
existing document claims and the dual-write path for new ones (both still separate, later
commits — this module is the shared primitive both will call).

The SAVEPOINT pattern below (not a single CTE) is deliberate: a CTE relying on `INSERT ...
ON CONFLICT DO NOTHING` followed by a `SELECT` in the same statement can race against a
concurrently-committing transaction under READ COMMITTED (the `SELECT` branch uses the
statement's original snapshot, taken before any wait for the conflicting transaction, and so
can miss a row that a concurrent INSERT just committed). A SAVEPOINT + real INSERT + catch
IntegrityError + rollback-to-savepoint + fresh SELECT is the standard safe Postgres upsert
pattern and has no such race: the fallback SELECT is a brand new statement that takes a fresh
snapshot after the concurrent transaction has already committed.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.memory_source_unit import (
    DocumentSourceUnit,
    MemorySourceUnit,
    OccurredAtBasis,
    SnapshotStatus,
    SourceKind,
    SourceRole,
)


class MemorySourceIdentityConflict(RuntimeError):
    """Raised when a `source_identity_key` collision is found, but the existing row doesn't
    actually match the requested source — e.g. same key, different document/chunk/version, or
    a still-`active` row with an unexpected `snapshot_status`. Never silently returns a
    mismatched id; the caller (backfill/dual-write) must stop and investigate rather than
    attach a claim to the wrong source."""


def source_identity_key(source_kind: SourceKind, *, chunk_id: uuid.UUID | None, version_id: uuid.UUID | None, document_id: uuid.UUID) -> str:
    """The stable, immutable dedup key `document_source_units_validate` (migration 0019)
    also independently verifies at INSERT time — computed here from the same three rules so
    application code and the database trigger can never silently disagree."""
    if source_kind == SourceKind.document_chunk:
        return f"document_chunk:{chunk_id}"
    if source_kind == SourceKind.document_version:
        return f"document_version:{version_id}"
    return f"document_record:{document_id}"


@dataclass
class DocumentSourceLocator:
    owner_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID | None
    chunk_id: uuid.UUID | None
    observed_at: datetime
    content_text: str | None
    content_hash: str | None
    snapshot_status: SnapshotStatus

    @property
    def source_kind(self) -> SourceKind:
        if self.chunk_id is not None:
            return SourceKind.document_chunk
        if self.version_id is not None:
            return SourceKind.document_version
        return SourceKind.document_record

    @property
    def identity_key(self) -> str:
        return source_identity_key(
            self.source_kind, chunk_id=self.chunk_id, version_id=self.version_id, document_id=self.document_id
        )


def get_or_create_memory_source_unit(db: Session, locator: DocumentSourceLocator) -> uuid.UUID:
    """Returns the `memory_source_units.id` for `locator`, creating the parent+subtype pair
    together (in one SAVEPOINT-scoped unit of work) if this is the first time this exact
    source has been seen for this owner. `source_role` is always `unknown` for document
    sources in S1A — see §4.8's "source_role": uploader is never author."""
    savepoint = db.begin_nested()
    try:
        msu = MemorySourceUnit(
            owner_id=locator.owner_id,
            source_kind=locator.source_kind,
            source_identity_key=locator.identity_key,
            source_role=SourceRole.unknown,
            observed_at=locator.observed_at,
            occurred_at=None,
            occurred_at_basis=OccurredAtBasis.unknown,
            content_text=locator.content_text,
            content_hash=locator.content_hash,
            snapshot_status=locator.snapshot_status,
        )
        db.add(msu)
        db.flush()
        db.add(
            DocumentSourceUnit(
                memory_source_id=msu.id,
                owner_id=locator.owner_id,
                source_kind=locator.source_kind,
                document_id=locator.document_id,
                version_id=locator.version_id,
                chunk_id=locator.chunk_id,
            )
        )
        db.flush()
        savepoint.commit()
        return msu.id
    except IntegrityError:
        savepoint.rollback()

    existing = (
        db.query(MemorySourceUnit, DocumentSourceUnit)
        .join(DocumentSourceUnit, DocumentSourceUnit.memory_source_id == MemorySourceUnit.id)
        .filter(
            MemorySourceUnit.owner_id == locator.owner_id,
            MemorySourceUnit.source_identity_key == locator.identity_key,
        )
        .one_or_none()
    )
    if existing is None:
        # The IntegrityError wasn't the identity-key conflict we expected (e.g. a real
        # constraint violation elsewhere) — re-raising a clear error beats silently
        # returning nothing, since the caller has no id to attach a claim to either way.
        raise MemorySourceIdentityConflict(
            f"no existing memory_source_units row found for identity_key={locator.identity_key!r} "
            f"after an unexplained IntegrityError"
        )

    existing_msu, existing_dsu = existing
    if (
        existing_msu.source_kind != locator.source_kind
        or existing_dsu.document_id != locator.document_id
        or existing_dsu.version_id != locator.version_id
        or (existing_dsu.chunk_id is not None and existing_dsu.chunk_id != locator.chunk_id)
    ):
        raise MemorySourceIdentityConflict(
            f"memory_source_units {existing_msu.id}: source_identity_key={locator.identity_key!r} "
            f"matched but locator differs (existing document_id={existing_dsu.document_id}, "
            f"version_id={existing_dsu.version_id}, chunk_id={existing_dsu.chunk_id} vs. "
            f"requested document_id={locator.document_id}, version_id={locator.version_id}, "
            f"chunk_id={locator.chunk_id})"
        )
    if existing_msu.lifecycle_status.value == "active" and existing_msu.snapshot_status != locator.snapshot_status:
        raise MemorySourceIdentityConflict(
            f"memory_source_units {existing_msu.id}: snapshot_status mismatch on lookup "
            f"(expected {locator.snapshot_status}, found {existing_msu.snapshot_status})"
        )

    return existing_msu.id
