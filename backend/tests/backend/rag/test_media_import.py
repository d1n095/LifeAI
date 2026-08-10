"""STEG 12: audio/video import v1 — app/providers/transcription.py,
app/rag/media_import.py, and the full import pipeline wired through
app/rag/library_import.py. Runs against real local Postgres (RLS included); the
transcription provider is monkeypatched with real, deterministic segment data for the
integration tests (the exact same pattern this codebase already uses for
OpenAIProvider.chat/.embed — see tests/backend/rag/test_library_import.py's _fake_chat/_fake_embed), never a
real network call."""

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.document import Document, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.providers.transcription import (
    MockTranscriptionProvider,
    TranscriptResult,
    TranscriptSegment,
)
from app.rag.media_import import (
    MediaImportError,
    chunk_segments,
    media_kind_for,
    validate_media_bytes,
)
from app.rag.library_import import run_import_job
from app.rag.vector_store import hybrid_search
from app.storage import get_storage

EMBEDDING_DIM = get_settings().embedding_dim

VALID_MP3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
VALID_MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _fake_embedding_provider(monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.01 * (i + 1)] * EMBEDDING_DIM for i, _ in enumerate(texts)]

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 2})

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)


def _set_rls_user(db_session, owner_id) -> None:
    from app.request_context import current_user_id as current_user_id_var

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


def _make_job(db_session, owner_id, raw: bytes = b"", filename: str = "test.mp3"):
    """Durable-worker package: run_import_job() reads the original from app/storage/ via
    ImportJob.source_storage_key rather than taking raw bytes directly — this helper does the
    storage write a real POST /api/library/import would already have done (see
    tests/backend/rag/test_library_import.py's identical helper)."""
    from app.models.import_job import ImportJob, ImportJobStatus

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


# --- media_kind_for / validate_media_bytes (pure, no DB) ---


def test_media_kind_for_recognizes_mp3_and_mp4():
    assert media_kind_for("lecture.mp3") == "audio"
    assert media_kind_for("LECTURE.MP3") == "audio"
    assert media_kind_for("clip.mp4") == "video"
    assert media_kind_for("notes.pdf") is None
    assert media_kind_for("noextension") is None


def test_validate_media_bytes_accepts_valid_mp3():
    validate_media_bytes("a.mp3", VALID_MP3, "audio")


def test_validate_media_bytes_accepts_valid_mp3_bare_frame_sync():
    validate_media_bytes("a.mp3", b"\xff\xfb\x90\x00" + b"\x00" * 64, "audio")


def test_validate_media_bytes_accepts_valid_mp4():
    validate_media_bytes("a.mp4", VALID_MP4, "video")


def test_validate_media_bytes_rejects_mismatched_signature():
    with pytest.raises(MediaImportError, match="MP3-signatur"):
        validate_media_bytes("a.mp3", b"not actually an mp3 file at all", "audio")
    with pytest.raises(MediaImportError, match="MP4-signatur"):
        validate_media_bytes("a.mp4", b"not actually an mp4 file at all", "video")


def test_validate_media_bytes_rejects_empty_file():
    with pytest.raises(MediaImportError, match="tom"):
        validate_media_bytes("a.mp3", b"", "audio")


def test_validate_media_bytes_rejects_oversized_file(monkeypatch):
    # Lowers the cap rather than allocating a real 60MB+ buffer in the test process.
    from app.rag import media_import as media_import_module

    monkeypatch.setattr(media_import_module, "MAX_MEDIA_FILE_BYTES", 10)
    with pytest.raises(MediaImportError, match="för stor"):
        validate_media_bytes("a.mp3", VALID_MP3, "audio")


# --- chunk_segments (pure, no DB) ---


def test_chunk_segments_groups_short_transcript_into_one_chunk():
    segments = [
        TranscriptSegment(0.0, 5.0, "Hej det här är en kort mening."),
        TranscriptSegment(5.0, 9.0, "Och en till mening direkt efter."),
    ]
    chunks = chunk_segments(segments, chunk_size=800)
    assert len(chunks) == 1
    assert chunks[0].start_seconds == 0.0
    assert chunks[0].end_seconds == 9.0
    assert "kort mening" in chunks[0].text
    assert "till mening" in chunks[0].text


def test_chunk_segments_splits_on_word_count_and_preserves_timestamp_span():
    # Each segment is exactly 6 words; chunk_size=13 lets the first two fit together
    # (6+6=12 <= 13) but not a third (12+6=18 > 13), forcing a split there.
    segments = [
        TranscriptSegment(0.0, 2.0, "ett two three four five six"),
        TranscriptSegment(2.0, 4.0, "seven eight nine ten eleven twelve"),
        TranscriptSegment(4.0, 6.0, "thirteen fourteen fifteen sixteen seventeen eighteen"),
    ]
    chunks = chunk_segments(segments, chunk_size=13)
    assert len(chunks) == 2
    assert chunks[0].start_seconds == 0.0
    assert chunks[0].end_seconds == 4.0  # spans the first two grouped segments
    assert chunks[1].start_seconds == 4.0
    assert chunks[1].end_seconds == 6.0


