"""Bounded recovery and cleanup for durable, owner-scoped import upload stages."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.library.import_uploads import _existing_resumable_job
from app.models.import_job import ImportJob, ImportJobStatus
from app.storage import get_storage
from app.storage.base import StorageError, StorageIntegrityError
from app.storage.references import acquire_owner_erasure_lock, acquire_storage_key_lock

logger = logging.getLogger("mainai.library.staging")

_MAX_RECOVERY_ATTEMPTS = 5
_RECOVERY_LEASE_SECONDS = 120
_MAX_STAGES_CLEANED_PER_TICK = 2
_UPLOAD_MAX_BYTES = 60 * 1024 * 1024


def _expected_stage_key(owner_id: uuid.UUID, job_id: uuid.UUID) -> str:
    return f"staging/{owner_id}/{job_id}"


def _claim_expired_upload(db: Session) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID] | None:
    candidate = db.execute(
        text("""
            SELECT id, owner_id
            FROM knowledge_import_jobs
            WHERE status = 'receiving'
              AND upload_lease_expires_at <= clock_timestamp()
              AND (staging_reconcile_after IS NULL OR staging_reconcile_after <= clock_timestamp())
            ORDER BY upload_lease_expires_at ASC, id ASC
            LIMIT 1
        """)
    ).first()
    if candidate is None:
        db.commit()
        return None

    job_id, owner_id = candidate
    acquire_owner_erasure_lock(db, owner_id)
    job = (
        db.query(ImportJob)
        .filter(
            ImportJob.id == job_id,
            ImportJob.status == ImportJobStatus.receiving,
            ImportJob.upload_lease_expires_at <= datetime.utcnow(),
        )
        .with_for_update()
        .first()
    )
    if job is None:
        db.commit()
        return None

    if job.staging_reconcile_count >= _MAX_RECOVERY_ATTEMPTS:
        job.status = ImportJobStatus.failed
        job.failure_reason = "Upload recovery exhausted; staged original retained for safe retry or review."
        job.completed_at = datetime.utcnow()
        job.upload_lease_token = None
        job.upload_lease_expires_at = None
        job.staging_reconcile_after = None
        record_audit(
            db,
            user_id=job.owner_id,
            action="library_import_recovery_exhausted",
            entity_type="import_job",
            entity_id=str(job.id),
            detail="staged original retained after bounded recovery attempts",
            commit=False,
        )
        db.commit()
        return None

    token = uuid.uuid4()
    job.upload_lease_token = token
    job.upload_lease_expires_at = datetime.utcnow() + timedelta(seconds=_RECOVERY_LEASE_SECONDS)
    job.staging_reconcile_count += 1
    db.commit()
    return job_id, owner_id, token


def _record_recovery_failure(
    db: Session,
    job_id: uuid.UUID,
    owner_id: uuid.UUID,
    token: uuid.UUID,
    error: StorageError,
) -> None:
    db.rollback()
    acquire_owner_erasure_lock(db, owner_id)
    job = (
        db.query(ImportJob)
        .filter(
            ImportJob.id == job_id,
            ImportJob.owner_id == owner_id,
            ImportJob.status == ImportJobStatus.receiving,
            ImportJob.upload_lease_token == token,
        )
        .with_for_update()
        .first()
    )
    if job is None:
        db.rollback()
        return

    if isinstance(error, StorageIntegrityError) or job.staging_reconcile_count >= _MAX_RECOVERY_ATTEMPTS:
        job.status = ImportJobStatus.failed
        job.failure_reason = "Staged original failed integrity verification; bytes were retained."
        job.completed_at = datetime.utcnow()
        job.upload_lease_token = None
        job.upload_lease_expires_at = None
        job.staging_reconcile_after = None
        record_audit(
            db,
            user_id=owner_id,
            action="library_import_recovery_failed",
            entity_type="import_job",
            entity_id=str(job.id),
            detail="staged original retained after failed verification or bounded retries",
            commit=False,
        )
    else:
        backoff_seconds = min(30 * (2 ** (job.staging_reconcile_count - 1)), 1800)
        next_attempt = datetime.utcnow() + timedelta(seconds=backoff_seconds)
        job.upload_lease_expires_at = next_attempt
        job.staging_reconcile_after = next_attempt
    db.commit()


def _recover_claimed_upload(
    db: Session,
    job_id: uuid.UUID,
    owner_id: uuid.UUID,
    token: uuid.UUID,
) -> bool:
    acquire_owner_erasure_lock(db, owner_id)
    job = (
        db.query(ImportJob)
        .filter(
            ImportJob.id == job_id,
            ImportJob.owner_id == owner_id,
            ImportJob.status == ImportJobStatus.receiving,
            ImportJob.upload_lease_token == token,
        )
        .with_for_update()
        .first()
    )
    if job is None:
        db.rollback()
        return False

    expected_stage = _expected_stage_key(owner_id, job_id)
    if job.staging_storage_key != expected_stage:
        job.status = ImportJobStatus.failed
        job.failure_reason = "Upload recovery identity did not match its owner and job."
        job.completed_at = datetime.utcnow()
        job.upload_lease_token = None
        job.upload_lease_expires_at = None
        record_audit(
            db,
            user_id=owner_id,
            action="library_import_recovery_failed",
            entity_type="import_job",
            entity_id=str(job.id),
            detail="staged object identity mismatch; no storage mutation performed",
            commit=False,
        )
        db.commit()
        return False

    storage = get_storage()
    try:
        staged = storage.inspect_staged(expected_stage)
        if staged is None:
            storage.delete_staged(expected_stage)
            job.status = ImportJobStatus.failed
            job.failure_reason = "Upload ended before a complete staged original was published."
            job.completed_at = datetime.utcnow()
            job.staging_storage_key = None
            job.upload_lease_token = None
            job.upload_lease_expires_at = None
            job.staging_reconcile_after = None
            record_audit(
                db,
                user_id=owner_id,
                action="library_import_recovery_missing",
                entity_type="import_job",
                entity_id=str(job.id),
                detail="no complete staged original existed after the upload lease expired",
                commit=False,
            )
            db.commit()
            return False
        if staged.size_bytes <= 0 or staged.size_bytes > _UPLOAD_MAX_BYTES:
            raise StorageIntegrityError("Staged original size is outside the accepted upload bounds.")

        existing = _existing_resumable_job(
            db, owner_id, staged.sha256, storage, include_active=True
        )
        if existing is not None:
            storage.delete_staged(expected_stage)
            job.staging_storage_key = None
            db.delete(job)
            record_audit(
                db,
                user_id=owner_id,
                action="library_import_duplicate_recovered",
                entity_type="import_job",
                entity_id=str(existing.id),
                detail="expired staged upload matched an existing verified same-owner import",
                commit=False,
            )
            db.commit()
            return False

        canonical_key = f"{staged.sha256[:2]}/{staged.sha256}"
        acquire_storage_key_lock(db, canonical_key)
        with storage.open_staged_read(expected_stage) as staged_file:
            canonical = storage.write_stream(lambda: staged_file.read(1 << 20), max_bytes=_UPLOAD_MAX_BYTES)
        if (
            canonical.storage_key != canonical_key
            or canonical.sha256 != staged.sha256
            or canonical.size_bytes != staged.size_bytes
        ):
            raise StorageIntegrityError("Canonical promotion differs from the verified staged original.")

        if not storage.verify(canonical.storage_key, expected_sha256=staged.sha256, expected_size=staged.size_bytes):
            raise StorageIntegrityError("Canonical original failed post-promotion verification.")

        job.source_checksum = staged.sha256
        job.source_size_bytes = staged.size_bytes
        job.source_storage_key = canonical.storage_key
        job.status = ImportJobStatus.pending
        job.failure_reason = None
        job.upload_lease_token = None
        job.upload_lease_expires_at = None
        job.staging_reconcile_after = None
        record_audit(
            db,
            user_id=owner_id,
            action="library_import_recovered",
            entity_type="import_job",
            entity_id=str(job.id),
            detail="owner-scoped staged original verified and promoted to canonical storage",
            commit=False,
        )
        db.commit()
        return True
    except (StorageError, OSError) as exc:
        storage_error = exc if isinstance(exc, StorageError) else StorageError("Staged storage is temporarily unavailable.")
        _record_recovery_failure(db, job_id, owner_id, token, storage_error)
        logger.warning("Staged import recovery failed for job %s (%s).", job_id, type(exc).__name__)
        return False


def recover_expired_import_uploads(db: Session) -> int:
    """Reconcile at most one expired receiving intent per worker poll, with lease fencing
    and bounded retries. The caller supplies the cross-owner claim session."""
    claimed = _claim_expired_upload(db)
    if claimed is None:
        return 0
    return int(_recover_claimed_upload(db, *claimed))


def cleanup_import_stages(db: Session) -> int:
    """Remove at most two redundant stages after their canonical original is independently
    verified. Missing stage files can be cleared; corrupt or ambiguous stage data is retained."""
    candidates = (
        db.query(ImportJob.id, ImportJob.owner_id)
        .filter(
            ImportJob.staging_storage_key.isnot(None),
            ImportJob.source_storage_key.isnot(None),
            ImportJob.status != ImportJobStatus.receiving,
            (ImportJob.staging_reconcile_after.is_(None))
            | (ImportJob.staging_reconcile_after <= datetime.utcnow()),
        )
        .order_by(ImportJob.updated_at.asc(), ImportJob.id.asc())
        .limit(_MAX_STAGES_CLEANED_PER_TICK)
        .all()
    )
    cleaned = 0
    for job_id, owner_id in candidates:
        acquire_owner_erasure_lock(db, owner_id)
        job = (
            db.query(ImportJob)
            .filter(
                ImportJob.id == job_id,
                ImportJob.owner_id == owner_id,
                ImportJob.staging_storage_key.isnot(None),
                ImportJob.source_storage_key.isnot(None),
                ImportJob.status != ImportJobStatus.receiving,
            )
            .with_for_update()
            .first()
        )
        if job is None:
            db.commit()
            continue

        if job.staging_storage_key != _expected_stage_key(owner_id, job_id):
            logger.error(
                "Staged upload identity mismatch for job %s during cleanup; retaining its pointer.",
                job_id,
            )
            record_audit(
                db,
                user_id=owner_id,
                action="library_import_stage_identity_mismatch",
                entity_type="import_job",
                entity_id=str(job.id),
                detail="staged object identity mismatch during cleanup; no storage mutation performed",
                commit=False,
            )
            db.commit()
            continue

        storage = get_storage()
        try:
            staged = storage.inspect_staged(job.staging_storage_key)
            canonical_key = job.source_storage_key
            checksum = job.source_checksum
            size_bytes = job.source_size_bytes
            if canonical_key is None or checksum is None or size_bytes is None:
                db.rollback()
                continue

            acquire_storage_key_lock(db, canonical_key)
            canonical_valid = storage.verify(
                canonical_key, expected_sha256=checksum, expected_size=size_bytes
            )
            stage_valid = staged is not None and staged.sha256 == checksum and staged.size_bytes == size_bytes
            if staged is not None and stage_valid and not canonical_valid:
                with storage.open_staged_read(job.staging_storage_key) as staged_file:
                    promoted = storage.write_stream(lambda: staged_file.read(1 << 20), max_bytes=_UPLOAD_MAX_BYTES)
                if promoted.storage_key != canonical_key or promoted.sha256 != checksum or promoted.size_bytes != size_bytes:
                    raise StorageIntegrityError("Staged source does not match the canonical import reference.")
                canonical_valid = storage.verify(canonical_key, expected_sha256=checksum, expected_size=size_bytes)
            if staged is not None and not stage_valid and not canonical_valid:
                raise StorageIntegrityError("Neither the staged nor canonical original is independently verified.")
            if not canonical_valid:
                raise StorageError("Canonical original could not be verified; staged bytes were retained.")

            storage.delete_staged(job.staging_storage_key)
            job.staging_storage_key = None
            job.staging_reconcile_after = None
            db.commit()
            cleaned += 1
        except (StorageError, OSError):
            db.rollback()
            acquire_owner_erasure_lock(db, owner_id)
            retry_job = (
                db.query(ImportJob)
                .filter(
                    ImportJob.id == job_id,
                    ImportJob.owner_id == owner_id,
                    ImportJob.staging_storage_key.isnot(None),
                )
                .with_for_update()
                .first()
            )
            if retry_job is not None:
                retry_job.staging_reconcile_count += 1
                retry_job.staging_reconcile_after = datetime.utcnow() + timedelta(
                    seconds=min(30 * (2 ** min(retry_job.staging_reconcile_count - 1, 6)), 1800)
                )
                db.commit()
            else:
                db.rollback()
            logger.exception("Could not reconcile staged upload for job %s; retaining its durable pointer.", job_id)
    return cleaned
