"""Integration tests for app/rag/library_import.py — the orchestrator tying ZIP validation,
text extraction, chunking/embedding and ImportJob progress together. Runs against a real
local Postgres (RLS included) and a deterministic fake embedding provider — see
conftest.py's `db_session`/`make_verified_user` fixtures and the `_fake_embed` monkeypatch
below. No real AI-provider key is used anywhere in this file."""

import io
import uuid
import zipfile

import pytest

from app.config import get_settings
from app.models.document import ActiveTruthStatus, Document, IndexStatus, KnowledgeClassification
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_version import KnowledgeVersion
from app.rag.library_import import run_import_job

EMBEDDING_DIM = get_settings().embedding_dim


@pytest.fixture(autouse=True)
def _fake_embedding_provider(monkeypatch):
    """Deterministic, offline embedding — no real provider call, no API key, matches the
    dimension pgvector's column requires (see conftest/other RAG tests for the same
    pattern)."""
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_embed(self, texts, model):
        return [[0.01 * (i + 1)] * EMBEDDING_DIM for i, _ in enumerate(texts)]

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _make_job(db_session, owner_id) -> ImportJob:
    from sqlalchemy import text

    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
    job = ImportJob(owner_id=owner_id, status=ImportJobStatus.pending)
    db_session.add(job)
    db_session.commit()
    return job


@pytest.mark.asyncio
async def test_single_text_file_import_succeeds(db_session, make_verified_user):
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id)

    await run_import_job(db_session, job.id, user.id, b"Detta ar ett testdokument om MainAI.", "test.txt")

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed
    assert job.succeeded_count == 1
    assert job.failed_count == 0

    doc = db_session.query(Document).filter_by(uploaded_by=user.id).first()
    assert doc is not None
    assert doc.status == IndexStatus.indexed
    assert doc.checksum is not None
    assert doc.classification == KnowledgeClassification.general
    assert doc.active_truth_status == ActiveTruthStatus.active

    version = db_session.query(KnowledgeVersion).filter_by(source_id=doc.id).first()
    assert version is not None
    assert version.checksum == doc.checksum


@pytest.mark.asyncio
async def test_zip_package_imports_every_supported_file(db_session, make_verified_user):
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id)
    raw = _make_zip({"a.txt": b"Innehall A", "b.md": b"# Innehall B", "sub/c.txt": b"Innehall C"})

    await run_import_job(db_session, job.id, user.id, raw, "package.zip")

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed
    assert job.succeeded_count == 3
    docs = db_session.query(Document).filter_by(uploaded_by=user.id).all()
    assert len(docs) == 3
    assert all(d.status == IndexStatus.indexed for d in docs)


@pytest.mark.asyncio
async def test_partial_failure_does_not_abort_the_whole_package(db_session, make_verified_user):
    """One unsupported/skipped file inside an otherwise-good package must not stop the
    others from importing — DEL 3's "fel i en fil far inte korrumpera hela paketet"."""
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id)
    raw = _make_zip({"good.txt": b"bra innehall", "unsupported.exe": b"MZfake", "also-good.md": b"# bra"})

    await run_import_job(db_session, job.id, user.id, raw, "mixed.zip")

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed  # skipped files aren't "failures"
    assert job.succeeded_count == 2
    assert job.skipped_count == 1
    docs = db_session.query(Document).filter_by(uploaded_by=user.id).all()
    assert {d.original_filename for d in docs} == {"good.txt", "also-good.md"}


@pytest.mark.asyncio
async def test_zip_security_violation_fails_the_job_cleanly_not_a_crash(db_session, make_verified_user):
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id)
    raw = _make_zip({"../../etc/passwd": b"pwned"})

    await run_import_job(db_session, job.id, user.id, raw, "evil.zip")

    db_session.refresh(job)
    assert job.status == ImportJobStatus.failed
    assert job.failure_reason is not None
    assert "sökväg" in job.failure_reason.lower()
    assert db_session.query(Document).filter_by(uploaded_by=user.id).count() == 0


@pytest.mark.asyncio
async def test_reimporting_identical_content_is_idempotent_marks_duplicate_not_a_second_document(
    db_session, make_verified_user
):
    user, _ = make_verified_user()
    job1 = _make_job(db_session, user.id)
    await run_import_job(db_session, job1.id, user.id, b"samma innehall varje gang", "same.txt")
    db_session.refresh(job1)
    assert job1.succeeded_count == 1

    job2 = _make_job(db_session, user.id)
    await run_import_job(db_session, job2.id, user.id, b"samma innehall varje gang", "same-igen.txt")
    db_session.refresh(job2)

    assert job2.status == ImportJobStatus.completed
    assert job2.succeeded_count == 0
    assert job2.skipped_count == 1  # counted as duplicate, not a failure
    assert db_session.query(Document).filter_by(uploaded_by=user.id).count() == 1


@pytest.mark.asyncio
async def test_manifest_classification_and_truth_status_are_applied(db_session, make_verified_user):
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id)
    manifest = (
        b'{"package": "test", "documents": ['
        b'{"file": "old-decision.txt", "classification": "decisions", "active_truth_status": "superseded"}'
        b"]}"
    )
    raw = _make_zip({"manifest.json": manifest, "old-decision.txt": b"Det gamla beslutet var X."})

    await run_import_job(db_session, job.id, user.id, raw, "with-manifest.zip")

    db_session.refresh(job)
    assert job.manifest is not None
    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="old-decision.txt").first()
    assert doc is not None
    assert doc.classification == KnowledgeClassification.decisions
    assert doc.active_truth_status == ActiveTruthStatus.superseded


@pytest.mark.asyncio
async def test_manifest_checksum_mismatch_rejects_that_file_only(db_session, make_verified_user):
    """DEL 2's "verifiera checksummor": if manifest.json declares an expected checksum for a
    file and the file's real content doesn't match it, that one file is rejected — the rest
    of the package still imports normally."""
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id)
    manifest = (
        b'{"documents": [{"file": "a.txt", "checksum": "' + b"0" * 64 + b'"}]}'
    )
    raw = _make_zip({"manifest.json": manifest, "a.txt": b"riktigt innehall", "b.txt": b"annat innehall"})

    await run_import_job(db_session, job.id, user.id, raw, "checksum-mismatch.zip")

    db_session.refresh(job)
    assert job.status == ImportJobStatus.partial
    assert job.succeeded_count == 1
    assert job.failed_count == 1
    docs = db_session.query(Document).filter_by(uploaded_by=user.id).all()
    assert {d.original_filename for d in docs} == {"b.txt"}
