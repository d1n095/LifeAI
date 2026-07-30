"""Canonical blob-reference policy (Pass 22): the ONE place that decides whether a
content-addressed storage_key is still needed by anything in the system, and the ONE place
that serializes a check-then-act sequence against it. Shared by app/routers/library.py's
upload-finalization path and app/rag/source_purge.py's retry_source_blob_purge() (phase B).

Why this had to become a shared module rather than staying app/rag/library_import.py's
`maybe_purge_blob()`-local inline query: that query only ever looked at live
`Document.storage_key` rows. It never looked at `ImportJob.source_storage_key` -- the RAW
uploaded package (a single file or a ZIP) that POST /api/library/import streams straight to
durable storage BEFORE any Document row exists, which app/worker.py's poll loop later opens
itself to do the actual extraction/indexing (see app/rag/library_import.py's module
docstring). Content-addressing means a byte-identical raw upload and a byte-identical
already-imported Document share the exact same storage_key -- so an older document being
purged could physically delete a blob a newer, still-pending import was relying on to even
begin.

Which ImportJob statuses actually still need the raw blob is read directly off
app/worker.py's real resumption paths, not guessed at:
  - `pending`/`running` -- the ordinary in-flight path; run_import_job() (library_import.py)
    opens job.source_storage_key itself on every claim, including every retry.
  - `blocked` -- `_requeue_blocked_jobs` flips this back to `pending` in bulk once the active
    embedding provider verifies ok again; no re-upload.
  - `partial` with `blocked_count > 0` -- the exact same requeue query matches this too (see
    that function's own comment on the 2026-07-28 incident where a job rolled up to `partial`
    instead of `blocked`).
  - ANY status at all, including a fully terminal `completed`/`partial`/`failed` -- if the job
    still has a live (non-deleted) Document sitting in
    app.models.document.RESUMABLE_INDEX_STATUSES, `_reconcile_orphaned_documents` resets that
    job back to `pending` and resumption needs the SAME raw original again, with no re-upload.
    This is the one non-obvious case: a single ZIP job can produce many Documents, so purging
    ONE already-purged sibling document must never destroy the blob a DIFFERENT, still-stuck
    sibling document from the exact same job still needs.

A job that's `cancelled`, or `completed`/`partial`/`failed` with no remaining live Document in
a resumable status, matches none of the above -- its blob reference is correctly treated as
expired, exactly like an already-purged Document's is.

Locking: `acquire_storage_key_lock()` is a Postgres advisory lock scoped to the CALLING
session's current transaction (`pg_advisory_xact_lock` -- released automatically at that
transaction's next commit or rollback, never leaked across a crash the way a Redis lease with
a TTL could be). It exists to close a real TOCTOU race a founder review caught: POST
/api/library/import's storage.write_stream() call durably writes a blob to disk BEFORE any DB
row references it (content-addressing means the key isn't even known until the bytes are
fully hashed, so it's structurally impossible to lock the key any earlier than that). Without
a shared lock, a concurrent retry_source_blob_purge() could run its own reference check in
that exact window, see nothing pointing at the just-written key yet, and physically delete it
before the new ImportJob row is created -- leaving that row committed with a
source_storage_key pointing at nothing. Both call sites take the SAME lock (same hashed key)
before doing their own check-then-act sequence, so whichever side gets there first fully
commits or rolls back before the other's check can even run.
"""

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.models.document import RESUMABLE_INDEX_STATUSES, Document
from app.models.import_job import ImportJob, ImportJobStatus

# Statuses where the job's OWN status already means "still runnable" -- no need to look at
# its documents at all. `blocked` is included here (not just partial+blocked_count) because a
# job that never had ANY successes/failures yet -- 100% blocked -- rolls up to `blocked`
# itself, not `partial` (see library_import.py's status-rollup branch).
_ALWAYS_RUNNABLE_STATUSES = (ImportJobStatus.pending, ImportJobStatus.running, ImportJobStatus.blocked)


def acquire_storage_key_lock(db: Session, storage_key: str) -> None:
    """Blocks until this session holds the advisory lock for `storage_key`, released
    automatically when the CURRENT transaction commits or rolls back -- never call this and
    then hold the session open indefinitely without committing/rolling back soon after, or a
    concurrent holder blocks for that entire time. hashtextextended's 64-bit output makes an
    accidental collision between two different storage_keys astronomically unlikely; even if
    one ever happened, the effect is only unnecessary serialization between two unrelated
    keys, never an incorrect result."""
    db.execute(sa_text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": storage_key})


def storage_key_still_referenced(db: Session, storage_key: str) -> bool:
    """True if `storage_key` is still needed by ANYTHING -- a live Document.storage_key row,
    or an ImportJob.source_storage_key the worker could still read from (see module
    docstring). Callers must hold acquire_storage_key_lock(db, storage_key) for the duration
    of their own check-then-act sequence around this; this function itself does no locking."""
    if (
        db.query(Document.id).filter(Document.storage_key == storage_key, Document.deleted_at.is_(None)).first()
        is not None
    ):
        return True

    jobs = db.query(ImportJob).filter(ImportJob.source_storage_key == storage_key).all()
    for job in jobs:
        if job.status in _ALWAYS_RUNNABLE_STATUSES:
            return True
        if job.status == ImportJobStatus.partial and job.blocked_count > 0:
            return True
        # Terminal-looking but still resurrectable: _reconcile_orphaned_documents (app/
        # worker.py) resets THIS job back to pending if any of its OWN live documents is
        # still stuck mid-pipeline -- checked directly against the real condition it uses,
        # not inferred from the job's status alone.
        stuck_document_exists = (
            db.query(Document.id)
            .filter(
                Document.import_job_id == job.id,
                Document.deleted_at.is_(None),
                Document.status.in_(RESUMABLE_INDEX_STATUSES),
            )
            .first()
            is not None
        )
        if stuck_document_exists:
            return True

    return False
