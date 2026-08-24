"""Durable /api/documents/upload — no BackgroundTasks; ImportJob + worker resume.

Proves `#126 FIXED OWNER CONTEXT != DURABLE DELIVERY`:
  HTTP success → durable ImportJob + Document.storage_key committed
  → API process may die (no in-process indexer required)
  → worker/run_import_job later claims and indexes
  → retries are idempotent
"""

from __future__ import annotations

import io

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
    db_session, make_verified_user
):
    """Crash boundary A → worker claim: durable job is enough for later indexing."""
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

    # Simulate API process death: drop request-scoped owner, do NOT call any background indexer.
    current_user_id_var.set(None)

    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    await run_import_job(db_session, job_id, user.id)

    db_session.expire_all()
    job = db_session.get(ImportJob, job_id)
    doc = db_session.get(Document, doc_id)
    assert job is not None and job.status == ImportJobStatus.completed
    assert doc is not None and doc.status == IndexStatus.indexed
    assert db_session.query(DocumentChunk).filter_by(document_id=doc_id).count() >= 1
    # Same Document row resumed — no duplicate.
    assert db_session.query(Document).filter(
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
