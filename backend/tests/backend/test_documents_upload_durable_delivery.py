"""Durable /api/documents/upload — no BackgroundTasks; ImportJob + worker resume.

Proves `#126 FIXED OWNER CONTEXT != DURABLE DELIVERY`:
  HTTP success → durable ImportJob + Document.storage_key committed
  → API process may die (no in-process indexer required)
  → worker/run_import_job later claims and indexes
  → retries are idempotent
"""

from __future__ import annotations

import io
import uuid

import pytest
from fastapi import UploadFile
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.document import Document, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.request_context import current_user_id as current_user_id_var
from app.routers.documents import upload_document
from app.rag.library_import import run_import_job
from app.storage import get_storage

EMBEDDING_DIM = get_settings().embedding_dim


@pytest.fixture(autouse=True)
def _fake_embedding_provider(monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.01 * (i + 1)] * EMBEDDING_DIM for i, _ in enumerate(texts)]

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(
            content="[]",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 1, "completion_tokens": 1},
        )

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)


def _upload_file(name: str, raw: bytes) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(raw))


@pytest.mark.asyncio
async def test_documents_upload_commits_durable_import_job_before_indexing(
    db_session, make_verified_user
):
    """Crash boundary A: after HTTP success, work must exist without any BackgroundTasks."""
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    raw = b"Durable documents upload content for crash-boundary A."
    doc = await upload_document(
        request=None,  # type: ignore[arg-type]
        file=_upload_file("durable.txt", raw),
        category="notes",
        db=db_session,
        user=user,
    )

    assert doc.id is not None
    assert doc.storage_key is not None
    assert doc.checksum is not None
    assert doc.import_job_id is not None
    assert doc.status == IndexStatus.original_stored
    assert get_storage().exists(doc.storage_key)

    job = db_session.get(ImportJob, doc.import_job_id)
    assert job is not None
    assert job.status == ImportJobStatus.pending
    assert job.source_storage_key == doc.storage_key
    assert job.source_checksum == doc.checksum

    # No chunks yet — indexer has not run; work is only durable queue state.
    assert db_session.query(DocumentChunk).filter_by(document_id=doc.id).count() == 0


@pytest.mark.asyncio
async def test_documents_upload_survives_process_death_then_worker_indexes(
    db_session, superuser_db, make_verified_user
):
    """Crash boundary A → PRODUCTION claim clock → indexing.

    Deliberately does NOT call run_import_job() directly: that would only prove the job row is
    processable if something hands it to the indexer, which is exactly the assumption
    `#126 FIXED OWNER CONTEXT != DURABLE DELIVERY` was about. The upload must be visible to
    app/worker.py's real, owner-blind claim clock (`claim_next_job` on the superuser claim
    session, since knowledge_import_jobs has FORCE RLS) and be driven by the same
    `process_claimed_job` wrapper the worker loop uses — with no API-process state surviving."""
    from sqlalchemy.orm import sessionmaker

    from app.db import SessionLocal, migration_engine
    from app.jobs.lease import claim_next_job
    from app.worker import process_claimed_job

    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    raw = b"Enough text content for the durable upload worker resume path to chunk and embed."
    doc = await upload_document(
        request=None,  # type: ignore[arg-type]
        file=_upload_file("resume.txt", raw),
        category=None,
        db=db_session,
        user=user,
    )
    job_id = doc.import_job_id
    doc_id = doc.id
    assert job_id is not None

    # API process death: request session gone, request-scoped owner context gone, no
    # BackgroundTasks and no in-process reference to the job.
    db_session.close()
    current_user_id_var.set(None)

    claim_db = sessionmaker(bind=migration_engine)()
    try:
        claimed = claim_next_job(claim_db, "worker-under-test", 120)
    finally:
        claim_db.close()
    # The claim clock found the upload on its own, with no owner context supplied to it.
    assert claimed == (job_id, user.id)

    superuser_db.expire_all()
    claimed_job = superuser_db.get(ImportJob, job_id)
    assert claimed_job.status == ImportJobStatus.running
    assert claimed_job.locked_by == "worker-under-test"
    assert claimed_job.lease_expires_at is not None

    worker_db = SessionLocal()
    try:
        await process_claimed_job(worker_db, job_id, user.id)
    finally:
        worker_db.close()

    superuser_db.expire_all()
    job = superuser_db.get(ImportJob, job_id)
    indexed = superuser_db.get(Document, doc_id)
    assert job is not None and job.status == ImportJobStatus.completed
    assert indexed is not None and indexed.status == IndexStatus.indexed
    assert superuser_db.query(DocumentChunk).filter_by(document_id=doc_id).count() >= 1
    # Same Document row resumed — no duplicate.
    assert superuser_db.query(Document).filter(
        Document.uploaded_by == user.id, Document.deleted_at.is_(None)
    ).count() == 1


