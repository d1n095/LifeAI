import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.db import get_db
from app.deps import require_founder
from app.library.import_uploads import EmptyUploadError, receive_import_upload
from app.models.document import Document, DocumentSource, IndexStatus
from app.models.import_job import ImportJob
from app.models.user import User
from app.schemas import DocumentOut
from app.storage.base import StorageError, StorageSizeLimitExceeded
from app.storage.purge import SourcePurgeNotFoundError, purge_source

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
def upload_document(
    request: Request,
    file: UploadFile,
    category: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    """Persist an owner-bound upload intent before staging or publishing its original."""
    def create_document(job: ImportJob, _blob: object) -> None:
        document = Document(
            title=file.filename or "upload",
            source=DocumentSource.upload,
            category=category,
            uploaded_by=user.id,
            original_filename=file.filename,
            checksum=job.source_checksum,
            media_type=file.content_type,
            storage_key=job.source_storage_key,
            size_bytes=job.source_size_bytes,
            import_job_id=job.id,
            status=IndexStatus.original_stored,
            stored_at=datetime.utcnow(),
        )
        db.add(document)
        db.flush()
        record_audit(
            db,
            user_id=user.id,
            action="document_upload",
            entity_type="document",
            entity_id=str(document.id),
            request=request,
            commit=False,
        )

    try:
        result = receive_import_upload(
            db,
            owner_id=user.id,
            source_filename=file.filename,
            media_type=file.content_type,
            read_chunk=lambda: file.file.read(1 << 20),
            max_bytes=MAX_UPLOAD_BYTES,
            deduplicate_active=True,
            finalize=create_document,
        )
        if result.deduplicated:
            existing_document = (
                db.query(Document)
                .filter(
                    Document.import_job_id == result.job.id,
                    Document.uploaded_by == user.id,
                    Document.deleted_at.is_(None),
                )
                .order_by(Document.created_at.asc())
                .first()
            )
            if existing_document is not None:
                return existing_document
            raise HTTPException(
                status_code=503,
                detail="Uppladdningen bearbetas fortfarande. Försök igen med samma fil.",
                headers={"Retry-After": "3"},
            )
        document = db.query(Document).filter_by(import_job_id=result.job.id, uploaded_by=user.id).first()
        return document
    except StorageSizeLimitExceeded:
        raise HTTPException(status_code=413, detail="Filen är för stor (max 25 MB).")
    except EmptyUploadError:
        raise HTTPException(status_code=400, detail="Filen är tom.")
    except OperationalError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Tjänsten är tillfälligt otillgänglig. Försök igen med samma fil.",
            headers={"Retry-After": "3"},
        ) from exc
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail="Lagringen är tillfälligt otillgänglig. Försök igen med samma fil.",
            headers={"Retry-After": "3"},
        ) from exc
    except StorageError as exc:
        raise HTTPException(status_code=500, detail="Kunde inte lagra filen säkert.") from exc


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
