"""Epistemic fail-closed for manifest active_truth_status.

Invalid values must not elevate to ActiveTruthStatus.active (most privileged trust class).
Kept in its own module so it does not collide with concurrent edits to test_library_import.py
(Claude PR #114).
"""

import io
import zipfile

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.models.document import ActiveTruthStatus, Document
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
            raw_usage={"prompt_tokens": 5, "completion_tokens": 2},
        )

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


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
async def test_invalid_manifest_truth_status_fails_closed_to_proposed(db_session, make_verified_user):
    """Invalid active_truth_status must not become active (fail-open to highest trust)."""
    user, _ = make_verified_user()
    manifest = (
        b'{"package": "test", "documents": ['
        b'{"file": "claim.txt", "classification": "decisions", "active_truth_status": "definitely-true"}'
        b"]}"
    )
    raw = _make_zip({"manifest.json": manifest, "claim.txt": b"A claim that is not yet confirmed."})
    job = _make_job(db_session, user.id, raw, "bad-truth.zip")

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed
    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="claim.txt").first()
    assert doc is not None
    assert doc.active_truth_status == ActiveTruthStatus.proposed


@pytest.mark.asyncio
async def test_valid_manifest_truth_status_still_applied(db_session, make_verified_user):
    user, _ = make_verified_user()
    manifest = (
        b'{"package": "test", "documents": ['
        b'{"file": "old.txt", "classification": "history", "active_truth_status": "historical"}'
        b"]}"
    )
    raw = _make_zip({"manifest.json": manifest, "old.txt": b"Historical note."})
    job = _make_job(db_session, user.id, raw, "ok-truth.zip")

    await run_import_job(db_session, job.id, user.id)

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="old.txt").first()
    assert doc is not None
    assert doc.active_truth_status == ActiveTruthStatus.historical


@pytest.mark.asyncio
async def test_missing_truth_status_still_defaults_to_active_for_ordinary_uploads(db_session, make_verified_user):
    """Empty/missing status on ordinary imports remains active (founder-direct upload default)."""
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, b"Ordinary founder upload.", "plain.txt")

    await run_import_job(db_session, job.id, user.id)

    doc = db_session.query(Document).filter_by(uploaded_by=user.id).first()
    assert doc is not None
    assert doc.active_truth_status == ActiveTruthStatus.active
