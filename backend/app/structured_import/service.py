"""Durable execution service for format-agnostic structured exports."""

import json
import logging
import re
import uuid
from collections.abc import Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.jobs.mainai_job_lease import JobLeaseLostError, renew_mainai_job_lease
from app.jobs.service import mark_cancelled, mark_completed, mark_failed, update_progress
from app.models.document import Document
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory, MainAIJobStatus
from app.models.structured_import import (
    StructuredImportItem,
    StructuredImportItemState,
    StructuredImportRun,
    StructuredImportRunStatus,
)
from app.storage import get_storage
from app.structured_import.adapter import AdapterItemFailure, StructuredExportItem, StructuredItemOutcome
from app.structured_import.registry import UnknownStructuredExportAdapter, resolve_adapter

logger = logging.getLogger("mainai.structured_import")

STRUCTURED_EXPORT_IMPORT_JOB_TYPE = "structured_export_import"
MAX_ITEMS_PER_TRANSACTION = 100
MAX_METADATA_JSON_BYTES = 64 * 1024
MAX_SOURCE_IDENTITY_BYTES = 4096
_SAFE_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class StructuredImportContractError(ValueError):
    pass


def _bounded_mapping(value: Mapping[str, object], label: str) -> dict:
    result = dict(value)
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_METADATA_JSON_BYTES:
        raise StructuredImportContractError(f"{label} exceeds {MAX_METADATA_JSON_BYTES} bytes")
    return result


def _validate_identity(value: str) -> str:
    if not value or len(value.encode("utf-8")) > MAX_SOURCE_IDENTITY_BYTES:
        raise StructuredImportContractError("source_identity must contain 1..4096 UTF-8 bytes")
    return value


def _validate_failure_code(value: str) -> str:
    if not _SAFE_CODE.fullmatch(value):
        raise StructuredImportContractError("failure_code is not in the safe closed-vocabulary shape")
    return value


def _validate_outcome(outcome: StructuredItemOutcome) -> StructuredItemOutcome:
    if not isinstance(outcome, StructuredItemOutcome):
        raise StructuredImportContractError("processor returned an invalid outcome type")
    if outcome.content_sha256 is not None and not _SHA256.fullmatch(outcome.content_sha256):
        raise StructuredImportContractError("processor returned an invalid sha256")
    if outcome.size_bytes is not None and outcome.size_bytes < 0:
        raise StructuredImportContractError("processor returned a negative size")
    return outcome


def _parse_refs(job: MainAIJob) -> tuple[uuid.UUID, str]:
    document_refs = [r for r in job.input_refs if r.get("type") == "document"]
    adapter_refs = [r for r in job.input_refs if r.get("type") == "structured_export_adapter"]
    if len(document_refs) != 1 or len(adapter_refs) != 1:
        raise StructuredImportContractError("job must reference exactly one document and one adapter")
    return uuid.UUID(str(document_refs[0]["id"])), str(adapter_refs[0]["id"])


def _get_or_create_run(
    db: Session, job: MainAIJob, document: Document, adapter_key: str, adapter_version: str
) -> StructuredImportRun:
    run = db.execute(select(StructuredImportRun).where(StructuredImportRun.job_id == job.id)).scalar_one_or_none()
    if run is not None:
        if (
            run.source_document_id != document.id
            or run.adapter_key != adapter_key
            or run.adapter_version != adapter_version
            or run.source_checksum != document.checksum
        ):
            raise StructuredImportContractError("durable import run no longer matches its immutable source/adapter contract")
        return run
    run = StructuredImportRun(
        job_id=job.id,
        owner_id=job.owner_id,
        source_document_id=document.id,
        adapter_key=adapter_key,
        adapter_version=adapter_version,
        source_checksum=document.checksum,
        checkpoint={},
        status=StructuredImportRunStatus.running,
    )
    db.add(run)
    db.flush()
    return run