def test_chunk_segments_skips_blank_segments():
    segments = [
        TranscriptSegment(0.0, 1.0, "   "),
        TranscriptSegment(1.0, 3.0, "riktigt innehåll här"),
    ]
    chunks = chunk_segments(segments)
    assert len(chunks) == 1
    assert chunks[0].start_seconds == 1.0


def test_chunk_segments_empty_list_yields_no_chunks():
    assert chunk_segments([]) == []


# --- MockTranscriptionProvider (pure async, no DB) ---


@pytest.mark.asyncio
async def test_mock_transcription_provider_is_always_configured():
    assert MockTranscriptionProvider().is_configured() is True


@pytest.mark.asyncio
async def test_mock_transcription_provider_produces_one_honest_placeholder_segment():
    provider = MockTranscriptionProvider()
    result = await provider.transcribe(b"x" * 32_000, "lecture.mp3", "audio")
    assert len(result.segments) == 1
    assert result.segments[0].start_seconds == 0.0
    assert result.duration_seconds > 0
    assert result.provider == "mock"
    assert "inte tillgänglig" in result.segments[0].text


@pytest.mark.asyncio
async def test_mock_transcription_provider_duration_scales_with_file_size():
    provider = MockTranscriptionProvider()
    short = await provider.transcribe(b"x" * 1_000, "a.mp3", "audio")
    long = await provider.transcribe(b"x" * 100_000, "a.mp3", "audio")
    assert long.duration_seconds > short.duration_seconds


# --- Full pipeline integration (real Postgres, monkeypatched transcription provider) ---


@pytest.mark.asyncio
async def test_mp3_import_produces_timestamped_searchable_chunks(db_session, make_verified_user, monkeypatch):
    """The vertical slice STEG 12 asks for: upload -> transcript -> timestamped chunks ->
    indexed -> searchable, with each chunk's start/end coming straight from the transcript,
    not guessed."""
    user, _ = make_verified_user()

    async def _fake_transcribe(self, raw, filename, media_kind):
        return TranscriptResult(
            segments=[
                TranscriptSegment(0.0, 4.5, "Grundaren pratar om produktvisionen för MainAI."),
                TranscriptSegment(30.0, 42.0, "Senare i inspelningen nämns budgeten för nästa kvartal."),
            ],
            duration_seconds=42.0,
            provider="mock",
            model="placeholder-v1",
        )

    monkeypatch.setattr(MockTranscriptionProvider, "transcribe", _fake_transcribe)

    job = _make_job(db_session, user.id, VALID_MP3, "founder-talk.mp3")
    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status.value == "completed"
    assert job.succeeded_count == 1

    document = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="founder-talk.mp3").one()
    assert document.status == IndexStatus.indexed
    assert document.media_type == "audio/mpeg"
    assert document.transcript_provider == "mock"
    assert document.media_duration_seconds == 42.0
    assert document.chunk_count == 1  # both short segments fit in one chunk_size=800 window

    chunk = db_session.query(DocumentChunk).filter_by(document_id=document.id).one()
    assert chunk.start_seconds == 0.0
    assert chunk.end_seconds == 42.0  # spans both grouped segments
    assert "produktvisionen" in chunk.text
    assert "budgeten" in chunk.text

    # Searchable — hybrid_search's text channel finds the exact term, and the hit carries
    # the chunk's timestamp range through so a citation can open the right moment.
    _set_rls_user(db_session, user.id)
    hits = hybrid_search(db_session, user.id, [0.01] * EMBEDDING_DIM, "produktvisionen")
    assert len(hits) == 1
    assert hits[0]["start_seconds"] == 0.0
    assert hits[0]["end_seconds"] == 42.0
    assert hits[0]["text_match"] is True


@pytest.mark.asyncio
async def test_mp4_import_dispatches_to_media_pipeline_not_text_extraction(db_session, make_verified_user, monkeypatch):
    user, _ = make_verified_user()

    async def _fake_transcribe(self, raw, filename, media_kind):
        assert media_kind == "video"
        return TranscriptResult(
            segments=[TranscriptSegment(0.0, 10.0, "Video-transkription fungerar.")],
            duration_seconds=10.0,
            provider="mock",
            model="placeholder-v1",
        )

    monkeypatch.setattr(MockTranscriptionProvider, "transcribe", _fake_transcribe)

    job = _make_job(db_session, user.id, VALID_MP4, "demo.mp4")
    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status.value == "completed"
    document = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="demo.mp4").one()
    assert document.media_type == "video/mp4"
    assert document.media_duration_seconds == 10.0


