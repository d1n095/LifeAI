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

Two phases, deliberately NOT one atomic unit spanning the database AND the filesystem — an
earlier version of this module claimed the whole operation was atomic, which was never actually
true and a founder review caught it: `LocalFilesystemStorage.delete()` physically `unlink()`s
the blob immediately, with no undo. If that succeeded and the DB `commit()` immediately after
it then failed, `db.rollback()` would resurrect a live Document/MemorySourceUnit/DocumentChunk
row whose original file was already, permanently gone — the exact opposite of what "atomic"
promises. The real, honest contract is:

- **Phase A — `purge_source()`, genuinely atomic (DB-only).** Locks the Document row, purges
  every MemorySourceUnit, hard-deletes the DocumentChunk rows, soft-deletes the Document, and
  sets `deletion_status` to `pending` (or `purged` immediately if there's no `storage_key` at
  all — nothing to purge) — commits, or on any failure rolls back to a state where NOTHING
  changed and the original blob is still exactly where it was. No `storage.delete()` call
  happens anywhere in this phase.
- **Phase B — `retry_source_blob_purge()`, idempotent and independently retryable.** Only ever
  runs against a document phase A has ALREADY committed as soft-deleted. Re-checks for other
  live documents still referencing the same content-addressed `storage_key` (same
  `maybe_purge_blob` logic phase A used to call inline), then either leaves it `pending` (still
  shared) or calls `storage.delete()` and commits `purged`/`failed` in its own, separate
  transaction. Safe to call any number of times: `LocalFilesystemStorage.delete()` uses
  `Path.unlink(missing_ok=True)`, so re-deleting an already-gone file is a no-op, not an error —
  a phase B run that deletes the file but then fails to commit its own status update leaves the
  document retryable, and the NEXT phase B call correctly reaches `purged` without erroring on
  the missing file.