def _record_event(
    db: Session,
    run: StructuredImportRun,
    event: StructuredExportItem | AdapterItemFailure,
    outcome: StructuredItemOutcome,
) -> bool:
    identity = _validate_identity(event.source_identity)
    provenance = _bounded_mapping(event.provenance, "provenance")
    checkpoint = _bounded_mapping(event.checkpoint_after, "checkpoint")
    existing = db.execute(
        select(StructuredImportItem).where(
            StructuredImportItem.run_id == run.id,
            StructuredImportItem.source_identity == identity,
        )
    ).scalar_one_or_none()
    if existing is not None:
        # Replay after a crash is a no-op for the durable item result. The checkpoint may
        # still advance past it, allowing an adapter with a conservative checkpoint to make
        # progress without rewriting already-truthful outcomes.
        if (
            existing.provenance != provenance
            or existing.state != outcome.state
            or existing.content_sha256 != outcome.content_sha256
            or existing.size_bytes != outcome.size_bytes
            or existing.failure_code != outcome.failure_code
            or existing.retryable != outcome.retryable
        ):
            raise StructuredImportContractError(
                f"stable source identity {identity!r} replayed with a different durable outcome"
            )
        run.checkpoint = checkpoint
        return False
    failure_code = _validate_failure_code(outcome.failure_code) if outcome.failure_code else None
    db.add(
        StructuredImportItem(
            run_id=run.id,
            owner_id=run.owner_id,
            source_identity=identity,
            state=outcome.state,
            provenance=provenance,
            checkpoint_after=checkpoint,
            content_sha256=outcome.content_sha256,
            size_bytes=outcome.size_bytes,
            failure_code=failure_code,
            retryable=outcome.retryable,
        )
    )
    # SessionLocal deliberately disables autoflush. Flush now so a repeated stable identity
    # later in the SAME transaction is visible to the next lookup and becomes a replay no-op
    # instead of reaching the unique constraint only at commit.
    db.flush()
    run.checkpoint = checkpoint
    return True


def _processed_count(db: Session, run_id: uuid.UUID) -> int:
    return int(
        db.execute(select(func.count()).select_from(StructuredImportItem).where(StructuredImportItem.run_id == run_id)).scalar_one()
    )


