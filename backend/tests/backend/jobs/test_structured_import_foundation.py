"""Contract tests for the format-agnostic structured-export import foundation.

The synthetic newline-delimited format below exists only to exercise the adapter boundary.
It is deliberately not described or named as any vendor's export format.
"""

import hashlib
import io
import json

import pytest
from sqlalchemy import text as sa_text

from app.jobs import service as job_service
from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.models.document import Document, DocumentSource, IndexStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from app.models.structured_import import StructuredImportItem, StructuredImportItemState, StructuredImportRun
from app.request_context import current_user_id as current_user_id_var
from app.storage import get_storage
from app.structured_import.adapter import (
    AdapterDiscovery,
    AdapterItemFailure,
    StructuredExportItem,
    StructuredItemOutcome,
)
from app.structured_import.registry import AdapterBinding, register_adapter, unregister_adapter
from app.structured_import.service import run_structured_export_import_job


class SyntheticLineAdapter:
    key = "synthetic-lines-v1"
    version = "1"
    max_observed_line_bytes = 0

    def discover(self, source):
        count = 0
        while line := source.readline(1025):
            self.max_observed_line_bytes = max(self.max_observed_line_bytes, len(line))
            if len(line) > 1024:
                raise ValueError("synthetic fixture line too large")
            count += 1
        return AdapterDiscovery(total_items=count, metadata={"fixture": "synthetic"})

    def iter_items(self, source, checkpoint):
        start = int(checkpoint.get("line", 0))
        for index, line in enumerate(iter(lambda: source.readline(1025), b""), start=1):
            self.max_observed_line_bytes = max(self.max_observed_line_bytes, len(line))
            if index <= start:
                continue
            checkpoint_after = {"line": index}
            try:
                value = json.loads(line)
                identity = str(value["identity"])
                payload = str(value["payload"]).encode()
                provenance_line = int(value.get("provenance_line", index))
            except Exception:  # noqa: BLE001 - the fixture intentionally emits a per-item failure
                yield AdapterItemFailure(
                    source_identity=f"malformed-line-{index}",
                    provenance={"line": index},
                    checkpoint_after=checkpoint_after,
                    failure_code="malformed_item",
                )
                continue
            yield StructuredExportItem(
                source_identity=identity,
                provenance={"line": provenance_line},
                checkpoint_after=checkpoint_after,
                content_chunks=lambda payload=payload: iter((payload,)),
            )


class HashingProcessor:
    def process(self, item):
        digest = hashlib.sha256()
        size = 0
        for chunk in item.content_chunks():
            digest.update(chunk)
            size += len(chunk)
        return StructuredItemOutcome(
            state=StructuredImportItemState.parsed,
            content_sha256=digest.hexdigest(),
            size_bytes=size,
        )


def _set_owner(db, owner_id):
    current_user_id_var.set(str(owner_id))
    db.execute(sa_text("SET LOCAL app.current_user_id = :owner"), {"owner": str(owner_id)})


def _source_and_job(db, owner_id, rows, *, adapter_key="synthetic-lines-v1"):
    _set_owner(db, owner_id)
    raw = b"\n".join(json.dumps(row).encode() if isinstance(row, dict) else row for row in rows) + b"\n"
    stream = io.BytesIO(raw)
    blob = get_storage().write_stream(lambda: stream.read(64), max_bytes=len(raw) + 1)
    document = Document(
        uploaded_by=owner_id,
        title="Synthetic structured export",
        source=DocumentSource.upload,
        status=IndexStatus.original_stored,
        checksum=blob.sha256,
        storage_key=blob.storage_key,
        size_bytes=blob.size_bytes,
        original_filename="synthetic-export.ndjson",
    )
    db.add(document)
    db.commit()
    job = job_service.create_job(
        db,
        owner_id=owner_id,
        job_type="structured_export_import",
        input_refs=[
            {"type": "document", "id": str(document.id)},
            {"type": "structured_export_adapter", "id": adapter_key},
        ],
        created_by="test",
    )
    return document, job


@pytest.fixture
def synthetic_binding():
    adapter = SyntheticLineAdapter()
    register_adapter(AdapterBinding(adapter=adapter, processor=HashingProcessor()))
    yield adapter
    unregister_adapter(adapter.key)


async def _claim_and_run(db, admin, job, owner_id, worker="structured-worker"):
    _, _, generation = claim_next_mainai_job(admin, worker, 120)
    _set_owner(db, owner_id)
    await run_structured_export_import_job(
        db,
        job.id,
        owner_id,
        worker_id=worker,
        lease_generation=generation,
        lease_seconds=120,
    )
    return generation


@pytest.mark.asyncio
async def test_malformed_item_isolated_and_duplicate_identity_replay_is_idempotent(
    db_session, superuser_db, make_verified_user, synthetic_binding
):
    owner, _ = make_verified_user()
    _, job = _source_and_job(
        db_session,
        owner.id,
        [
            {"identity": "one", "payload": "first"},
            b"not-json",
            {"identity": "one", "payload": "first", "provenance_line": 1},
            {"identity": "two", "payload": "last"},
        ],
    )
    await _claim_and_run(db_session, superuser_db, job, owner.id)

    persisted_job = superuser_db.get(MainAIJob, job.id)
    items = superuser_db.query(StructuredImportItem).order_by(StructuredImportItem.source_identity).all()
    assert persisted_job.status == MainAIJobStatus.completed
    assert [(item.source_identity, item.state) for item in items] == [
        ("malformed-line-2", "failed"),
        ("one", "parsed"),
        ("two", "parsed"),
    ]
    assert items[0].failure_code == "malformed_item"
    run = superuser_db.query(StructuredImportRun).filter_by(job_id=job.id).one()
    assert run.checkpoint == {"line": 4}


