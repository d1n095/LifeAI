"""Orchestrates one Founder Knowledge Studio import end to end: a single supported document
OR a ZIP package -> app/rag/zip_import.py's security gate -> app/rag/extract.py's text
extraction -> app/rag/ingest.py's existing chunk/embed/store pipeline -> a KnowledgeVersion
snapshot -> ImportJob progress/result tracking. See docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md.

No external worker/queue tonight (see that doc's "Background jobs" section for what a real
one would need and why none was activated) — this runs synchronously inside a FastAPI
BackgroundTasks callback, the same pattern app/routers/documents.py already uses for a single
upload. The ImportJob row itself doesn't assume that, though: a future queue consumer could
pick up a `pending` row and call run_import_job() exactly the way the background task does
today, with no schema change.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.jobs.lock import JobLock, JobLockUnavailable
from app.jobs.retry import compute_backoff_seconds, is_transient_error
from app.models.document import ActiveTruthStatus, Document, DocumentSource, IndexStatus, KnowledgeClassification
from app.request_context import current_user_id as current_user_id_var
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_version import KnowledgeVersion
from app.rag.claims import extract_claims_for_document
from app.rag.extract import extract_text
from app.rag.ingest import index_document
from app.rag import media_import
from app.rag.zip_import import ZipSecurityError, sha256_bytes, validate_and_extract_zip

logger = logging.getLogger("mainai.rag.library_import")

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


@dataclass
class FileOutcome:
    filename: str
    status: str  # "indexed" | "duplicate" | "failed" | "skipped"
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
    """Background tasks get a fresh session that never goes through app/deps.py's
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


