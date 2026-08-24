import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.db import get_db
from app.deps import require_founder
from app.models.document import Document, DocumentSource, IndexStatus
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.user import User
from app.schemas import DocumentOut
from app.storage import get_storage
from app.storage.base import StorageError, StorageSizeLimitExceeded
from app.storage.purge import SourcePurgeNotFoundError, purge_source
from app.storage.references import (
    acquire_owner_erasure_lock,
    acquire_storage_key_lock,
    delete_if_unreferenced,
    retain_pending_rejected_upload_cleanup_tasks,
)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB

router = APIRouter(prefix="/api/documents", tags=["documents"], dependencies=[Depends(require_founder)])


@router.get("", response_model=list[DocumentOut])
def list_documents(db: Session = Depends(get_db)):
    # Document now has a soft-delete flag (deleted_at — see migration 0006 and
    # app/routers/library.py's delete_source): a source removed through the newer Founder
    # Knowledge Studio Library UI must not keep reappearing here, in the older /documents
    # view of the same underlying table. Found as a real bug (not assumed) — an E2E test
    # deleting a source via /library still saw it listed via /documents.
    return db.query(Document).filter(Document.deleted_at.is_(None)).order_by(Document.created_at.desc()).all()


@router.post("/upload", response_model=DocumentOut)
async def upload_document(
    request: Request,
    file: UploadFile,
    category: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Legacy `/api/documents/upload` durability fix (runtime clock sweep).

    PREVIOUSLY: HTTP success committed a Document row then handed extracted text to FastAPI
    `BackgroundTasks`. Process death after the response left a permanent metadata tombstone
    with no blob, no ImportJob, and no worker clock able to resume indexing
    (`#126 FIXED OWNER CONTEXT != DURABLE DELIVERY`).

    NOW: thin adapter onto the Library durable path — stream bytes to `app/storage/`, commit
    `ImportJob(status=pending)` + a linked Document with `storage_key`/`checksum`/
    `import_job_id`, then return `DocumentOut`. `app/worker.py`'s existing claim loop does
    indexing. No second queue. No in-process BackgroundTasks.
    """
    # Same owner-erasure lock ordering as /api/library/import — must precede any durable
    # write so account erasure cannot inventory past a not-yet-referenced blob.
    acquire_owner_erasure_lock(db, user.id)
    if db.query(User).filter_by(id=user.id).first() is None:
        raise HTTPException(status_code=401, detail="Kontot finns inte längre.")

    storage = get_storage()

    # Fail closed on empty before any durable write — avoids content-addressed empty-blob
    # collisions and the delete_if_unreferenced path for a never-needed key.
    peek = file.file.read(1)
    if not peek:
        raise HTTPException(status_code=400, detail="Filen är tom.")
    try:
        file.file.seek(0)
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"Kunde inte läsa filen: {exc}") from exc

    def _read_chunk() -> bytes:
        return file.file.read(1 << 20)

    try:
        blob = await run_in_threadpool(storage.write_stream, _read_chunk, max_bytes=MAX_UPLOAD_BYTES)
    except StorageSizeLimitExceeded:
        raise HTTPException(status_code=413, detail="Filen är för stor (max 25 MB).")
    except StorageError as exc:
        raise HTTPException(status_code=500, detail=f"Kunde inte lagra filen: {exc}")

    if blob.size_bytes == 0:
        delete_if_unreferenced(db, storage, blob.storage_key)
        db.rollback()
        raise HTTPException(status_code=400, detail="Filen är tom.")

    checksum = blob.sha256

    # Whole-upload idempotency (same contract as library import): byte-identical completed
    # upload with a still-live Document returns that Document instead of a new job.
    existing_job = (
        db.query(ImportJob)
        .filter_by(owner_id=user.id, source_checksum=checksum, status=ImportJobStatus.completed)
        .order_by(ImportJob.completed_at.desc())
        .first()
    )
    if existing_job is not None:
        existing_doc = (
            db.query(Document)
            .filter(
                Document.import_job_id == existing_job.id,
                Document.deleted_at.is_(None),
                Document.uploaded_by == user.id,
            )
            .order_by(Document.created_at.desc())
            .first()
        )
        if existing_doc is not None:
            return existing_doc

    acquire_storage_key_lock(db, blob.storage_key)
    if not storage.exists(blob.storage_key):
        raise HTTPException(
            status_code=409,
            detail="Uppladdningen kolliderade med en samtidig radering av en identisk fil. Försök igen.",
        )

    job = ImportJob(
        owner_id=user.id,
        status=ImportJobStatus.pending,
        source_filename=file.filename,
        source_checksum=checksum,
        source_storage_key=blob.storage_key,
        source_size_bytes=blob.size_bytes,
        source_media_type=file.content_type,
    )
    db.add(job)
    db.flush()

    # Pre-create the Document so DocumentOut stays stable and the worker's checksum
    # resume path (_resume_incomplete_document) reuses THIS row instead of inventing a
    # second one. Status is resumable (`original_stored`) so a crash before the worker
    # claim still has a durable wake via ImportJob.pending + reconcile/resume.
    document = Document(
        title=file.filename or "upload",
        source=DocumentSource.upload,
        category=category,
        uploaded_by=user.id,
        original_filename=file.filename,
        checksum=checksum,
        media_type=file.content_type,
        storage_key=blob.storage_key,
        size_bytes=blob.size_bytes,
        import_job_id=job.id,
        status=IndexStatus.original_stored,
        stored_at=datetime.utcnow(),
    )
    db.add(document)
    db.commit()
    # Retain only AFTER the ImportJob/Document reference is durably COMMITTED — never while
    # it is merely flushed inside the still-open transaction. retain_pending_* runs on its own
    # `_MaintenanceSession` and commits independently, so a retain issued pre-commit survives a
    # rollback/process death that the reference row does not: the cleanup task would sit in the
    # terminal `retained_shared` state guarding a blob nothing references, and nothing in the
    # system would ever purge it again. Same ordering as _import_one_file()'s durable path and
    # what retain_pending_rejected_upload_cleanup_tasks()'s own docstring requires (#133).
    # Dropping the storage-key lock at commit is safe: from that instant the reference check
    # in storage_key_still_referenced_global() sees the committed rows and protects the blob.
    retain_pending_rejected_upload_cleanup_tasks(blob.storage_key)
    db.refresh(document)

    record_audit(
        db,
        user_id=user.id,
        action="document_upload",
        entity_type="document",
        entity_id=str(document.id),
        request=request,
    )
    return document


@router.delete("/{document_id}")
def delete_document(document_id: uuid.UUID, request: Request, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    """Thin wrapper delegating to the shared app/storage/purge.py::purge_source() service
    — see that module's docstring. INTENTIONAL behavior change from the old hard `db.delete
    (document)` (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8's "En gemensam purge-tjänst"):
    migration 0019's `document_source_units.document_id` FK (no `ON DELETE` action) would
    otherwise RESTRICT-block a hard delete outright once any memory_source_units row exists
    for this document, and the old implementation's own previous docstring already flagged an
    unresolved multi-uploader chunk-deletion gap besides (the old code's `db.get(Document,
    document_id)` never even checked `uploaded_by`). This route now gets the same soft-delete
    + blob-purge + memory-purge behavior `/api/library` already had, including
    `purge_source()`'s own explicit ownership check (defense in depth alongside `documents`'
    RLS policy, not a replacement for it — see app/rls.py) — not a second, diverging
    implementation. As of Pass 22, the source_purged audit entry is written INSIDE
    purge_source()'s own atomic transaction, not by this router — see that module's
    docstring."""
    client_ip = request.client.host if request.client else None
    try:
        purge_source(db, document_id, user.id, client_ip=client_ip)
    except SourcePurgeNotFoundError:
        raise HTTPException(status_code=404, detail="Dokumentet hittades inte.")

    return {"status": "deleted"}
