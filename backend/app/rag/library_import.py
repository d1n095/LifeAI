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
from app.models.document import (
    ActiveTruthStatus,
    DeletionStatus,
    Document,
    DocumentSource,
    IndexStatus,
    KnowledgeClassification,
    RESUMABLE_INDEX_STATUSES,
)
from app.request_context import current_user_id as current_user_id_var
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_version import KnowledgeVersion
from app.rag.blob_references import storage_key_still_referenced
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

# 2026-07-28: mirrors app/rag/context_status.py's _FAILED_STATUSES — a Document already in
# one of these when _import_one_file sees it again (e.g. a resumed `partial` job) is a real,
# still-unresolved failure, not a duplicate. See _import_one_file's existing-document branch.
_FAILED_INDEX_STATUSES = (
    IndexStatus.failed,
    IndexStatus.storage_failed,
    IndexStatus.extraction_failed,
    IndexStatus.indexing_failed,
)


class JobCancelled(Exception):
    """Raised internally to unwind out of per-file processing the instant a cancellation is
    observed — never escapes run_import_job (caught there and treated as a clean, expected
    stop, not a failure to retry)."""


@dataclass
class FileOutcome:
    filename: str
    status: str  # "indexed" | "duplicate" | "failed" | "skipped" | "cancelled" | "blocked" | "encrypted"
    reason: str | None = None
    source_id: str | None = None
    # P2: only set for a file that came from inside a nested ZIP archive — mirrors
    # ZipEntryResult.archive_path/archive_chain (see app/rag/zip_import.py), carried through
    # unchanged so ImportJob.file_results retains the same provenance the extraction step
    # already computed. None for a top-level file, exactly like before P2.
    archive_path: str | None = None
    archive_chain: list[dict] | None = None


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
    """Life Library durable-worker package (DEL 5): physically removes a blob ONLY when
    nothing still needs it — content-addressing (see app/storage/local_fs.py) means two
    different documents (or a pending ImportJob's raw upload) with byte-identical content
    already share one physical file, so the reference check is a live DB query, not a
    maintained counter that could drift. Pass 22: the reference check itself now lives in
    app/rag/blob_references.py::storage_key_still_referenced() — a live Document.storage_key
    row was always checked here, but a pending/running/resumable ImportJob.source_storage_key
    was not, which let a source purge physically delete a blob a not-yet-processed import job
    still needed (see that module's docstring for the full incident and the fix). Returns the
    DeletionStatus to persist on the caller's Document row: `purged` (removed, or nothing to
    remove), `pending` (still referenced elsewhere — correct to leave the file in place),
    `failed` (a real I/O error, surfaced distinctly rather than silently retried forever).
    Callers with a real storage_key must hold app.rag.blob_references.acquire_storage_key_lock
    for this call — this function itself does no locking."""
    if storage_key is None:
        return DeletionStatus.purged
    if storage_key_still_referenced(db, storage_key):
        return DeletionStatus.pending
    try:
        storage.delete(storage_key)
        return DeletionStatus.purged
    except StorageError:
        logger.exception("Kunde inte radera blob %s fysiskt.", storage_key)
        return DeletionStatus.failed


