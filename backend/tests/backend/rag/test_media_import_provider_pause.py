"""Media index path must pause on provider outage and never persist str(exc).

Mirrors app/rag/ingest.py's index_document gates — media previously marked IndexStatus.failed
with raw exception text (which can embed API keys in URLs) and never used awaiting_provider.
"""

import httpx
import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.document import Document, IndexStatus
from app.models.import_job import ImportJob, ImportJobStatus
from app.providers.openai_provider import OpenAIProvider
from app.providers.transcription import (
    MockTranscriptionProvider,
    TranscriptResult,
    TranscriptSegment,
)
from app.rag.library_import import run_import_job
from app.request_context import current_user_id as current_user_id_var
from app.storage import get_storage

EMBEDDING_DIM = get_settings().embedding_dim
VALID_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64


def _set_rls_user(db_session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _read_chunk_for(data: bytes, size: int = 1 << 16):
    pos = 0

    def _read():
        nonlocal pos
        chunk = data[pos : pos + size]
        pos += len(chunk)
        return chunk

    return _read


def _make_job(db_session, owner_id, raw: bytes, filename: str) -> ImportJob:
    _set_rls_user(db_session, owner_id)
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


@pytest.fixture(autouse=True)
def _fake_transcription(monkeypatch):
    async def _fake_transcribe(self, raw, filename, media_kind):
        return TranscriptResult(
            segments=[TranscriptSegment(0.0, 2.0, "En kort anteckning.")],
            duration_seconds=2.0,
            provider="mock",
            model="placeholder-v1",
        )

    monkeypatch.setattr(MockTranscriptionProvider, "transcribe", _fake_transcribe)


@pytest.mark.asyncio
async def test_media_import_pauses_awaiting_provider_when_embedding_unconfigured(
    db_session, make_verified_user, monkeypatch
):
    monkeypatch.setattr(get_settings(), "openai_api_key", None)
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, VALID_MP3, "pause.mp3")

    await run_import_job(db_session, job.id, user.id)

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="pause.mp3").one()
    assert doc.status == IndexStatus.awaiting_provider
    assert "bearbetas automatiskt" in (doc.error_message or "")
    assert doc.content_preview is not None
    assert "anteckning" in doc.content_preview


@pytest.mark.asyncio
async def test_media_import_post_preflight_embed_failure_never_persists_raw_exc(
    db_session, make_verified_user, monkeypatch
):
    """httpx errors can embed secrets in the request URL — must use classify_provider_exception."""
    from app.providers.verification import VERIFICATION_PROBE_TEXT

    leak_url = "https://generativelanguage.googleapis.com/v1/models?key=SECRET_KEY_SHOULD_NOT_LEAK"

    async def _fake_embed(self, texts, model, **kwargs):
        if texts == [VERIFICATION_PROBE_TEXT]:
            return [[0.01] * EMBEDDING_DIM]
        request = httpx.Request("POST", leak_url)
        response = httpx.Response(500, request=request)
        raise httpx.HTTPStatusError("500 error", request=request, response=response)

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)

    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, VALID_MP3, "leak.mp3")

    await run_import_job(db_session, job.id, user.id)

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="leak.mp3").one()
    assert doc.status == IndexStatus.indexing_failed
    assert "SECRET_KEY_SHOULD_NOT_LEAK" not in (doc.error_message or "")
    assert "key=" not in (doc.error_message or "").lower()
