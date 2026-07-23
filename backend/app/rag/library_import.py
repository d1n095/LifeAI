"""Orchestrates one Founder Knowledge Studio import end to end: a single supported document
OR a ZIP package -> app/rag/zip_import.py's security gate -> app/rag/extract.py's text
extraction -> app/rag/ingest.py's existing chunk/embed/store pipeline -> a KnowledgeVersion
snapshot -> ImportJob progress/result tracking. See docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md.

Life Library durable-worker package: the raw uploaded package is no longer passed around in
memory — POST /api/library/import (app/routers/library.py) streams it straight to durable
storage (app/storage/) before this module ever runs, and records where at
ImportJob.source_storage_key. run_import_job() opens that file itself. This module is called
from app/worker.py's poll loop now, not a FastAPI BackgroundTask — a crash between here and a
job reaching a terminal status simply leaves it `pending`/`running` with an expired lease,
and any worker (the same one restarted, or a different replica) picks it back up exactly like
a fresh claim (see app/worker.py's claim_next_job).
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.jobs.lease import renew_lease
from app.jobs.lock import JobLock, JobLockUnavailable
from app.models.document import ActiveTruthStatus, DeletionStatus, Document, DocumentSource, IndexStatus, KnowledgeClassification
from app.request_context import current_user_id as current_user_id_var
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_version import KnowledgeVersion
from app.rag.claims import extract_claims_for_document
from app.rag.extract import extract_text
from app.rag.ingest import index_document
from app.rag import media_import
from app.rag.zip_import import ZipSecurityError, sha256_bytes, validate_and_extract_zip
from app.storage import StorageBackend, StorageError, get_storage

logger = logging.getLogger("mainai.rag.library_import")

WORKER_LEASE_SECONDS = get_settings().worker_lease_seconds

EXTRACTION_VERSION = "extract-v1"  # bump when chunking/extraction logic changes meaningfully

MEDIA_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".json": "application/json",
    ".html": "text/html",
    ".htm": "text/html",
    # STEG 12: audio/video (see app/rag/media_import.py) — dispatched to a different
    # pipeline in _import_one_file below, but the MIME type it gets labeled with in the
    # Document row comes from this same lookup table as everything else.
    **media_import.MEDIA_TYPES,
}

VALID_CLASSIFICATIONS = {c.value for c in KnowledgeClassification}
VALID_TRUTH_STATUSES = {s.value for s in ActiveTruthStatus}


class JobCancelled(Exception):
    """Raised internally to unwind out of per-file processing the instant a cancellation is
    observed — never escapes run_import_job (caught there and treated as a clean, expected
    stop, not a failure to retry)."""


@dataclass
class FileOutcome:
    filename: str
    status: str  # "indexed" | "duplicate" | "failed" | "skipped" | "cancelled"
    reason: str | None = None
    source_id: str | None = None


def _manifest_entry_for(manifest: dict | None, filename: str) -> dict:
    """Looks up per-file metadata from a `{"documents": [{"file": ..., ...}, ...]}`-shaped
    manifest (the FKP-like package format DEL 2 asks to support) by filename or basename.
    Returns {} if there's no manifest, or no entry for this specific file — the import still
    proceeds with defaults, since the manifest is enrichment, not a requirement."""
    if not manifest or not isinstance(manifest.get("documents"), list):
        return {}
    base = PurePosixPath(filename).name
    for entry in manifest["documents"]:
        if not isinstance(entry, dict):
            continue
        if entry.get("file") in (filename, base):
            return entry
    return {}


def _set_rls_owner(db: Session, owner_id: uuid.UUID) -> None:
    """The worker gets a fresh session that never goes through app/deps.py's
    get_current_user (see app/rag/ingest.py's identical requirement). This whole orchestrator
    makes several separate db.commit() calls as it goes (progress updates), each of which
    ends the current transaction — app/db.py's after_begin listener re-applies SET LOCAL on
    every *new* transaction, but only by reading the current_user_id contextvar, so setting
    the raw session variable alone (as app/rag/ingest.py's single-shot caller does) only
    covers the first transaction. Setting the contextvar too — exactly what get_current_user
    does for a real request — is what makes every later commit's next transaction correctly
    re-scoped without this function needing to be called again before each one."""
    current_user_id_var.set(str(owner_id))
    db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _job_is_cancelled(db: Session, job_id: uuid.UUID) -> bool:
    """A cheap, dedicated status-only read (not a full ORM load) — called between every
    per-file step and before every major pipeline transition, so a delete-triggered
    cancellation (app/routers/library.py's delete_source) is noticed promptly rather than
    only at the next natural commit boundary."""
    row = db.execute(text("SELECT status FROM knowledge_import_jobs WHERE id = :id"), {"id": str(job_id)}).first()
    return row is not None and row[0] == ImportJobStatus.cancelled.value


def _store_bytes(storage: StorageBackend, content: bytes, *, max_bytes: int):
    """Adapts a plain in-memory bytes value (already validated/extracted from a ZIP entry,
    or the whole single-file upload) to StorageBackend.write_stream's pull-based
    read_chunk() interface — chunked, not because `content` isn't already fully in memory
    here (the worker's own documented tradeoff, see this module's top docstring), but so the
    write itself still goes through the exact same streaming-hash-and-atomic-write path a
    truly-streamed source would."""
    chunk_size = 1 << 20
    pos = 0

    def _read():
        nonlocal pos
        chunk = content[pos : pos + chunk_size]
        pos += len(chunk)
        return chunk

    return storage.write_stream(_read, max_bytes=max_bytes)


def maybe_purge_blob(db: Session, storage: StorageBackend, storage_key: str | None) -> DeletionStatus:
    """Life Library durable-worker package (DEL 5): physically removes a blob ONLY when no
    live (non-deleted) Document still points at it — content-addressing (see
    app/storage/local_fs.py) means two different documents with byte-identical content
    already share one physical file, so this reference count is a live DB query, not a
    maintained counter that could drift. Returns the DeletionStatus to persist on the
    caller's Document row: `purged` (removed, or nothing to remove), `pending` (still
    referenced elsewhere — correct to leave the file in place), `failed` (a real I/O error,
    surfaced distinctly rather than silently retried forever)."""
    if storage_key is None:
        return DeletionStatus.purged
    still_referenced = (
        db.query(Document.id).filter(Document.storage_key == storage_key, Document.deleted_at.is_(None)).first() is not None
    )
    if still_referenced:
        return DeletionStatus.pending
    try:
        storage.delete(storage_key)
        return DeletionStatus.purged
    except StorageError:
        logger.exception("Kunde inte radera blob %s fysiskt.", storage_key)
        return DeletionStatus.failed


async def _import_one_file(
    db: Session,
    storage: StorageBackend,
    owner_id: uuid.UUID,
    job_id: uuid.UUID,
    filename: str,
    content: bytes,
    checksum: str,
    *,
    project_id: uuid.UUID | None,
    import_job_id: uuid.UUID | None,
    manifest_entry: dict,
    max_upload_bytes: int,
) -> FileOutcome:
    # Idempotency at the file level: identical content (by checksum) already owned by this
    # user is never re-imported as a second copy — DEL 2's "vara idempotent vid samma
    # checksumma" and DEL 4's duplicate-marking both point at this same check.
    existing = (
        db.query(Document)
        .filter(Document.uploaded_by == owner_id, Document.checksum == checksum, Document.deleted_at.is_(None))
        .first()
    )
    if existing is not None:
        return FileOutcome(filename=filename, status="duplicate", reason="Identiskt innehåll finns redan.", source_id=str(existing.id))

    suffix = PurePosixPath(filename).suffix.lower()
    media_kind = media_import.media_kind_for(filename)

    classification_raw = manifest_entry.get("classification")
    classification = classification_raw if classification_raw in VALID_CLASSIFICATIONS else KnowledgeClassification.general.value
    truth_status_raw = manifest_entry.get("active_truth_status")
    truth_status = truth_status_raw if truth_status_raw in VALID_TRUTH_STATUSES else ActiveTruthStatus.active.value

    declared_checksum = manifest_entry.get("checksum")
    if declared_checksum and declared_checksum != checksum:
        return FileOutcome(
            filename=filename,
            status="failed",
            reason="Manifestets deklarerade checksumma matchar inte filens faktiska innehåll.",
        )

    # The Document row exists (status `received`) BEFORE the original is durably stored or
    # extraction is attempted — a failure at ANY later step is recorded ON this row instead
    # of losing the received upload entirely. See IndexStatus's docstring for the full
    # granular state list this function walks through.
    document = Document(
        title=manifest_entry.get("title") or filename,
        source=DocumentSource.zip_import if import_job_id else DocumentSource.upload,
        category=manifest_entry.get("category"),
        uploaded_by=owner_id,
        checksum=checksum,
        media_type=MEDIA_TYPES.get(suffix, "application/octet-stream"),
        original_filename=filename,
        classification=classification,
        active_truth_status=truth_status,
        project_id=project_id,
        import_job_id=import_job_id,
        imported_at=datetime.utcnow(),
        status=IndexStatus.received,
        # STEG 13: media_blob is kept for now (the in-DB copy the player reads back from
        # today) — storage_key below is the SEPARATE, durable-on-disk copy every document
        # gets, superseding media_blob as the actual durability guarantee; not removing
        # media_blob yet is a deliberate no-behavior-change choice for this package.
        media_blob=content if media_kind is not None else None,
    )
    db.add(document)
    db.commit()

    version = KnowledgeVersion(
        source_id=document.id,
        owner_id=owner_id,
        version_number=1,
        checksum=checksum,
        extraction_version=EXTRACTION_VERSION,
        raw_metadata={"original_filename": filename, "size_bytes": len(content), "media_type": document.media_type},
    )
    db.add(version)
    db.commit()

    if _job_is_cancelled(db, job_id):
        document.status = IndexStatus.cancelled
        db.add(document)
        db.commit()
        return FileOutcome(filename=filename, status="cancelled", reason="Importen avbröts.", source_id=str(document.id))

    # DEL 1 (persistent original storage): write, fsync, verify — see
    # app/storage/local_fs.py's write_stream. Only once this succeeds does the document
    # advance past `original_storing`; a storage failure here is recorded as a normal
    # `failed` outcome, exactly like an extraction failure, never a silently-lost upload.
    document.status = IndexStatus.original_storing
    db.add(document)
    db.commit()
    try:
        blob = _store_bytes(storage, content, max_bytes=max_upload_bytes)
    except StorageError as exc:
        document.status = IndexStatus.failed
        document.error_message = f"Kunde inte lagra originalfilen: {exc}"
        db.add(document)
        db.commit()
        return FileOutcome(filename=filename, status="failed", reason=document.error_message, source_id=str(document.id))

    document.storage_key = blob.storage_key
    document.size_bytes = blob.size_bytes
    document.stored_at = datetime.utcnow()
    document.status = IndexStatus.original_stored
    db.add(document)
    db.commit()

    if _job_is_cancelled(db, job_id):
        document.status = IndexStatus.cancelled
        db.add(document)
        db.commit()
        return FileOutcome(filename=filename, status="cancelled", reason="Importen avbröts.", source_id=str(document.id))

    if media_kind is None:
        document.status = IndexStatus.extracting
        db.add(document)
        db.commit()
        try:
            text_content = extract_text(filename, content)
        except Exception as exc:  # noqa: BLE001 - one file's extraction failure must not abort the batch
            document.status = IndexStatus.failed
            document.error_message = f"Kunde inte extrahera text: {exc}"
            db.add(document)
            db.commit()
            return FileOutcome(
                filename=filename, status="failed", reason=document.error_message, source_id=str(document.id)
            )
        document.status = IndexStatus.extracted
        db.add(document)
        db.commit()
        await index_document(db, document, text_content)
    else:
        # STEG 12: MIME/size check — a rejected media file becomes a per-file FileOutcome,
        # exactly like a text-extraction failure above, never a job-level failure (see
        # app/rag/media_import.py's MediaImportError docstring).
        try:
            media_import.validate_media_bytes(filename, content, media_kind)
        except media_import.MediaImportError as exc:
            document.status = IndexStatus.failed
            document.error_message = str(exc)
            db.add(document)
            db.commit()
            return FileOutcome(filename=filename, status="failed", reason=str(exc), source_id=str(document.id))
        await media_import.index_media_document(db, document, content, filename, media_kind)

    if document.status == IndexStatus.failed:
        return FileOutcome(filename=filename, status="failed", reason=document.error_message, source_id=str(document.id))

    # Claim extraction (STEG 10, app/rag/claims.py) runs after indexing succeeds, on the
    # chunks index_document just created. A failure here must never turn a successfully
    # indexed, searchable document into a "failed" import outcome — claims are an
    # enrichment layer on top of the already-complete indexing result, not a precondition
    # for it, so any exception is swallowed the same way a single chunk's extraction
    # failure already is inside extract_claims_for_document itself.
    try:
        await extract_claims_for_document(db, document, owner_id, version.id)
    except Exception:  # noqa: BLE001 - claim extraction is best-effort, indexing already succeeded
        pass

    return FileOutcome(filename=filename, status="indexed", source_id=str(document.id))


async def run_import_job(db: Session, job_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    """Restart-safe worker entry point (DEL 3): one attempt at the actual import work for a
    job app/worker.py's claim_next_job() has already atomically marked `running` under a
    Postgres lease (`FOR UPDATE SKIP LOCKED`) — that claim is what guarantees no two workers
    ever process the SAME job row concurrently. This function additionally acquires the
    existing STEG 11 Redis JobLock, scoped by (owner, content checksum) rather than job id —
    a DIFFERENT, narrower race the Postgres claim alone doesn't cover: two separate ImportJob
    rows created from two near-simultaneous uploads of byte-identical content could each be
    claimed by a different worker at the same time, and without this lock both could pass
    _import_one_file's duplicate-checksum check before either commits its new Document row.

    Never raises for a normal per-file failure (those become FileOutcome entries); DOES
    raise for a genuine, unexpected orchestration-level error. Unlike the old synchronous-
    BackgroundTask design, this function itself does NOT retry — app/worker.py's caller owns
    the retry/backoff loop (app/jobs/retry.py, reused unchanged), since the worker is what
    now decides whether to keep this job claimed for a local retry or let it go back to
    `pending` for any worker to reclaim on the next poll.
    """
    _set_rls_owner(db, owner_id)
    job = db.get(ImportJob, job_id)
    if job is None:
        return
    if job.source_storage_key is None:
        raise ValueError("Jobbet saknar en lagrad originalfil att bearbeta.")

    lock_key = f"import:{owner_id}:{job.source_checksum or job_id}"
    lock = JobLock(lock_key, lease_seconds=60)
    # None = "couldn't even check" (Redis unreachable) — proceed without coordination rather
    # than blocking every import whenever Redis happens to be down (see app/limiter.py's
    # identical in-memory-fallback philosophy for rate limiting). False = another worker
    # genuinely holds this exact (owner, content) lock right now — a real, expected outcome
    # of concurrency, not a degraded-Redis situation.
    lock_held: bool | None
    try:
        lock_held = lock.acquire()
    except JobLockUnavailable as exc:
        logger.warning("Jobblås otillgängligt (%s) — fortsätter utan distribuerad koordinering.", exc)
        lock_held = None

    if lock_held is False:
        job.status = ImportJobStatus.failed
        job.failure_reason = "En identisk import pågår redan i en annan process (jobblås upptaget)."
        job.completed_at = datetime.utcnow()
        db.add(job)
        db.commit()
        return

    try:
        await _run_once(db, job, owner_id, job_id)
    finally:
        if lock_held:
            lock.release()


async def _run_once(db: Session, job: ImportJob, owner_id: uuid.UUID, job_id: uuid.UUID) -> None:
    """The actual import work for one attempt — opens the durably-stored original from
    app/storage/ and runs it through the same zip/single-file pipeline as before. Raises on
    an unexpected orchestration-level error (never on a per-file failure, which becomes a
    FileOutcome instead) so app/worker.py's retry wrapper can classify and act on it."""
    storage = get_storage()
    try:
        with storage.open_read(job.source_storage_key) as f:
            raw = f.read()
    except (FileNotFoundError, StorageError) as exc:
        job.status = ImportJobStatus.failed
        job.failure_reason = f"Originalfilen kunde inte läsas från lagringen: {exc}"
        job.completed_at = datetime.utcnow()
        db.add(job)
        db.commit()
        return

    job.status = ImportJobStatus.running
    job.started_at = job.started_at or datetime.utcnow()
    db.add(job)
    db.commit()

    filename = job.source_filename or "upload"
    suffix = PurePosixPath(filename).suffix.lower()
    outcomes: list[FileOutcome] = []

    if suffix == ".zip":
        zip_result = validate_and_extract_zip(raw)  # ZipSecurityError propagates — permanent, see app/worker.py
        job.manifest = zip_result.manifest
        entries = zip_result.entries
        job.progress_total = len(entries)
        db.add(job)
        db.commit()

        for entry in entries:
            if _job_is_cancelled(db, job_id):
                outcomes.append(FileOutcome(filename=entry.filename, status="cancelled", reason="Importen avbröts."))
                job.progress_current = len(outcomes)
                job.file_results = [o.__dict__ for o in outcomes]
                db.add(job)
                db.commit()
                continue
            # manifest.json describes the package (see zip_import.py's own parsing of
            # it into zip_result.manifest, already applied above) — it is package
            # metadata, not a knowledge source in its own right, so it's never imported
            # as a Document even though it passes the same .json allow-list check every
            # other JSON file in the package does.
            if entry.status == "ok" and PurePosixPath(entry.filename).name.lower() == "manifest.json":
                outcomes.append(
                    FileOutcome(filename=entry.filename, status="skipped", reason="Manifestfil, importeras inte som ett eget dokument.")
                )
            elif entry.status != "ok":
                outcomes.append(FileOutcome(filename=entry.filename, status="skipped", reason=entry.reason))
            else:
                manifest_entry = _manifest_entry_for(zip_result.manifest, entry.filename)
                outcome = await _import_one_file(
                    db,
                    storage,
                    owner_id,
                    job_id,
                    entry.filename,
                    entry.content,
                    entry.checksum,
                    project_id=job.project_id,
                    import_job_id=job.id,
                    manifest_entry=manifest_entry,
                    max_upload_bytes=len(raw) or 1,
                )
                outcomes.append(outcome)
            job.progress_current = len(outcomes)
            job.file_results = [o.__dict__ for o in outcomes]
            db.add(job)
            db.commit()
            # Heartbeat: renews the Postgres lease so a large package doesn't outlive its
            # claim while still genuinely being worked on — see app/jobs/lease.py's
            # renew_lease docstring. A failed renewal (locked_by no longer matches — the
            # lease already expired and another worker reclaimed this job as abandoned) is
            # logged, not fatal: the import keeps going rather than aborting mid-batch over
            # a coordination signal, matching the same "per-file idempotency is the real
            # safety net, not the lock itself" philosophy STEG 11 already established.
            if job.locked_by and not renew_lease(db, job_id, job.locked_by, WORKER_LEASE_SECONDS):
                logger.warning("Import %s: kunde inte förnya jobbleasen — kan ha övertagits som övergivet.", job_id)
    else:
        job.progress_total = 1
        db.add(job)
        db.commit()
        if _job_is_cancelled(db, job_id):
            outcomes.append(FileOutcome(filename=filename, status="cancelled", reason="Importen avbröts."))
        else:
            checksum = sha256_bytes(raw)
            outcome = await _import_one_file(
                db,
                storage,
                owner_id,
                job_id,
                filename,
                raw,
                checksum,
                project_id=job.project_id,
                import_job_id=job.id,
                manifest_entry={},
                max_upload_bytes=len(raw) or 1,
            )
            outcomes.append(outcome)
        job.progress_current = 1
        job.file_results = [o.__dict__ for o in outcomes]
        db.add(job)
        db.commit()

    succeeded = sum(1 for o in outcomes if o.status == "indexed")
    duplicates = sum(1 for o in outcomes if o.status == "duplicate")
    failed = sum(1 for o in outcomes if o.status == "failed")
    skipped = sum(1 for o in outcomes if o.status == "skipped")
    cancelled = sum(1 for o in outcomes if o.status == "cancelled")

    job.succeeded_count = succeeded
    job.failed_count = failed
    job.skipped_count = skipped + duplicates

    if cancelled and not succeeded:
        job.status = ImportJobStatus.cancelled
    elif failed and (succeeded or duplicates or skipped):
        job.status = ImportJobStatus.partial
    elif failed and not (succeeded or duplicates or skipped):
        job.status = ImportJobStatus.failed
        job.failure_reason = "Alla filer i paketet misslyckades."
    else:
        job.status = ImportJobStatus.completed
    job.completed_at = datetime.utcnow()
    db.add(job)
    db.commit()