async def _resume_incomplete_document(
    db: Session,
    document: Document,
    owner_id: uuid.UUID,
    filename: str,
    content: bytes,
    *,
    archive_path: str | None = None,
    archive_chain: list[dict] | None = None,
) -> FileOutcome:
    """P1, widened 2026-07-28: re-attempts extraction+embedding for a Document that is NOT
    in a terminal state — reuses the SAME row and its existing KnowledgeVersion rather than
    creating a duplicate. Two distinct ways to land here, both driving the exact same code:
      - `awaiting_provider`/`blocked_provider` (the original P1 case): a real pause, resumed
        once app/worker.py's _requeue_blocked_jobs sees the provider verify ok again.
      - Any status in app/models/document.py's RESUMABLE_INDEX_STATUSES (extracting,
        embedding, etc.): the worker PROCESS itself died mid-step with no exception for
        Python to catch, leaving the row stuck there — reached either when its ImportJob's
        lease expires and gets reclaimed, or via app/worker.py's _reconcile_orphaned_documents
        for a job that had already (wrongly) reached a terminal status. Restarting the whole
        extraction+embedding pipeline from scratch is deliberately simple and safe here: we
        don't know which exact sub-step the crash interrupted, and re-extraction is cheap and
        local (no AI involved) regardless.
    Reached whenever a file with a matching checksum is seen again — never needs the ORIGINAL
    http upload again: `content` here always comes from re-reading the job's own durably
    stored original (ImportJob.source_storage_key, see run_import_job/_run_once). The
    original bytes are never re-stored — the document is already `original_stored` (or
    further along), and app/storage/'s content-addressing would just no-op a re-write
    anyway.

    2026-07-28, second correction: RESUMABLE_INDEX_STATUSES applies to every Document
    regardless of type — an audio/video file (media_import.py's pipeline) can crash while
    stuck at `extracting`/`embedding` exactly like a text document can, but it does NOT go
    through extract_text()/index_document() on a first import (see _import_one_file's own
    `if media_kind is None: ... else: ...` split) — it goes through
    media_import.validate_media_bytes() + media_import.index_media_document() instead, which
    transcribes and builds timestamped chunks rather than extracting/embedding plain text.
    Resuming a crash-stuck MP3/MP4 through the text pipeline would misclassify it as
    extraction_failed (extract_text() cannot parse binary media). This function now mirrors
    _import_one_file's own dispatch on media_import.media_kind_for(filename) so a resumed
    media document goes through the SAME pipeline a first import would have."""
    version = (
        db.query(KnowledgeVersion)
        .filter(KnowledgeVersion.source_id == document.id)
        .order_by(KnowledgeVersion.version_number.desc())
        .first()
    )

    media_kind = media_import.media_kind_for(filename)
    if media_kind is None:
        document.status = IndexStatus.extracting
        db.add(document)
        db.commit()
        try:
            text_content = extract_text(filename, content)
        except Exception as exc:  # noqa: BLE001 - same per-file-failure handling as a first attempt
            document.status = IndexStatus.extraction_failed
            document.error_message = f"Kunde inte extrahera text: {exc}"
            db.add(document)
            db.commit()
            return FileOutcome(
                filename=filename,
                status="failed",
                reason=document.error_message,
                source_id=str(document.id),
                archive_path=archive_path,
                archive_chain=archive_chain,
            )
        document.status = IndexStatus.extracted
        db.add(document)
        db.commit()
        await index_document(db, document, text_content)
    else:
        try:
            media_import.validate_media_bytes(filename, content, media_kind)
        except media_import.MediaImportError as exc:
            document.status = IndexStatus.failed
            document.error_message = str(exc)
            db.add(document)
            db.commit()
            return FileOutcome(
                filename=filename,
                status="failed",
                reason=str(exc),
                source_id=str(document.id),
                archive_path=archive_path,
                archive_chain=archive_chain,
            )
        await media_import.index_media_document(db, document, content, filename, media_kind)

    if document.status in (IndexStatus.awaiting_provider, IndexStatus.blocked_provider):
        return FileOutcome(
            filename=filename,
            status="blocked",
            reason=document.error_message,
            source_id=str(document.id),
            archive_path=archive_path,
            archive_chain=archive_chain,
        )
    if document.status != IndexStatus.indexed:
        return FileOutcome(
            filename=filename,
            status="failed",
            reason=document.error_message,
            source_id=str(document.id),
            archive_path=archive_path,
            archive_chain=archive_chain,
        )

    if version is not None:
        try:
            await extract_claims_for_document(db, document, owner_id, version.id)
        except Exception:  # noqa: BLE001 - claim extraction is best-effort, indexing already succeeded
            # S1A dual-write (app/rag/claims.py) flushes MemorySourceUnit/DocumentSourceUnit
            # rows before it commits — an exception partway through (a later chunk's provider
            # error surfacing unexpectedly, an identity conflict) can leave those flushed-but-
            # uncommitted writes sitting in this session. Without rollback here, a LATER
            # db.commit() elsewhere in this same worker session could commit them anyway,
            # even though extraction was reported as failed — or, if that later commit itself
            # hits a DB error, leave the session stuck in PendingRollback and break the rest
            # of the job. db.rollback() only undoes this function's own uncommitted work
            # (document.status/indexing were already committed above), so "indexed" below is
            # still accurate.
            db.rollback()
            logger.exception(
                "Claim-extraktion misslyckades för dokument %s (owner=%s, version=%s) — "
                "session rullades tillbaka, indexering kvarstår som lyckad.",
                document.id,
                owner_id,
                version.id,
            )

    return FileOutcome(
        filename=filename,
        status="indexed",
        source_id=str(document.id),
        archive_path=archive_path,
        archive_chain=archive_chain,
    )


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
    archive_path: str | None = None,
    archive_chain: list[dict] | None = None,
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
        # P1: a row already paused on the provider is NOT a duplicate — resume it in place
        # instead of creating a second Document/KnowledgeVersion (see
        # _resume_incomplete_document's docstring).
        if existing.status in (IndexStatus.awaiting_provider, IndexStatus.blocked_provider):
            return await _resume_incomplete_document(
                db, existing, owner_id, filename, content, archive_path=archive_path, archive_chain=archive_chain
            )
        # 2026-07-28, second incident: a document left at any OTHER non-terminal status
        # (embedding, extracting, original_stored, ...) got there because the worker PROCESS
        # itself died mid-step — no exception was ever raised, so nothing set a terminal
        # status. Falling through to "duplicate" below would leave it stuck at that status
        # forever even though the job it belongs to can still reach `completed` — the exact
        # confirmed production incident (MAINAI_CONTEXT_BUNDLE.md stuck at `embedding`, job
        # showing "Klar", queue empty, nothing left to ever revisit it). Resuming re-attempts
        # the whole extraction+embedding pipeline on the SAME row from the durably stored
        # original — see _resume_incomplete_document's docstring for why restarting from
        # scratch is safe here regardless of which exact step the crash interrupted.
        if existing.status in RESUMABLE_INDEX_STATUSES:
            return await _resume_incomplete_document(
                db, existing, owner_id, filename, content, archive_path=archive_path, archive_chain=archive_chain
            )
        # 2026-07-28 incident: a real, still-unresolved failure must keep reporting as
        # "failed" on a resumed pass (e.g. app/worker.py's _requeue_blocked_jobs reprocessing
        # a `partial` job to unstick its blocked files) — falling through to the generic
        # "duplicate" outcome below would silently erase it from THIS pass's failed_count,
        # since _run_once's summary (see that module) recomputes every count from scratch on
        # each attempt rather than merging across attempts. A job with real failures could
        # then flip straight to `completed` once its blocked files finally succeed, with the
        # original failures never surfaced again anywhere. This never re-attempts
        # extraction/embedding — the failure is already durably recorded on the Document row
        # — it only reports it honestly again instead of masking it as a duplicate.
        if existing.status in _FAILED_INDEX_STATUSES:
            return FileOutcome(
                filename=filename,
                status="failed",
                reason=existing.error_message,
                source_id=str(existing.id),
                archive_path=archive_path,
                archive_chain=archive_chain,
            )
        # Every other existing status (indexed) is a real duplicate, unchanged from before P1.
        return FileOutcome(
            filename=filename,
            status="duplicate",
            reason="Identiskt innehåll finns redan.",
            source_id=str(existing.id),
            archive_path=archive_path,
            archive_chain=archive_chain,
        )

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
            archive_path=archive_path,
            archive_chain=archive_chain,
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

    raw_metadata = {"original_filename": filename, "size_bytes": len(content), "media_type": document.media_type}
    # P2: provenance for a file that came from inside a nested ZIP — see
    # docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §10.6. Omitted entirely (not just None) for a
    # top-level file, unchanged from before P2.
    if archive_path is not None:
        raw_metadata["archive_path"] = archive_path
        raw_metadata["archive_chain"] = archive_chain
    version = KnowledgeVersion(
        source_id=document.id,
        owner_id=owner_id,
        version_number=1,
        checksum=checksum,
        extraction_version=EXTRACTION_VERSION,
        raw_metadata=raw_metadata,
    )
    db.add(version)
    db.commit()

    if _job_is_cancelled(db, job_id):
        document.status = IndexStatus.cancelled
        db.add(document)
        db.commit()
        return FileOutcome(
            filename=filename,
            status="cancelled",
            reason="Importen avbröts.",
            source_id=str(document.id),
            archive_path=archive_path,
            archive_chain=archive_chain,
        )

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
        # P1: reclassified from the old undifferentiated `failed` — the original never made
        # it into durable storage at all, so unlike awaiting_provider/blocked_provider this
        # is terminal: there is nothing safely stored to resume from, a fresh upload is
        # required (see docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.7).
        document.status = IndexStatus.storage_failed
        document.error_message = f"Kunde inte lagra originalfilen: {exc}"
        db.add(document)
        db.commit()
        return FileOutcome(
            filename=filename,
            status="failed",
            reason=document.error_message,
            source_id=str(document.id),
            archive_path=archive_path,
            archive_chain=archive_chain,
        )

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
        return FileOutcome(
            filename=filename,
            status="cancelled",
            reason="Importen avbröts.",
            source_id=str(document.id),
            archive_path=archive_path,
            archive_chain=archive_chain,
        )

    if media_kind is None:
        document.status = IndexStatus.extracting
        db.add(document)
        db.commit()
        try:
            text_content = extract_text(filename, content)
        except Exception as exc:  # noqa: BLE001 - one file's extraction failure must not abort the batch
            # P1: reclassified from the old undifferentiated `failed` — the file's CONTENT is
            # the problem here, not the provider or storage, so this is terminal too (see
            # docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.7).
            document.status = IndexStatus.extraction_failed
            document.error_message = f"Kunde inte extrahera text: {exc}"
            db.add(document)
            db.commit()
            return FileOutcome(
                filename=filename,
                status="failed",
                reason=document.error_message,
                source_id=str(document.id),
                archive_path=archive_path,
                archive_chain=archive_chain,
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
            # MediaImportError is raised only by our own app/rag/media_import.py validation
            # code with a deliberate, pre-written message — never wraps an httpx/provider
            # exception, so str(exc) here can never contain a URL, header or API key (unlike
            # the embedding call site in app/rag/ingest.py — see
            # app/providers/verification.py's classify_provider_exception docstring).
            document.error_message = str(exc)
            db.add(document)
            db.commit()
            return FileOutcome(
                filename=filename,
                status="failed",
                reason=str(exc),
                source_id=str(document.id),
                archive_path=archive_path,
                archive_chain=archive_chain,
            )
        await media_import.index_media_document(db, document, content, filename, media_kind)

    # P1: awaiting_provider/blocked_provider is a PAUSE, not a failure — the file is safely
    # stored and extracted, just waiting on the AI provider, and app/worker.py's automatic
    # requeue will retry it with no re-upload. Every other non-`indexed` status here
    # (storage_failed/extraction_failed/indexing_failed, or the legacy generic `failed`) is a
    # genuine, terminal per-file failure.
    if document.status in (IndexStatus.awaiting_provider, IndexStatus.blocked_provider):
        return FileOutcome(
            filename=filename,
            status="blocked",
            reason=document.error_message,
            source_id=str(document.id),
            archive_path=archive_path,
            archive_chain=archive_chain,
        )
    if document.status != IndexStatus.indexed:
        return FileOutcome(
            filename=filename,
            status="failed",
            reason=document.error_message,
            source_id=str(document.id),
            archive_path=archive_path,
            archive_chain=archive_chain,
        )

    # Claim extraction (STEG 10, app/rag/claims.py) runs after indexing succeeds, on the
    # chunks index_document just created. A failure here must never turn a successfully
    # indexed, searchable document into a "failed" import outcome — claims are an
    # enrichment layer on top of the already-complete indexing result, not a precondition
    # for it, so any exception is swallowed the same way a single chunk's extraction
    # failure already is inside extract_claims_for_document itself.
    try:
        await extract_claims_for_document(db, document, owner_id, version.id)
    except Exception:  # noqa: BLE001 - claim extraction is best-effort, indexing already succeeded
        # See _resume_incomplete_document's matching except block: dual-write flushes
        # MSU/DSU rows before committing, so an exception here can leave uncommitted writes
        # in the session that a later commit could accidentally persist, or that could leave
        # the session in PendingRollback on a DB-level failure. Roll back before continuing.
        db.rollback()
        logger.exception(
            "Claim-extraktion misslyckades för dokument %s (owner=%s, version=%s) — "
            "session rullades tillbaka, indexering kvarstår som lyckad.",
            document.id,
            owner_id,
            version.id,
        )

    return FileOutcome(
        filename=filename,
        status="indexed",
        source_id=str(document.id),
        archive_path=archive_path,
        archive_chain=archive_chain,
    )


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
        # P2: outer_filename seeds the first segment of any nested entry's archive_path (e.g.
        # "backup.zip!/users/docs.zip!/contracts/lease.pdf") — see zip_import.py's docstring.
        zip_result = validate_and_extract_zip(raw, outer_filename=filename)  # ZipSecurityError propagates — permanent, see app/worker.py
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
                    FileOutcome(
                        filename=entry.filename,
                        status="skipped",
                        reason="Manifestfil, importeras inte som ett eget dokument.",
                        archive_path=entry.archive_path,
                        archive_chain=entry.archive_chain,
                    )
                )
            elif entry.status == "encrypted":
                # P2: kept as its own distinct FileOutcome.status (not folded into generic
                # "skipped") purely for diagnostic clarity in ImportJob.file_results — see
                # docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §10.5. Counted alongside
                # skipped/duplicate in the job-level summary below; never resumable/blocked in
                # the P1 provider-pause sense.
                outcomes.append(
                    FileOutcome(
                        filename=entry.filename,
                        status="encrypted",
                        reason=entry.reason,
                        archive_path=entry.archive_path,
                        archive_chain=entry.archive_chain,
                    )
                )
            elif entry.status != "ok":
                outcomes.append(
                    FileOutcome(
                        filename=entry.filename,
                        status="skipped",
                        reason=entry.reason,
                        archive_path=entry.archive_path,
                        archive_chain=entry.archive_chain,
                    )
                )
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
                    archive_path=entry.archive_path,
                    archive_chain=entry.archive_chain,
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
    # P2: an encrypted/password-protected entry — never attempted, never resumable. Counted
    # alongside skipped/duplicate for job-level status purposes (see docs/
    # MAINAI_PROJECT_UNDERSTANDING_PLAN.md §10.5); kept as its own FileOutcome.status only for
    # clearer per-file diagnostics.
    encrypted = sum(1 for o in outcomes if o.status == "encrypted")
    # P1: files paused on awaiting_provider/blocked_provider — nothing genuinely failed here,
    # the job is just waiting for the AI provider (see IndexStatus's docstring).
    blocked = sum(1 for o in outcomes if o.status == "blocked")

    job.succeeded_count = succeeded
    job.failed_count = failed
    job.skipped_count = skipped + duplicates + encrypted
    job.blocked_count = blocked

    if cancelled and not succeeded:
        job.status = ImportJobStatus.cancelled
        job.completed_at = datetime.utcnow()
    elif blocked and not (succeeded or failed):
        # P1: every non-duplicate/non-skipped file is paused on the provider — this is NOT a
        # terminal outcome. app/worker.py's _requeue_blocked_jobs flips this back to `pending`
        # automatically once the active provider verifies ok, so `completed_at` is
        # deliberately left unset — the job isn't done, it's waiting.
        job.status = ImportJobStatus.blocked
    elif failed and (succeeded or duplicates or skipped or blocked or encrypted):
        job.status = ImportJobStatus.partial
        job.completed_at = datetime.utcnow()
    elif failed and not (succeeded or duplicates or skipped or blocked or encrypted):
        job.status = ImportJobStatus.failed
        job.failure_reason = "Alla filer i paketet misslyckades."
        job.completed_at = datetime.utcnow()
    else:
        job.status = ImportJobStatus.completed
        job.completed_at = datetime.utcnow()

    # 2026-07-28, permanent fix (production incident: MAINAI_CONTEXT_BUNDLE.md stuck at
    # `embedding` while its job showed "Klar"): a job must never report completed/partial/
    # failed while a linked, live Document is still stuck mid-pipeline. This is a safety net
    # independent of THIS pass's own outcomes list — it re-checks the actual, current Document
    # rows rather than trusting the counts above alone, since not every stuck row is
    # necessarily represented in `outcomes` (e.g. a prior attempt's crash left it behind before
    # this pass ever reached it). Overrides back to `pending` (no completed_at/failure_reason)
    # so the very next poll cycle reclaims and resumes it via claim_next_job — see
    # _import_one_file's RESUMABLE_INDEX_STATUSES branch above, and app/worker.py's
    # _reconcile_orphaned_documents for the equivalent guard when a job is discovered to
    # already be terminal (no poll cycle left to naturally reach this function again).
    if job.status in (ImportJobStatus.completed, ImportJobStatus.partial, ImportJobStatus.failed):
        still_stuck = (
            db.query(Document.id)
            .filter(
                Document.import_job_id == job.id,
                Document.deleted_at.is_(None),
                Document.status.in_(RESUMABLE_INDEX_STATUSES),
            )
            .first()
            is not None
        )
        if still_stuck:
            job.status = ImportJobStatus.pending
            job.completed_at = None
            job.failure_reason = None

    db.add(job)
    db.commit()
