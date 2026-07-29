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
    except IntegrityError as exc:
        # Only the exact identity-key collision this function is designed to recover from
        # gets caught here. `exc.orig.diag.constraint_name` is psycopg2's mechanism for
        # inspecting which specific constraint fired; any other IntegrityError (a real bug,
        # a different constraint, a locator that's simply invalid) is re-raised unmodified
        # instead of being silently swallowed and misdiagnosed as "someone else already
        # created this" — a caller debugging a genuine data problem needs the real
        # constraint name and the original exception, not this function's guess.
        constraint_name = getattr(getattr(getattr(exc, "orig", None), "diag", None), "constraint_name", None)
        # Roll back to the savepoint regardless of which constraint fired: leaving the
        # session's transaction aborted after an error we're about to re-raise would abort
        # more of the caller's outer transaction than necessary (any other work already
        # done earlier in it) and isn't needed to preserve the original exception, which we
        # still re-raise unmodified below.
        savepoint.rollback()
        if constraint_name != "uq_msu_owner_identity":
            raise

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
        # uq_msu_owner_identity fired but no row is visible under our own snapshot — a
        # concurrent transaction that inserted it hasn't committed yet (SERIALIZABLE) or was
        # itself rolled back after our INSERT already conflicted with it. Either way there is
        # genuinely no id to return; the caller must retry, not receive nothing silently.
        raise MemorySourceIdentityConflict(
            f"uq_msu_owner_identity fired for identity_key={locator.identity_key!r} but no matching "
            f"memory_source_units row is visible — likely a concurrent, not-yet-committed writer; retry"
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

    # A revoked/purged source is never silently reused: revocation/purge is a deliberate,
    # audited lifecycle transition (memory_source_lifecycle_events), and find-or-create
    # re-attaching a NEW claim to a source the owner (or an admin) already revoked/purged
    # would quietly resurrect it as if that transition never happened. Only an `active`
    # source is eligible for reuse; anything else is a real conflict the caller must decide
    # how to handle (e.g. create a fresh source instead), not something this function papers
    # over.
    if existing_msu.lifecycle_status.value != "active":
        raise MemorySourceIdentityConflict(
            f"memory_source_units {existing_msu.id}: source_identity_key={locator.identity_key!r} "
            f"matched an existing source, but its lifecycle_status is "
            f"{existing_msu.lifecycle_status.value!r}, not 'active' — refusing to silently reuse a "
            f"revoked or purged source"
        )
    if existing_msu.snapshot_status != locator.snapshot_status or existing_msu.content_hash != locator.content_hash:
        raise MemorySourceIdentityConflict(
            f"memory_source_units {existing_msu.id}: snapshot_status/content_hash mismatch on lookup "
            f"(expected snapshot_status={locator.snapshot_status}, content_hash={locator.content_hash!r}; "
            f"found snapshot_status={existing_msu.snapshot_status}, content_hash={existing_msu.content_hash!r})"
        )

    return existing_msu.id