@pytest.mark.asyncio
async def test_documents_upload_identical_bytes_returns_completed_document(
    db_session, make_verified_user
):
    """Retry / duplicate HTTP upload after successful index is idempotent."""
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    raw = b"Idempotent durable documents upload payload."
    first = await upload_document(
        request=None,  # type: ignore[arg-type]
        file=_upload_file("same.txt", raw),
        category=None,
        db=db_session,
        user=user,
    )
    await run_import_job(db_session, first.import_job_id, user.id)
    db_session.expire_all()
    first = db_session.get(Document, first.id)
    assert first is not None and first.status == IndexStatus.indexed

    second = await upload_document(
        request=None,  # type: ignore[arg-type]
        file=_upload_file("same.txt", raw),
        category=None,
        db=db_session,
        user=user,
    )
    assert second.id == first.id
    assert (
        db_session.query(ImportJob)
        .filter_by(owner_id=user.id, source_checksum=first.checksum)
        .count()
        == 1
    )


def _admin_session():
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine

    return sessionmaker(bind=migration_engine)()


def _blob_with_pending_cleanup_task(raw: bytes) -> tuple[str, uuid.UUID]:
    """Reproduce the real precondition: this exact content-addressed key already has an
    outstanding `rejected_upload_cleanup` task (e.g. an earlier empty/rejected upload of the
    same bytes), so retain ordering is observable."""
    from app.storage.references import enqueue_rejected_upload_cleanup_task

    storage_key = get_storage().write_stream(iter([raw, b""]).__next__, max_bytes=len(raw)).storage_key
    return storage_key, enqueue_rejected_upload_cleanup_task(storage_key)


@pytest.mark.asyncio
async def test_documents_upload_crash_before_commit_leaves_blob_purgeable(
    db_session, make_verified_user, monkeypatch
):
    """#143 fix-forward regression: blob written → cleanup task pending → the ImportJob/
    Document transaction dies before commit → no durable reference exists → cleanup must
    STILL be able to purge the blob.

    retain_pending_rejected_upload_cleanup_tasks() commits on its own maintenance connection,
    so a pre-commit retain outlives this rollback and would strand the blob in the terminal
    `retained_shared` state forever."""
    from app.account.erasure import attempt_storage_deletion_task
    from app.models.storage_deletion_task import StorageDeletionStatus, StorageDeletionTask

    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    raw = f"documents upload crash before durable commit {uuid.uuid4().hex}".encode()
    storage_key, operation_id = _blob_with_pending_cleanup_task(raw)

    def _process_death(*_args, **_kwargs):
        raise RuntimeError("process death before durable commit")

    monkeypatch.setattr(db_session, "commit", _process_death)
    with pytest.raises(RuntimeError):
        await upload_document(
            request=None,  # type: ignore[arg-type]
            file=_upload_file("crash.txt", raw),
            category=None,
            db=db_session,
            user=user,
        )
    monkeypatch.undo()
    db_session.rollback()

    admin = _admin_session()
    try:
        # Checked RLS-free: "no reference exists" must not be ambiguous with "RLS hides it".
        assert (
            admin.execute(
                sa_text("SELECT count(*) FROM knowledge_import_jobs WHERE source_storage_key = :k"),
                {"k": storage_key},
            ).scalar()
            == 0
        )
        assert (
            admin.execute(
                sa_text("SELECT count(*) FROM documents WHERE storage_key = :k"), {"k": storage_key}
            ).scalar()
            == 0
        )

        task = admin.query(StorageDeletionTask).filter_by(operation_id=operation_id, storage_key=storage_key).one()
        assert task.status == StorageDeletionStatus.pending
        attempt_storage_deletion_task(admin, task)
        admin.refresh(task)
        assert task.status == StorageDeletionStatus.purged
        assert get_storage().exists(storage_key) is False
    finally:
        admin.close()


@pytest.mark.asyncio
async def test_documents_upload_committed_reference_retains_blob_against_cleanup(
    db_session, make_verified_user
):
    """Success complement of the crash test above: once the ImportJob/Document reference
    commits, retain supersedes the outstanding cleanup task and the outbox worker can no
    longer delete the blob the pending import job still needs to read."""
    from app.account.erasure import attempt_storage_deletion_task
    from app.models.storage_deletion_task import StorageDeletionStatus, StorageDeletionTask

    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    raw = f"documents upload committed reference retains blob {uuid.uuid4().hex}".encode()
    storage_key, operation_id = _blob_with_pending_cleanup_task(raw)

    doc = await upload_document(
        request=None,  # type: ignore[arg-type]
        file=_upload_file("retained.txt", raw),
        category=None,
        db=db_session,
        user=user,
    )
    assert doc.storage_key == storage_key

    admin = _admin_session()
    try:
        task = admin.query(StorageDeletionTask).filter_by(operation_id=operation_id, storage_key=storage_key).one()
        assert task.status == StorageDeletionStatus.retained_shared
        attempt_storage_deletion_task(admin, task)
        admin.refresh(task)
        assert task.status == StorageDeletionStatus.retained_shared
        assert get_storage().exists(storage_key) is True
    finally:
        admin.close()


@pytest.mark.asyncio
async def test_documents_upload_rejects_empty_file(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await upload_document(
            request=None,  # type: ignore[arg-type]
            file=_upload_file("empty.txt", b""),
            category=None,
            db=db_session,
            user=user,
        )
    assert exc.value.status_code == 400
