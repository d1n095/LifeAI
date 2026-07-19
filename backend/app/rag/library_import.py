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

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.document import ActiveTruthStatus, Document, DocumentSource, IndexStatus, KnowledgeClassification
from app.request_context import current_user_id as current_user_id_var
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_version import KnowledgeVersion
from app.rag.extract import extract_text
from app.rag.ingest import index_document
from app.rag.zip_import import ZipSecurityError, sha256_bytes, validate_and_extract_zip

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
    try:
        text_content = extract_text(filename, content)
    except Exception as exc:  # noqa: BLE001 - one file's extraction failure must not abort the batch
        return FileOutcome(filename=filename, status="failed", reason=f"Kunde inte extrahera text: {exc}")

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

    db.add(
        KnowledgeVersion(
            source_id=document.id,
            owner_id=owner_id,
            version_number=1,
            checksum=checksum,
            extraction_version=EXTRACTION_VERSION,
            raw_metadata={"original_filename": filename, "size_bytes": len(content), "media_type": document.media_type},
        )
    )
    db.commit()

    await index_document(db, document, text_content)

    if document.status == IndexStatus.failed:
        return FileOutcome(filename=filename, status="failed", reason=document.error_message, source_id=str(document.id))
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
    """Runs synchronously to completion, writing progress to the ImportJob row as it goes.
    Never raises — every failure mode (a bad ZIP, an extraction error, a DB error on one
    file) is captured on the job row itself so a caller polling GET /api/library/jobs/{id}
    always sees a terminal, informative status rather than the request just hanging or the
    job row being stuck at "running" forever."""
    _set_rls_owner(db, owner_id)
    job = db.get(ImportJob, job_id)
    if job is None:
        return

    job.status = ImportJobStatus.running
    job.started_at = datetime.utcnow()
    db.add(job)
    db.commit()

    suffix = PurePosixPath(filename).suffix.lower()
    outcomes: list[FileOutcome] = []

    try:
        if suffix == ".zip":
            try:
                zip_result = validate_and_extract_zip(raw)
            except ZipSecurityError as exc:
                job.status = ImportJobStatus.failed
                job.failure_reason = str(exc)
                job.completed_at = datetime.utcnow()
                db.add(job)
                db.commit()
                return

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
                        import_job_id=job_id,
                        manifest_entry=manifest_entry,
                    )
                    outcomes.append(outcome)
                job.progress_current = len(outcomes)
                job.file_results = [o.__dict__ for o in outcomes]
                db.add(job)
                db.commit()
        else:
            job.progress_total = 1
            db.add(job)
            db.commit()
            checksum = sha256_bytes(raw)
            outcome = await _import_one_file(
                db, owner_id, filename, raw, checksum, project_id=project_id, import_job_id=job_id, manifest_entry={}
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
    except Exception as exc:  # noqa: BLE001 - the job row is the only place this failure can safely surface
        db.rollback()
        _set_rls_owner(db, owner_id)
        job = db.get(ImportJob, job_id)
        if job is not None:
            job.status = ImportJobStatus.failed
            job.failure_reason = f"Oväntat fel under import: {exc}"
            job.completed_at = datetime.utcnow()
            db.add(job)
            db.commit()
