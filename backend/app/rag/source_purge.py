"""The shared purge service for a single knowledge source (docs/MAINAI_PROJECT_UNDERSTANDING_
PLAN.md §4.8's "En gemensam purge-tjänst") — the ONE place both app/routers/library.py's
`delete_source` and the older, still-live `DELETE /api/documents/{id}`
(app/routers/documents.py) call. Neither router implements its own cleanup logic; both are
thin wrappers around `purge_source()` here.

Why this had to become a shared service rather than staying two separate implementations:
migration 0019's `document_source_units.document_id` FK has no `ON DELETE` action (plain
RESTRICT), so a hard `DELETE FROM documents` — what `app/routers/documents.py`'s route used
to do — now fails outright with a foreign-key violation the moment any `memory_source_units`
row exists for that document. `documents.py`'s route now gets the exact same soft-delete +
blob-purge + memory-purge behavior `library.py` already had — a deliberate behavior change for
that older route (its own previous docstring already flagged an unrelated, unresolved
multi-uploader chunk-deletion gap), not a bug.

Ordering, and why it's not arbitrary: every `document_source_units` row for this document whose
parent `memory_source_units.lifecycle_status` is still `active`/`revoked` is transitioned to
`purged` via `transition_own_memory_source()` (the same reviewed, owner-verified,
audit-logged lifecycle function S1A's core already uses — never a direct `UPDATE`) BEFORE any
`DocumentChunk` row is hard-deleted. `trg_dsu_guard_update` (migration 0019) only allows a
`document_source_units.chunk_id` to go from set to NULL (the `ON DELETE SET NULL` a
`DocumentChunk` delete triggers) once its parent's `lifecycle_status` is no longer `active` —
purging first is what makes the later chunk delete legal instead of raising. An already-`purged`
row is left alone (idempotent no-op): `transition_own_memory_source()` has no `purged -> purged`
transition and would raise `illegal transition purged -> purged` otherwise.

What this deliberately does NOT do, matching the founder's explicit revision of §4.8's original
per-document purge steps for a single SOURCE deletion (as opposed to full account erasure,
which is `erase_owner_memory()` — untouched here, out of scope for this PR): `KnowledgeClaim`
rows are NEVER deleted here. A claim survives as historical record, still pointing at its own
(now-purged, content-nulled) `memory_source_id` — the founder's own claim-level trust judgment
about a piece of text shouldn't disappear just because its source was deleted.
`memory_source_units`/`document_source_units`/`memory_source_lifecycle_events` rows themselves
are NEVER deleted here either (only the owner-scoped `erase_owner_memory()` admin path can hard
-delete those, per migration 0019's `trg_dsu_forbid_delete`/`trg_msu_no_delete`). A document that
predates S1A's backfill (or was never dual-written) can have ZERO `document_source_units` rows
at all — `legacy_without_memory_source` on the result flags this; no source unit is ever
fabricated to "fill the gap," and normal chunk/document deletion proceeds exactly as it would
for a document that never had one.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.models.document import DeletionStatus, Document, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_claim import KnowledgeClaim
from app.models.memory_source_unit import DocumentSourceUnit, LifecycleStatus, MemorySourceUnit
from app.rag.library_import import maybe_purge_blob
from app.storage import get_storage

logger = logging.getLogger("mainai.rag.source_purge")

PURGE_REASON = "source_deleted"


class SourcePurgeNotFoundError(RuntimeError):
    """Raised when document_id doesn't resolve to a live, owner_id-owned Document — a missing
    row, someone else's row (RLS/explicit ownership check both apply), or one already
    soft-deleted. Callers translate this to an HTTP 404 uniformly; never leaks which of those
    three it actually was."""


@dataclass
class PurgeSourceResult:
    document_id: uuid.UUID
    sources_purged: int = 0
    sources_already_purged: int = 0
    chunks_deleted: int = 0
    claims_preserved: int = 0
    legacy_without_memory_source: bool = False
    deletion_status: DeletionStatus = DeletionStatus.none


def purge_source(db: Session, document_id: uuid.UUID, owner_id: uuid.UUID) -> PurgeSourceResult:
    """Deletes a knowledge source: soft-deletes the Document row, hard-deletes its
    DocumentChunk rows, purges (never hard-deletes) every associated MemorySourceUnit, and
    releases its storage blob if no other live document still references it (content-addressed
    — see maybe_purge_blob). KnowledgeClaim/MemorySourceUnit/lifecycle-event rows all survive,
    see module docstring. Atomic: either the whole thing commits, or none of it does — mirrors
    app/routers/account.py's delete_account's explicit try/except/rollback discipline rather
    than relying on the request-scoped session's implicit teardown to clean up a failed
    transaction.

    The caller's session must already be RLS-scoped to owner_id (same convention as every
    other app/rag/*.py entry point in this codebase) — ownership is additionally verified
    explicitly here (`Document.uploaded_by == owner_id`), not left to RLS alone, so a bug
    disabling RLS would still fail closed rather than silently purging the wrong owner's
    source. Raises SourcePurgeNotFoundError if document_id doesn't resolve to a live document
    owned by owner_id.
    """
    try:
        # FOR UPDATE on the Document row is the real serialization point: a second concurrent
        # purge_source() call for the SAME document blocks here until the first one commits
        # (deleted_at is now set) or rolls back (row unchanged) — the second call's own query
        # then either finds nothing (clean 404, "already deleted") or proceeds normally,
        # instead of racing transition_own_memory_source() into an illegal purged -> purged
        # transition on a row the first call already finished with.
        document = (
            db.query(Document)
            .filter(Document.id == document_id, Document.uploaded_by == owner_id, Document.deleted_at.is_(None))
            .with_for_update()
            .first()
        )
        if document is None:
            raise SourcePurgeNotFoundError(f"document {document_id} not found or not owned by {owner_id}")

        result = PurgeSourceResult(document_id=document_id)

        dsu_rows = (
            db.query(DocumentSourceUnit, MemorySourceUnit)
            .join(MemorySourceUnit, MemorySourceUnit.id == DocumentSourceUnit.memory_source_id)
            .filter(DocumentSourceUnit.document_id == document_id, DocumentSourceUnit.owner_id == owner_id)
            .all()
        )
        if not dsu_rows:
            result.legacy_without_memory_source = True

        for _dsu, msu in dsu_rows:
            if msu.lifecycle_status == LifecycleStatus.purged:
                result.sources_already_purged += 1
                continue
            db.execute(
                sa_text("SELECT transition_own_memory_source(:id, 'purged', :reason)"),
                {"id": str(msu.id), "reason": PURGE_REASON},
            )
            result.sources_purged += 1

        # Only now — every DSU row's parent is either already-purged or just transitioned to
        # purged above — is it legal for the DSU's chunk_id to go to NULL, which this delete
        # triggers via ON DELETE SET NULL. A legacy document with no DSU rows at all has
        # nothing gating this; it deletes exactly as it always did.
        result.chunks_deleted = (
            db.query(DocumentChunk)
            .filter(DocumentChunk.document_id == document_id, DocumentChunk.owner_id == owner_id)
            .delete(synchronize_session=False)
        )

        result.claims_preserved = (
            db.query(KnowledgeClaim)
            .filter(KnowledgeClaim.source_id == document_id, KnowledgeClaim.owner_id == owner_id)
            .count()
        )

        # Document's own soft-delete bookkeeping — unchanged from the pre-S1A delete_source
        # behavior (app/routers/library.py), now shared by both callers.
        document.deleted_at = datetime.utcnow()
        document.chunk_count = 0
        if document.status not in (IndexStatus.indexed, IndexStatus.failed, IndexStatus.cancelled):
            document.status = IndexStatus.cancelled
            document.error_message = "Källan togs bort innan bearbetningen slutfördes."

        if document.import_job_id is not None:
            job = db.get(ImportJob, document.import_job_id)
            if job is not None and job.status in (ImportJobStatus.pending, ImportJobStatus.running) and job.progress_total <= 1:
                job.status = ImportJobStatus.cancelled
                job.completed_at = datetime.utcnow()
                db.add(job)

        db.add(document)
        db.flush()  # deleted_at must be visible to maybe_purge_blob's "still referenced?" query below

        storage = get_storage()
        document.deletion_status = maybe_purge_blob(db, storage, document.storage_key)
        db.add(document)
        result.deletion_status = document.deletion_status

        db.commit()
        return result
    except SourcePurgeNotFoundError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Källradering misslyckades för document_id=%s (owner=%s), återställd (rollback).", document_id, owner_id)
        raise