@pytest.mark.asyncio
async def test_stable_identity_collision_with_different_content_fails_closed(
    db_session, superuser_db, make_verified_user, synthetic_binding
):
    owner, _ = make_verified_user()
    _, job = _source_and_job(
        db_session,
        owner.id,
        [
            {"identity": "same", "payload": "first", "provenance_line": 1},
            {"identity": "same", "payload": "different", "provenance_line": 1},
        ],
    )
    await _claim_and_run(db_session, superuser_db, job, owner.id)
    assert superuser_db.get(MainAIJob, job.id).status == MainAIJobStatus.failed
    run = superuser_db.query(StructuredImportRun).filter_by(job_id=job.id).one()
    assert run.status == "failed"
    assert superuser_db.query(StructuredImportItem).count() == 0


@pytest.mark.asyncio
async def test_resume_after_crash_replays_only_uncommitted_batch(
    db_session, superuser_db, make_verified_user, synthetic_binding, monkeypatch
):
    import app.structured_import.service as import_service

    owner, _ = make_verified_user()
    _, job = _source_and_job(
        db_session,
        owner.id,
        [{"identity": str(i), "payload": str(i)} for i in range(5)],
    )
    monkeypatch.setattr(import_service, "MAX_ITEMS_PER_TRANSACTION", 2)
    _, _, generation = claim_next_mainai_job(superuser_db, "worker-crash", 120)
    _set_owner(db_session, owner.id)

    calls = 0
    original = HashingProcessor.process

    def crash_on_third(self, item):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise KeyboardInterrupt("synthetic hard crash")
        return original(self, item)

    monkeypatch.setattr(HashingProcessor, "process", crash_on_third)
    with pytest.raises(KeyboardInterrupt):
        await run_structured_export_import_job(
            db_session,
            job.id,
            owner.id,
            worker_id="worker-crash",
            lease_generation=generation,
            lease_seconds=120,
        )
    db_session.rollback()
    assert superuser_db.query(StructuredImportItem).count() == 2

    monkeypatch.setattr(HashingProcessor, "process", original)
    await run_structured_export_import_job(
        db_session,
        job.id,
        owner.id,
        worker_id="worker-crash",
        lease_generation=generation,
        lease_seconds=120,
    )
    assert superuser_db.query(StructuredImportItem).count() == 5
    assert superuser_db.get(MainAIJob, job.id).status == MainAIJobStatus.completed


@pytest.mark.asyncio
async def test_cancel_is_acknowledged_before_item_processing(
    db_session, superuser_db, make_verified_user, synthetic_binding
):
    owner, _ = make_verified_user()
    _, job = _source_and_job(db_session, owner.id, [{"identity": "one", "payload": "x"}])
    job.cancel_requested = True
    db_session.commit()
    await _claim_and_run(db_session, superuser_db, job, owner.id)
    assert superuser_db.get(MainAIJob, job.id).status == MainAIJobStatus.cancelled
    assert superuser_db.query(StructuredImportItem).count() == 0


@pytest.mark.asyncio
async def test_stale_worker_fence_rolls_back_item_and_checkpoint(
    db_session, superuser_db, make_verified_user, synthetic_binding, monkeypatch
):
    owner, _ = make_verified_user()
    _, job = _source_and_job(db_session, owner.id, [{"identity": "one", "payload": "x"}])
    _, _, generation = claim_next_mainai_job(superuser_db, "worker-stale", 120)
    _set_owner(db_session, owner.id)
    original = HashingProcessor.process
    reclaimed = False

    def reclaim_after_processing(self, item):
        nonlocal reclaimed
        outcome = original(self, item)
        if not reclaimed:
            reclaimed = True
            superuser_db.execute(
                sa_text(
                    "UPDATE mainai_jobs SET locked_by='worker-new', lease_generation=lease_generation+1 "
                    "WHERE id=:job"
                ),
                {"job": str(job.id)},
            )
            superuser_db.commit()
        return outcome

    monkeypatch.setattr(HashingProcessor, "process", reclaim_after_processing)
    await run_structured_export_import_job(
        db_session,
        job.id,
        owner.id,
        worker_id="worker-stale",
        lease_generation=generation,
        lease_seconds=120,
    )
    assert superuser_db.query(StructuredImportItem).count() == 0
    run = superuser_db.query(StructuredImportRun).filter_by(job_id=job.id).one()
    # Discovery was fenced and committed before the race; only the in-flight item/checkpoint
    # transaction must disappear when ownership changes.
    assert run.checkpoint == {}
    assert superuser_db.get(MainAIJob, job.id).locked_by == "worker-new"


@pytest.mark.asyncio
async def test_large_stream_is_bounded_and_provider_independent(
    db_session, superuser_db, make_verified_user, synthetic_binding, monkeypatch
):
    from app.providers.openai_provider import OpenAIProvider

    def provider_must_not_run(*args, **kwargs):
        raise AssertionError("provider was called by deterministic import")

    monkeypatch.setattr(OpenAIProvider, "chat", provider_must_not_run)
    owner, _ = make_verified_user()
    _, job = _source_and_job(
        db_session,
        owner.id,
        [{"identity": str(i), "payload": "x" * 700} for i in range(1200)],
    )
    await _claim_and_run(db_session, superuser_db, job, owner.id)
    assert superuser_db.query(StructuredImportItem).count() == 1200
    assert synthetic_binding.max_observed_line_bytes <= 1024
    assert superuser_db.get(MainAIJob, job.id).provider is None
    assert superuser_db.get(MainAIJob, job.id).model is None