@pytest.mark.asyncio
async def test_invalid_media_signature_fails_the_file_not_the_whole_job(db_session, make_verified_user):
    """MIME/size check (STEG 12's own first pipeline step) rejects one bad file as a
    per-file failure — mirrors ZipSecurityError's per-entry counterpart, never a job-level
    crash (see app/rag/media_import.py's MediaImportError docstring)."""
    user, _ = make_verified_user()
    fake_mp3 = b"this is not an mp3 despite the extension" + b"\x00" * 32
    job = _make_job(db_session, user.id, fake_mp3, "fake.mp3")

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status.value == "failed"
    assert job.failed_count == 1
    assert "signatur" in (job.failure_reason or "").lower() or any(
        "signatur" in (r.get("reason") or "").lower() for r in (job.file_results or [])
    )


@pytest.mark.asyncio
async def test_media_import_never_makes_the_placeholder_transcript_look_like_real_content(db_session, make_verified_user):
    """Without monkeypatching transcribe(), the real (only) shipped provider — MockTranscriptionProvider
    — runs for real. It must still succeed end-to-end (the pipeline itself is fully wired),
    but its content must stay honestly labeled as a placeholder, never invented speech."""
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, VALID_MP3, "unmocked.mp3")

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status.value == "completed"
    document = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="unmocked.mp3").one()
    chunk = db_session.query(DocumentChunk).filter_by(document_id=document.id).one()
    assert "inte tillgänglig" in chunk.text


# --- Crash/resume recovery for media documents (2026-07-28 permanent fix) ------------------
#
# app/models/document.py's RESUMABLE_INDEX_STATUSES applies to every Document regardless of
# type, including MP3/MP4 stuck at `extracting`/`embedding` after a worker crash — but a media
# document never went through extract_text()/index_document() on its first import (see
# app/rag/library_import.py's _import_one_file `if media_kind is None: ... else: ...` split).
# app/rag/library_import.py's _resume_incomplete_document now dispatches on
# media_import.media_kind_for(filename) so a resumed media document goes through the SAME
# media_import.validate_media_bytes()/index_media_document() pipeline a first import uses,
# instead of being misrouted into text extraction (which would misclassify it as
# extraction_failed — binary media isn't parseable text).


@pytest.mark.asyncio
async def test_worker_crash_mid_media_embedding_is_resumed_to_indexed_before_job_completes(
    db_session, superuser_db, make_verified_user, monkeypatch
):
    """Mirrors tests/backend/providers/test_provider_verification.py's equivalent text-pipeline test: a REAL worker
    process crash (a BaseException that is deliberately NOT an Exception subclass, caught by
    nothing) mid the real chunk-embedding call for an MP3. Proves the reclaimed job resumes the
    SAME Document row through the MEDIA pipeline (not text extraction), reaches `indexed` with
    real, timestamped chunks, and the job only completes once that has actually happened."""
    from datetime import datetime, timedelta

    from app.models.import_job import ImportJob, ImportJobStatus
    from app.providers.openai_provider import OpenAIProvider
    from app.providers.verification import VERIFICATION_PROBE_TEXT
    from app.worker import Worker

    class _SimulatedProcessKill(BaseException):
        """Deliberately NOT an Exception subclass — a real SIGKILL is caught by nothing,
        including index_media_document's own `except Exception` and app/worker.py's
        process_claimed_job's `except Exception`."""

    async def _fake_transcribe(self, raw, filename, media_kind):
        return TranscriptResult(
            segments=[TranscriptSegment(0.0, 5.0, "Grundaren spelar in en anteckning.")],
            duration_seconds=5.0,
            provider="mock",
            model="placeholder-v1",
        )

    async def _crash_mid_embed(self, texts, model, **kwargs):
        # The pre-flight verification probe (app/worker.py's _requeue_blocked_jobs, called
        # BEFORE a job is even claimed) must keep succeeding — only the REAL chunk-embedding
        # call inside index_media_document simulates the crash.
        if texts == [VERIFICATION_PROBE_TEXT]:
            return [[0.01] * EMBEDDING_DIM]
        raise _SimulatedProcessKill("simulated hard process kill mid-embed()")

    monkeypatch.setattr(MockTranscriptionProvider, "transcribe", _fake_transcribe)
    monkeypatch.setattr(OpenAIProvider, "embed", _crash_mid_embed)

    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, VALID_MP3, "crash-mid-embedding.mp3")

    with pytest.raises(_SimulatedProcessKill):
        await Worker().run_once()

    superuser_db.expire_all()
    job_row = superuser_db.get(ImportJob, job.id)
    assert job_row.status == ImportJobStatus.running  # nothing ever finalized the job row

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="crash-mid-embedding.mp3").first()
    assert doc is not None
    assert doc.status == IndexStatus.embedding  # exactly where the crash left it
    stuck_doc_id = doc.id

    # Simulate the abandoned lease expiring, exactly like tests/backend/jobs/test_worker.py's reclaim test.
    job_row.lease_expires_at = datetime.utcnow() - timedelta(seconds=5)
    superuser_db.add(job_row)
    superuser_db.commit()

    async def _fake_embed_ok(self, texts, model, **kwargs):
        return [[0.03] * EMBEDDING_DIM for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed_ok)

    worked = await Worker().run_once()
    assert worked is True

    superuser_db.expire_all()
    job_row2 = superuser_db.get(ImportJob, job.id)
    assert job_row2.status == ImportJobStatus.completed  # only AFTER the document actually resolved
    assert job_row2.succeeded_count == 1

    db_session.expire_all()
    doc2 = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="crash-mid-embedding.mp3").first()
    assert doc2.id == stuck_doc_id  # same row, never duplicated
    assert doc2.status == IndexStatus.indexed
    assert doc2.chunk_count > 0

    chunk = db_session.query(DocumentChunk).filter_by(document_id=stuck_doc_id).one()
    assert chunk.start_seconds == 0.0
    assert chunk.end_seconds == 5.0  # a real timestamped media chunk, not plain text