async def run_structured_export_import_job(
    db: Session,
    job_id: uuid.UUID,
    owner_id: uuid.UUID,
    *,
    worker_id: str,
    lease_generation: int,
    lease_seconds: int,
) -> None:
    """Runs bounded transactions; every checkpoint commits with its item outcomes and fence."""

    job = db.get(MainAIJob, job_id)
    if job is None:
        return
    try:
        document_id, adapter_key = _parse_refs(job)
        binding = resolve_adapter(adapter_key)
        document = db.execute(
            select(Document).where(
                Document.id == document_id,
                Document.uploaded_by == owner_id,
                Document.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if document is None or not document.storage_key or not document.checksum:
            raise StructuredImportContractError("canonical source document is missing, deleted, or not durably stored")
        storage = get_storage()
        expected_storage_key = f"{document.checksum[:2]}/{document.checksum}"
        if not _SHA256.fullmatch(document.checksum) or document.storage_key != expected_storage_key:
            raise StructuredImportContractError("canonical source locator does not match its recorded checksum")
        if not storage.exists(document.storage_key):
            raise StructuredImportContractError("canonical source blob is missing")
        run = _get_or_create_run(db, job, document, adapter_key, binding.adapter.version)

        if run.discovered_total is None:
            with storage.open_read(document.storage_key) as source:
                discovery = binding.adapter.discover(source)
            if discovery.total_items is not None and discovery.total_items < 0:
                raise StructuredImportContractError("adapter returned a negative discovery total")
            _bounded_mapping(discovery.metadata, "discovery metadata")
            run.discovered_total = discovery.total_items
            update_progress(
                db,
                job,
                worker_id=worker_id,
                lease_generation=lease_generation,
                current=_processed_count(db, run.id),
                total=discovery.total_items,
                phase="importing",
            )
            db.commit()

        while True:
            db.expire_all()
            job = db.get(MainAIJob, job_id)
            run = db.execute(select(StructuredImportRun).where(StructuredImportRun.job_id == job_id)).scalar_one()
            if job is None or job.status != MainAIJobStatus.running:
                return
            if job.cancel_requested:
                run.status = StructuredImportRunStatus.cancelled
                mark_cancelled(db, job, worker_id=worker_id, lease_generation=lease_generation)
                return

            emitted = 0
            exhausted = True
            with storage.open_read(document.storage_key) as source:
                iterator = binding.adapter.iter_items(source, dict(run.checkpoint))
                for event in iterator:
                    exhausted = False
                    if isinstance(event, AdapterItemFailure):
                        outcome = StructuredItemOutcome(
                            state=StructuredImportItemState.failed,
                            failure_code=_validate_failure_code(event.failure_code),
                            retryable=event.retryable,
                        )
                    else:
                        try:
                            outcome = _validate_outcome(binding.processor.process(event))
                        except Exception:  # noqa: BLE001 - isolate one malformed item from the export
                            logger.exception("structured import item failed safely (job=%s)", job_id)
                            outcome = StructuredItemOutcome(
                                state=StructuredImportItemState.failed,
                                failure_code="item_processing_error",
                                retryable=False,
                            )
                    _record_event(db, run, event, outcome)
                    emitted += 1
                    if emitted >= MAX_ITEMS_PER_TRANSACTION:
                        exhausted = False
                        break

            current = _processed_count(db, run.id)
            renew_mainai_job_lease(db, job_id, worker_id, lease_generation, lease_seconds)
            update_progress(
                db,
                job,
                worker_id=worker_id,
                lease_generation=lease_generation,
                current=current,
                total=run.discovered_total,
                phase="importing",
            )
            db.commit()
            if emitted == 0 or exhausted:
                break

        db.expire_all()
        job = db.get(MainAIJob, job_id)
        run = db.execute(select(StructuredImportRun).where(StructuredImportRun.job_id == job_id)).scalar_one()
        if job is None or job.status != MainAIJobStatus.running:
            return
        run.status = StructuredImportRunStatus.completed
        mark_completed(
            db,
            job,
            worker_id=worker_id,
            lease_generation=lease_generation,
            public_message=f"Processed {_processed_count(db, run.id)} structured export item(s) deterministically.",
        )
    except JobLeaseLostError:
        db.rollback()
        logger.warning("structured import job %s lost lease generation %s", job_id, lease_generation)
    except (StructuredImportContractError, UnknownStructuredExportAdapter):
        db.rollback()
        logger.exception("structured import job %s rejected its source/adapter contract", job_id)
        job = db.get(MainAIJob, job_id)
        if job is not None:
            try:
                run = db.execute(select(StructuredImportRun).where(StructuredImportRun.job_id == job_id)).scalar_one_or_none()
                if run is not None:
                    run.status = StructuredImportRunStatus.failed
                mark_failed(
                    db,
                    job,
                    worker_id=worker_id,
                    lease_generation=lease_generation,
                    error_category=MainAIJobErrorCategory.permanent,
                )
            except JobLeaseLostError:
                db.rollback()
    except Exception:  # noqa: BLE001 - package-level adapter failure is terminal and safely classified
        db.rollback()
        logger.exception("structured import job %s failed", job_id)
        job = db.get(MainAIJob, job_id)
        if job is not None:
            try:
                run = db.execute(select(StructuredImportRun).where(StructuredImportRun.job_id == job_id)).scalar_one_or_none()
                if run is not None:
                    run.status = StructuredImportRunStatus.failed
                mark_failed(
                    db,
                    job,
                    worker_id=worker_id,
                    lease_generation=lease_generation,
                    error_category=MainAIJobErrorCategory.unexpected,
                )
            except JobLeaseLostError:
                db.rollback()
