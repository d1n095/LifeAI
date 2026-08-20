"""Fail-closed JobLock behavior for library import — Redis unreachable must not race."""

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.jobs.lock import JobLock, JobLockUnavailable
from app.models.document import Document
from app.models.import_job import ImportJob, ImportJobStatus
from app.rag.library_import import run_import_job
from app.request_context import current_user_id as current_user_id_var
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


def _read_chunk_for(data: bytes, size: int = 1 << 16):
    pos = 0

    def _read():
        nonlocal pos
        chunk = data[pos : pos + size]
        pos += len(chunk)
        return chunk

    return _read


def _make_job(db_session, owner_id, raw: bytes, filename: str) -> ImportJob:
    current_user_id_var.set(str(owner_id))
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
    blob = get_storage().write_stream(_read_chunk_for(raw), max_bytes=max(len(raw), 1))
    job = ImportJob(
        owner_id=owner_id,
        status=ImportJobStatus.pending,
        source_filename=filename,
        source_checksum=blob.sha256,
        source_storage_key=blob.storage_key,
        source_size_bytes=blob.size_bytes,
    )
    db_session.add(job)
    db_session.commit()
    return job


@pytest.mark.asyncio
async def test_run_import_job_raises_when_job_lock_unavailable(db_session, make_verified_user, monkeypatch):
    """Redis unreachable must fail closed (raise JobLockUnavailable), never proceed and
    race duplicate Document inserts for byte-identical uploads."""
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, b"content that must not race", "a.txt")

    def _raise_unavailable(self):
        raise JobLockUnavailable("redis down")

    monkeypatch.setattr(JobLock, "acquire", _raise_unavailable)

    with pytest.raises(JobLockUnavailable):
        await run_import_job(db_session, job.id, user.id)

    assert db_session.query(Document).filter_by(uploaded_by=user.id).count() == 0