@pytest.mark.asyncio
async def test_reconciliation_repairs_a_terminal_job_with_a_media_document_stuck_in_embedding(
    db_session, superuser_db, make_verified_user, monkeypatch
):
    """Regression test for the confirmed production incident shape, reproduced for a MEDIA
    document: a job already `completed`, a linked live MP3 Document already stuck at
    `embedding`, otherwise empty queue. Proves app/worker.py's _reconcile_orphaned_documents
    together with _resume_incomplete_document's media dispatch repair it automatically, with
    no manual intervention and no misrouting into text extraction."""
    from datetime import datetime

    from app.models.import_job import ImportJob, ImportJobStatus
    from app.rag.zip_import import sha256_bytes
    from app.worker import Worker

    async def _fake_transcribe(self, raw, filename, media_kind):
        return TranscriptResult(
            segments=[TranscriptSegment(0.0, 8.0, "En kort ljudanteckning som redan sades vara klar.")],
            duration_seconds=8.0,
            provider="mock",
            model="placeholder-v1",
        )

    monkeypatch.setattr(MockTranscriptionProvider, "transcribe", _fake_transcribe)

    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, VALID_MP3, "already-completed.mp3")

    doc = Document(
        uploaded_by=user.id,
        title="already-completed.mp3",
        original_filename="already-completed.mp3",
        media_type="audio/mpeg",
        checksum=sha256_bytes(VALID_MP3),  # must match what _import_one_file recomputes on resume
        status=IndexStatus.embedding,
        import_job_id=job.id,
    )
    db_session.add(doc)
    db_session.commit()
    stuck_doc_id = doc.id

    # The exact reported production shape: job already terminal ("Klar"), queue otherwise empty.
    job_row = superuser_db.get(ImportJob, job.id)
    job_row.status = ImportJobStatus.completed
    job_row.succeeded_count = 1
    job_row.completed_at = datetime.utcnow()
    superuser_db.add(job_row)
    superuser_db.commit()

    worked = await Worker().run_once()
    assert worked is True  # _reconcile_orphaned_documents reset the job to pending, then it was claimed

    superuser_db.expire_all()
    job_row2 = superuser_db.get(ImportJob, job.id)
    assert job_row2.status == ImportJobStatus.completed
    assert job_row2.succeeded_count == 1

    db_session.expire_all()
    doc2 = db_session.get(Document, stuck_doc_id)
    assert doc2.id == stuck_doc_id  # same row, never duplicated
    assert doc2.status == IndexStatus.indexed
    assert doc2.chunk_count > 0

    chunk = db_session.query(DocumentChunk).filter_by(document_id=stuck_doc_id).one()
    assert chunk.start_seconds == 0.0
    assert chunk.end_seconds == 8.0  # resumed through the media pipeline, not text extraction


# --- MediaUrlImport RLS isolation ---


def test_media_url_imports_isolated_between_owners(db_session, superuser_db, make_verified_user):
    from app.models.media_url_import import MediaUrlImport

    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(superuser_db, user_a.id)
    record = MediaUrlImport(owner_id=user_a.id, url="https://example.com/a", platform="generic")
    superuser_db.add(record)
    superuser_db.commit()

    _set_rls_user(db_session, user_b.id)
    assert db_session.query(MediaUrlImport).count() == 0

    _set_rls_user(db_session, user_a.id)
    assert db_session.query(MediaUrlImport).count() == 1