async def _import_one_file(
    db: Session,
    owner_id: uuid.UUID,
    filename: str,
    content: bytes,
    checksum: str,
    *,
    project_id: uuid.UUID | None,
    import_job_id: uuid.UUID | None,
    manifest_entry: dict,
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
    text_content: str | None = None
    if media_kind is None:
        try:
            text_content = extract_text(filename, content)
        except Exception as exc:  # noqa: BLE001 - one file's extraction failure must not abort the batch
            return FileOutcome(filename=filename, status="failed", reason=f"Kunde inte extrahera text: {exc}")
    else:
        # STEG 12: MIME/size check happens before anything is written to the database — a
        # rejected media file becomes a per-file FileOutcome, exactly like a text-extraction
        # failure above, never a job-level failure (see app/rag/media_import.py's
        # MediaImportError docstring).
        try:
            media_import.validate_media_bytes(filename, content, media_kind)
        except media_import.MediaImportError as exc:
            return FileOutcome(filename=filename, status="failed", reason=str(exc))

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

    if media_kind is None:
        await index_document(db, document, text_content)
    else:
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


async def run_import_job(
    db: Session,
    job_id: uuid.UUID,
    owner_id: uuid.UUID,
    raw: bytes,
    filename: str,
    *,
    project_id: uuid.UUID | None = None,
) -> None:
    """STEG 11 entry point: coordinates a distributed lock (app/jobs/lock.py) around the
    actual work (_run_once below) and retries transient failures with exponential backoff
    (app/jobs/retry.py) before giving up. Never raises — every terminal outcome (success,
    permanent failure, or transient failure with attempts exhausted) is captured on the job
    row itself, so a caller polling GET /api/library/jobs/{id} always sees a definitive
    status rather than the request hanging or the job stuck at "running" forever.

    Resumability is an emergent property, not new logic: retrying calls _run_once again from
    the top of the same file list, and _import_one_file's existing per-file checksum
    idempotency check means any file already successfully imported on a prior attempt is
    detected as a "duplicate" and skipped, not redone — a retry naturally only does the
    remaining work.
    """
    _set_rls_owner(db, owner_id)
    job = db.get(ImportJob, job_id)
    if job is None:
        return

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
        while True:
            try:
                await _run_once(db, job, owner_id, raw, filename, project_id, lock if lock_held else None)
                return
            except Exception as exc:  # noqa: BLE001 - the job row is the only place this failure can safely surface
                db.rollback()
                _set_rls_owner(db, owner_id)
                job = db.get(ImportJob, job_id)
                if job is None:
                    return
                transient = is_transient_error(exc)
                job.last_failure_transient = transient
                if transient and job.attempt_count + 1 < job.max_attempts:
                    job.attempt_count += 1
                    job.status = ImportJobStatus.pending
                    db.add(job)
                    db.commit()
                    delay = compute_backoff_seconds(job.attempt_count)
                    logger.warning(
                        "Import %s: tillfälligt fel (%s), försök %d/%d om %.1fs.", job_id, exc, job.attempt_count, job.max_attempts, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                job.status = ImportJobStatus.failed
                # ZipSecurityError's own message is already a clear, specific rejection
                # reason (see app/rag/zip_import.py) — wrapping it in a generic "unexpected
                # error" prefix would bury the actual, actionable explanation.
                job.failure_reason = str(exc) if isinstance(exc, ZipSecurityError) else f"Oväntat fel under import: {exc}"
                job.completed_at = datetime.utcnow()
                db.add(job)
                db.commit()
                return
    finally:
        if lock_held:
            lock.release()


async def _run_once(
    db: Session,
    job: ImportJob,
    owner_id: uuid.UUID,
    raw: bytes,
    filename: str,
    project_id: uuid.UUID | None,
    lock: JobLock | None,
) -> None:
    """One attempt at the actual import work — everything run_import_job used to do inline
    before STEG 11 added the retry/lock wrapper around it. Raises on failure (unlike the
    outer function) so run_import_job's retry loop can classify and act on the exception;
    never itself decides retry vs. permanent failure."""
    job.status = ImportJobStatus.running
    job.started_at = datetime.utcnow()
    db.add(job)
    db.commit()

    suffix = PurePosixPath(filename).suffix.lower()
    outcomes: list[FileOutcome] = []

    if suffix == ".zip":
        try:
            zip_result = validate_and_extract_zip(raw)
        except ZipSecurityError as exc:
            # Permanent, not transient (see app/jobs/retry.py's is_transient_error) — a
            # malicious/malformed ZIP will never succeed no matter how many times it's
            # retried, so this is raised, not written directly as a terminal failure here;
            # run_import_job's retry loop classifies it and skips straight to "failed".
            raise

        job.manifest = zip_result.manifest
        entries = zip_result.entries
        job.progress_total = len(entries)
        db.add(job)
        db.commit()

        for entry in entries:
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
                    owner_id,
                    entry.filename,
                    entry.content,
                    entry.checksum,
                    project_id=project_id,
                    import_job_id=job.id,
                    manifest_entry=manifest_entry,
                )
                outcomes.append(outcome)
            job.progress_current = len(outcomes)
            job.file_results = [o.__dict__ for o in outcomes]
            db.add(job)
            db.commit()
            # Heartbeat: renews the lease so a large package doesn't outlive its lock while
            # still genuinely being worked on. A failed renewal (lease already expired,
            # possibly reacquired by another worker under an abandoned-job assumption) is
            # logged, not fatal — the import keeps going rather than aborting mid-batch over
            # a coordination signal, since the actual data-safety guarantee here is
            # per-file/per-document idempotency (checksums), not the lock itself.
            if lock is not None and not lock.renew():
                logger.warning("Import %s: kunde inte förnya jobblåset — kan ha övertagits som övergivet.", job.id)
    else:
        job.progress_total = 1
        db.add(job)
        db.commit()
        checksum = sha256_bytes(raw)
        outcome = await _import_one_file(
            db, owner_id, filename, raw, checksum, project_id=project_id, import_job_id=job.id, manifest_entry={}
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

    job.succeeded_count = succeeded
    job.failed_count = failed
    job.skipped_count = skipped + duplicates
    if failed and (succeeded or duplicates or skipped):
        job.status = ImportJobStatus.partial
    elif failed and not (succeeded or duplicates or skipped):
        job.status = ImportJobStatus.failed
        job.failure_reason = "Alla filer i paketet misslyckades."
    else:
        job.status = ImportJobStatus.completed
    job.completed_at = datetime.utcnow()
    db.add(job)
    db.commit()