`purge_source()` makes one immediate, best-effort phase B attempt right after phase A's commit
succeeds (todays's actual UX: usually purges the blob in the same request) — but a phase B
failure is caught, logged, and never propagated as this call's own failure and never rolls
phase A back. `deletion_status` staying `pending`/`failed` in the DB is precisely what makes a
LATER, independent `retry_source_blob_purge()` call (a future ops/admin trigger — not wired to
an HTTP route in this PR, intentionally out of scope here) able to finish the job.

Pass 22, two further founder-review fixes on top of the above:

1. **The blob-reference check itself was incomplete.** `maybe_purge_blob()` (still the shared
   function both phase B here and this module call) used to check only live `Document
   .storage_key` rows — never `ImportJob.source_storage_key`, the RAW upload a pending/running/
   resumable import job still needs to read. See app/storage/references.py's module docstring
   for the full incident and the now-canonical `storage_key_still_referenced()` policy both
   this module and the upload endpoint (`POST /api/library/import`, app/routers/library.py)
   share. That module also owns `acquire_storage_key_lock()` — a transaction-scoped Postgres
   advisory lock `retry_source_blob_purge()` now holds around its own check-then-delete
   sequence, closing a TOCTOU race against that same upload endpoint's blob-finalization step.
2. **The `source_purged` audit entry moved INTO phase A's own transaction.** It used to be
   written by the ROUTER, after this function had already returned and already committed, via
   its own separate `record_audit()` commit. A failure in that second commit meant an HTTP
   caller could see a 500 for a purge that had, in fact, already durably succeeded. See
   `purge_source()`'s own docstring for the detail.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.models.document import DeletionStatus, Document, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_claim import KnowledgeClaim
from app.models.memory_source_unit import DocumentSourceUnit, LifecycleStatus, MemorySourceUnit
from app.rag.library_import import maybe_purge_blob
from app.storage import get_storage
from app.storage.references import acquire_storage_key_lock

logger = logging.getLogger("mainai.storage.purge")

PURGE_REASON = "source_deleted"


class SourcePurgeNotFoundError(RuntimeError):
    """Raised when document_id doesn't resolve to a matching, owner_id-owned Document — a
    missing row, someone else's row (RLS/explicit ownership check both apply), or (for
    purge_source() specifically) one already soft-deleted. Callers translate this to an HTTP
    404 uniformly; never leaks which of those it actually was."""


@dataclass
class PurgeSourceResult:
    document_id: uuid.UUID
    sources_purged: int = 0
    sources_already_purged: int = 0
    chunks_deleted: int = 0
    claims_preserved: int = 0
    legacy_without_memory_source: bool = False
    deletion_status: DeletionStatus = DeletionStatus.none


def purge_source(
    db: Session, document_id: uuid.UUID, owner_id: uuid.UUID, *, client_ip: str | None = None
) -> PurgeSourceResult:
    """Phase A + a best-effort phase B attempt. Phase A (genuinely atomic, DB-only):
    soft-deletes the Document row, hard-deletes its DocumentChunk rows, purges (never
    hard-deletes) every associated MemorySourceUnit, and writes the `source_purged` audit
    entry — either the whole DB change (including the audit row) commits, or none of it does,
    mirroring app/routers/account.py's delete_account's explicit try/except/rollback
    discipline rather than relying on the request-scoped session's implicit teardown.
    KnowledgeClaim/MemorySourceUnit/lifecycle-event rows all survive, see module docstring.
    `storage.delete()` is NEVER called before phase A's commit — see module docstring for why
    that used to be a real bug.

    Pass 22: the audit write used to happen in the ROUTER, after this function already
    returned and its own commit had already succeeded — record_audit() did a SEPARATE commit
    of its own. A founder review caught the resulting gap: if that second, separate commit
    failed, the HTTP caller got a 500 even though the document was already, durably purged: a
    retry would then 404 ("already deleted") while the founder's client still believes the
    first attempt failed outright. Writing the audit row here, inside phase A's own
    transaction (see the `record_audit(..., commit=False)` call below), makes "the purge
    happened" and "the purge is audited" a single atomic fact — never one without the other.
    `client_ip` is a plain string, not a fastapi.Request — the router extracts it (see
    app/routers/library.py / app/routers/documents.py) so this domain-layer module never
    imports fastapi at all.

    Immediately after phase A commits, this makes ONE best-effort attempt at phase B
    (`retry_source_blob_purge`) — but a phase B failure here is caught, logged, and returned
    via `deletion_status` (`pending`/`failed`), never raised as this call's own failure and
    never a reason to undo phase A. A later, independent `retry_source_blob_purge()` call can
    always finish the job.

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

        # Phase A never calls storage.delete() — nothing here can leave a physically-deleted
        # blob behind a failed/rolled-back commit. A document with no storage_key at all has
        # nothing to purge in phase B either, so it's marked purged immediately (matching
        # maybe_purge_blob's own behavior for storage_key=None); everything else starts phase
        # B as `pending`.
        document.deletion_status = DeletionStatus.purged if document.storage_key is None else DeletionStatus.pending
        db.add(document)
        result.deletion_status = document.deletion_status

        # commit=False: this row joins phase A's single commit below, not a separate one — a
        # failure writing the audit entry rolls back the entire purge along with it, see the
        # docstring's Pass 22 note.
        record_audit(
            db,
            user_id=owner_id,
            action="source_purged",
            entity_type="document",
            entity_id=str(document_id),
            detail=(
                f"sources_purged={result.sources_purged} sources_already_purged={result.sources_already_purged} "
                f"chunks_deleted={result.chunks_deleted} claims_preserved={result.claims_preserved} "
                f"legacy_without_memory_source={result.legacy_without_memory_source} deletion_status={result.deletion_status.value}"
            ),
            ip_address=client_ip,
            commit=False,
        )

        db.commit()
    except SourcePurgeNotFoundError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        logger.exception("Källradering misslyckades för document_id=%s (owner=%s), återställd (rollback).", document_id, owner_id)
        raise

    if result.deletion_status == DeletionStatus.pending:
        try:
            result.deletion_status = retry_source_blob_purge(db, document_id, owner_id)
        except Exception:
            # Phase A already committed successfully — that's the real outcome of this call.
            # A phase B failure here (a DB error committing the status update; storage.delete
            # itself never raises, see maybe_purge_blob) must never be raised as purge_source's
            # own failure, and never triggers any rollback of the already-durable phase A work.
            db.rollback()
            logger.exception(
                "Initial blob-purge attempt misslyckades för document_id=%s (owner=%s) efter en lyckad DB-purge -- "
                "deletion_status kvarstår återförsökbar via retry_source_blob_purge().",
                document_id,
                owner_id,
            )

    return result


def retry_source_blob_purge(db: Session, document_id: uuid.UUID, owner_id: uuid.UUID) -> DeletionStatus:
    """Phase B: the physical blob deletion for a source ALREADY soft-deleted by purge_source()
    (phase A — DB-only, already committed by the time this runs). Idempotent and independently
    retryable, deliberately separate from purge_source() itself (see module docstring): never
    touches memory_source_units/document_chunks/knowledge_claims, which are already
    permanently settled. Safe to call any number of times — LocalFilesystemStorage.delete()
    uses Path.unlink(missing_ok=True), so re-deleting an already-gone file is a no-op, and this
    function's own DB write (`deletion_status`) is a single, independent commit.

    Unlike purge_source(), this accepts a document that's ALREADY soft-deleted
    (`deleted_at IS NOT NULL`) — that's the whole point, it's meant to be called again after an
    earlier purge_source() call's own best-effort phase B attempt left `deletion_status` at
    `pending` or `failed`. Raises SourcePurgeNotFoundError if document_id doesn't resolve to a
    soft-deleted document owned by owner_id (never a live, not-yet-purged one — call
    purge_source() for that).

    Pass 22: acquires app.storage.references.acquire_storage_key_lock() for the document's
    storage_key BEFORE checking whether anything still references it — the same lock POST
    /api/library/import takes around its own blob-finalization + ImportJob-commit sequence,
    closing the TOCTOU race a founder review found (see that module's docstring for the full
    incident this closes).
    """
    document = (
        db.query(Document)
        .filter(Document.id == document_id, Document.uploaded_by == owner_id, Document.deleted_at.isnot(None))
        .with_for_update()
        .first()
    )
    if document is None:
        raise SourcePurgeNotFoundError(
            f"document {document_id} not found, not owned by {owner_id}, or not yet soft-deleted"
        )

    if document.deletion_status == DeletionStatus.purged:
        return document.deletion_status  # idempotent no-op — nothing left to do

    storage = get_storage()
    if document.storage_key is not None:
        # Held for the rest of this transaction (released at the db.commit() below) — see
        # this function's docstring and app/storage/references.py's module docstring for the
        # race this closes.
        acquire_storage_key_lock(db, document.storage_key)
    document.deletion_status = maybe_purge_blob(db, storage, document.storage_key)
    db.add(document)
    db.commit()
    return document.deletion_status
