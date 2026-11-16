"""Durable, owner-scoped upload intents for the existing ImportJob pipeline."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.models.document import Document
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.user import User
from app.storage import get_storage
from app.storage.base import StorageBackend, StorageError, StorageSizeLimitExceeded
from app.storage.references import (
    acquire_owner_erasure_lock,
    acquire_storage_key_lock,
    delete_if_unreferenced,
    retain_pending_rejected_upload_cleanup_tasks,
)

UPLOAD_INTENT_LEASE_SECONDS = 300


class EmptyUploadError(ValueError):
    pass


class UploadIntentLostError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImportUploadResult:
    job: ImportJob
    deduplicated: bool


def _staging_key(owner_id: uuid.UUID, job_id: uuid.UUID) -> str:
    return f"staging/{owner_id}/{job_id}"


def _job_clock_is_live(db: Session, job: ImportJob, upload_token: uuid.UUID) -> bool:
    row = db.execute(
        text("""
            SELECT owner_id, status, upload_lease_token, upload_lease_expires_at >= clock_timestamp()
            FROM knowledge_import_jobs
            WHERE id = :job_id
            FOR UPDATE
        """),
        {"job_id": str(job.id)},
    ).first()
    return bool(
        row
        and str(row.owner_id) == str(job.owner_id)
        and row.status == ImportJobStatus.receiving.value
        and row.upload_lease_token == upload_token
        and row[3]
    )


def _existing_resumable_job(
    db: Session,
    owner_id: uuid.UUID,
    checksum: str,
    storage: StorageBackend,
    *,
    include_active: bool,
):
    active = (
        db.query(ImportJob)
        .filter(
            ImportJob.owner_id == owner_id,
            ImportJob.source_checksum == checksum,
            (
                ImportJob.status.in_(
                    (ImportJobStatus.pending, ImportJobStatus.running, ImportJobStatus.blocked)
                )
                | ((ImportJob.status == ImportJobStatus.partial) & (ImportJob.blocked_count > 0))
            ),
        )
        .order_by(ImportJob.created_at.desc())
        .first()
    ) if include_active else None
    if active and active.source_storage_key and storage.verify(
        active.source_storage_key, expected_sha256=checksum, expected_size=active.source_size_bytes
    ):
        return active

    completed = (
        db.query(ImportJob)
        .filter_by(owner_id=owner_id, source_checksum=checksum, status=ImportJobStatus.completed)
        .order_by(ImportJob.completed_at.desc())
        .first()
    )
    if completed and db.query(Document.id).filter(
        Document.import_job_id == completed.id, Document.deleted_at.is_(None)
    ).first():
        return completed
    return None


def receive_import_upload(
    db: Session,
    *,
    owner_id: uuid.UUID,
    source_filename: str | None,
    media_type: str | None,
    read_chunk: Callable[[], bytes],
    max_bytes: int,
    project_id: uuid.UUID | None = None,
    deduplicate: bool = True,
    deduplicate_active: bool = True,
    purge_empty_blob: bool = False,
    finalize: Callable[[ImportJob, object], None] | None = None,
) -> ImportUploadResult:
    """Reserve durable ownership before staging, then verify and atomically attach the
    canonical original to that same ImportJob.

    The transaction holding the owner-erasure lock spans staging and final publication.
    If PostgreSQL disappears, its row/advisory locks are released by the server, but the
    already-committed receiving row still identifies the owner and staging key for recovery.
    Canonical bytes are never published before that durable intent exists.
    """
    acquire_owner_erasure_lock(db, owner_id)
    if db.query(User.id).filter_by(id=owner_id).first() is None:
        raise UploadIntentLostError("Owner no longer exists.")

    first_chunk = read_chunk()
    if not first_chunk:
        if purge_empty_blob:
            delete_if_unreferenced(
                db,
                get_storage(),
                "e3/e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            )
        db.rollback()
        raise EmptyUploadError("Filen är tom.")

    pending_chunk = first_chunk

    def _read_with_initial_chunk() -> bytes:
        nonlocal pending_chunk
        if pending_chunk:
            chunk = pending_chunk
            pending_chunk = b""
            return chunk
        return read_chunk()

    job_id = uuid.uuid4()
    upload_token = uuid.uuid4()
    staging_key = _staging_key(owner_id, job_id)
    now = datetime.utcnow()
    job = ImportJob(
        id=job_id,
        owner_id=owner_id,
        project_id=project_id,
        status=ImportJobStatus.receiving,
        source_filename=source_filename,
        source_media_type=media_type,
        staging_storage_key=staging_key,
        upload_lease_token=upload_token,
        upload_lease_expires_at=now + timedelta(seconds=UPLOAD_INTENT_LEASE_SECONDS),
    )
    db.add(job)
    record_audit(
        db,
        user_id=owner_id,
        action="library_import_reserved",
        entity_type="import_job",
        entity_id=str(job_id),
        detail="owner-scoped durable upload intent created",
        commit=False,
    )
    db.commit()

    storage = get_storage()
    acquire_owner_erasure_lock(db, owner_id)
    if not _job_clock_is_live(db, job, upload_token):
        db.rollback()
        raise UploadIntentLostError("Upload intent lease is no longer live.")

    try:
        staged = storage.write_staged_stream(staging_key, _read_with_initial_chunk, max_bytes=max_bytes)
    except StorageSizeLimitExceeded:
        job.status = ImportJobStatus.failed
        job.failure_reason = "Uppladdningen överskred storleksgränsen."
        job.completed_at = datetime.utcnow()
        job.staging_storage_key = None
        job.upload_lease_token = None
        job.upload_lease_expires_at = None
        db.commit()
        raise
    except StorageError:
        db.rollback()
        raise

    if not storage.verify_staged(staging_key, expected_sha256=staged.sha256, expected_size=staged.size_bytes):
        db.rollback()
        raise StorageError("Staged original failed hash/size verification.")

    # Serialize identical bytes across owners with the same canonical-key lock. The durable
    # owner/job staging records remain distinct; only their verified immutable blob is shared.
    acquire_storage_key_lock(db, f"{staged.sha256[:2]}/{staged.sha256}")
    if deduplicate:
        existing = _existing_resumable_job(
            db, owner_id, staged.sha256, storage, include_active=deduplicate_active
        )
        if existing is not None:
            storage.delete_staged(staging_key)
            job.staging_storage_key = None
            db.delete(job)
            record_audit(
                db,
                user_id=owner_id,
                action="library_import_duplicate_reused",
                entity_type="import_job",
                entity_id=str(existing.id),
                detail="same-owner upload matched an existing verified import",
                commit=False,
            )
            db.commit()
            return ImportUploadResult(existing, deduplicated=True)

    with storage.open_staged_read(staging_key) as staged_file:
        canonical = storage.write_stream(
            lambda: staged_file.read(1 << 20),
            max_bytes=max_bytes,
        )
    if canonical.sha256 != staged.sha256 or canonical.size_bytes != staged.size_bytes:
        db.rollback()
        raise StorageError("Canonical promotion did not match the durable staged original.")
    if not storage.verify(
        canonical.storage_key,
        expected_sha256=staged.sha256,
        expected_size=staged.size_bytes,
    ):
        db.rollback()
        raise StorageError("Canonical original failed post-promotion verification.")

    job.source_checksum = canonical.sha256
    job.source_size_bytes = canonical.size_bytes
    job.source_storage_key = canonical.storage_key
    job.status = ImportJobStatus.pending
    job.progress_total = 0
    job.failure_reason = None
    job.upload_lease_token = None
    job.upload_lease_expires_at = None
    if finalize is not None:
        finalize(job, canonical)
    record_audit(
        db,
        user_id=owner_id,
        action="library_import_canonicalized",
        entity_type="import_job",
        entity_id=str(job_id),
        detail="verified staged original promoted to canonical storage",
        commit=False,
    )
    db.commit()
    db.refresh(job)
    retain_pending_rejected_upload_cleanup_tasks(canonical.storage_key)
    return ImportUploadResult(job, deduplicated=False)
