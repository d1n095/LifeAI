"""Integration tests for app/rag/library_import.py — the orchestrator tying ZIP validation,
text extraction, chunking/embedding and ImportJob progress together. Runs against a real
local Postgres (RLS included) and a deterministic fake embedding provider — see
conftest.py's `db_session`/`make_verified_user` fixtures and the `_fake_embed` monkeypatch
below. No real AI-provider key is used anywhere in this file.

Life Library durable-worker package: run_import_job() no longer takes `raw`/`filename`
directly — it reads the original from app/storage/ via ImportJob.source_storage_key (see
that module's docstring). `_make_job` below does the storage write a real
POST /api/library/import would have already done, so every test still just calls
`run_import_job(db_session, job.id, user.id)`."""

import io
import uuid
import zipfile

import pytest

from app.config import get_settings
from app.models.document import ActiveTruthStatus, Document, IndexStatus, KnowledgeClassification
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_version import KnowledgeVersion
from app.rag.library_import import run_import_job
from app.storage import get_storage

EMBEDDING_DIM = get_settings().embedding_dim


@pytest.fixture(autouse=True)
def _fake_embedding_provider(monkeypatch):
    """Deterministic, offline embedding — no real provider call, no API key, matches the
    dimension pgvector's column requires (see conftest/other RAG tests for the same
    pattern)."""
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_embed(self, texts, model):
        return [[0.01 * (i + 1)] * EMBEDDING_DIM for i, _ in enumerate(texts)]

    # Import now also runs claim extraction (app/rag/claims.py, STEG 10) right after
    # indexing, which calls the chat provider too — see the identical comment in
    # test_library_routes.py's fixture for why this mock is required here as well.
    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 2})

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


def _store(raw: bytes):
    """Writes `raw` to the real (test) storage backend, exactly like POST
    /api/library/import does before this module ever runs — returns the StoredBlob."""
    return get_storage().write_stream(_read_chunk_for(raw), max_bytes=max(len(raw), 1))


def _make_job(db_session, owner_id, raw: bytes = b"", filename: str = "test.txt") -> ImportJob:
    from sqlalchemy import text

    from app.request_context import current_user_id as current_user_id_var

    # Sets the contextvar too, not just the raw SQL setting — a caller that commits again
    # AFTER this helper returns (starting a new transaction) needs app/db.py's after_begin
    # listener to re-apply RLS scoping, which only reads the contextvar, not any previously
    # executed raw SET LOCAL (see app/rag/library_import.py's _set_rls_owner docstring for
    # the identical, previously-diagnosed bug class). Found as a real bug in this exact
    # helper: a test that reused `db_session` for a second commit after _make_job (setting
    # job.max_attempts before calling run_import_job) hit a StaleDataError because the
    # knowledge_import_jobs RLS policy silently filtered out the UPDATE.
    current_user_id_var.set(str(owner_id))
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
    blob = _store(raw)
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
async def test_single_text_file_import_succeeds(db_session, make_verified_user):
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, b"Detta ar ett testdokument om MainAI.", "test.txt")

    await run_import_job(db_session, job.id, user.id)

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
    # DEL 1 (persistent original storage): the document's own durable blob, verified
    assert doc.storage_key is not None
    assert doc.stored_at is not None
    assert get_storage().verify(doc.storage_key, expected_sha256=doc.checksum, expected_size=doc.size_bytes)

    version = db_session.query(KnowledgeVersion).filter_by(source_id=doc.id).first()
    assert version is not None
    assert version.checksum == doc.checksum


@pytest.mark.asyncio
async def test_zip_package_imports_every_supported_file(db_session, make_verified_user):
    user, _ = make_verified_user()
    raw = _make_zip({"a.txt": b"Innehall A", "b.md": b"# Innehall B", "sub/c.txt": b"Innehall C"})
    job = _make_job(db_session, user.id, raw, "package.zip")

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed
    assert job.succeeded_count == 3
    docs = db_session.query(Document).filter_by(uploaded_by=user.id).all()
    assert len(docs) == 3
    assert all(d.status == IndexStatus.indexed for d in docs)
    assert all(d.storage_key is not None for d in docs)


@pytest.mark.asyncio
async def test_partial_failure_does_not_abort_the_whole_package(db_session, make_verified_user):
    """One unsupported/skipped file inside an otherwise-good package must not stop the
    others from importing — DEL 3's "fel i en fil far inte korrumpera hela paketet"."""
    user, _ = make_verified_user()
    raw = _make_zip({"good.txt": b"bra innehall", "unsupported.exe": b"MZfake", "also-good.md": b"# bra"})
    job = _make_job(db_session, user.id, raw, "mixed.zip")

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed  # skipped files aren't "failures"
    assert job.succeeded_count == 2
    assert job.skipped_count == 1
    docs = db_session.query(Document).filter_by(uploaded_by=user.id).all()
    assert {d.original_filename for d in docs} == {"good.txt", "also-good.md"}


@pytest.mark.asyncio
async def test_media_files_bundled_in_a_zip_are_skipped_not_silently_mishandled(db_session, make_verified_user):
    """STEG 12's own docstring (app/rag/media_import.py) claims audio/video bundled inside a
    ZIP package is unsupported in v1 — zip_import.py's ALLOWED_EXTENSIONS was never extended
    for .mp3/.mp4, so those entries should be rejected at the zip-validation stage, before
    _import_one_file's media_kind dispatch ever sees them. Proven here end-to-end through the
    real import pipeline rather than just asserted in a comment: the package still completes
    successfully for its supported file, and the media entry is skipped cleanly (not
    misinterpreted as some other file type, not silently dropped without a reason)."""
    user, _ = make_verified_user()
    valid_mp3 = b"ID3\x03\x00\x00\x00\x00\x00\x00" + b"\x00" * 64
    raw = _make_zip({"notes.txt": b"vanligt textinnehall", "recording.mp3": valid_mp3})
    job = _make_job(db_session, user.id, raw, "mixed-media.zip")

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed
    assert job.succeeded_count == 1
    assert job.skipped_count == 1
    skipped_entry = next(f for f in job.file_results if f["filename"] == "recording.mp3")
    assert skipped_entry["status"] == "skipped"
    assert "stöds inte" in skipped_entry["reason"]
    docs = db_session.query(Document).filter_by(uploaded_by=user.id).all()
    assert {d.original_filename for d in docs} == {"notes.txt"}


@pytest.mark.asyncio
async def test_zip_security_violation_fails_the_job_cleanly_not_a_crash(db_session, make_verified_user):
    """run_import_job (a single attempt) RAISES ZipSecurityError by design, so
    app/worker.py's retry wrapper (process_claimed_job) can classify it as permanent — see
    that function's docstring. Driving it through process_claimed_job here, like the other
    permanent-failure tests below, is what actually exercises "the job ends up cleanly
    failed, not an unhandled exception the caller has to deal with"."""
    from app.worker import process_claimed_job

    user, _ = make_verified_user()
    raw = _make_zip({"../../etc/passwd": b"pwned"})
    job = _make_job(db_session, user.id, raw, "evil.zip")

    await process_claimed_job(db_session, job.id, user.id)

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
    job1 = _make_job(db_session, user.id, b"samma innehall varje gang", "same.txt")
    await run_import_job(db_session, job1.id, user.id)
    db_session.refresh(job1)
    assert job1.succeeded_count == 1

    job2 = _make_job(db_session, user.id, b"samma innehall varje gang", "same-igen.txt")
    await run_import_job(db_session, job2.id, user.id)
    db_session.refresh(job2)

    assert job2.status == ImportJobStatus.completed
    assert job2.succeeded_count == 0
    assert job2.skipped_count == 1  # counted as duplicate, not a failure
    assert db_session.query(Document).filter_by(uploaded_by=user.id).count() == 1


@pytest.mark.asyncio
async def test_manifest_classification_and_truth_status_are_applied(db_session, make_verified_user):
    user, _ = make_verified_user()
    manifest = (
        b'{"package": "test", "documents": ['
        b'{"file": "old-decision.txt", "classification": "decisions", "active_truth_status": "superseded"}'
        b"]}"
    )
    raw = _make_zip({"manifest.json": manifest, "old-decision.txt": b"Det gamla beslutet var X."})
    job = _make_job(db_session, user.id, raw, "with-manifest.zip")

    await run_import_job(db_session, job.id, user.id)

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
    manifest = b'{"documents": [{"file": "a.txt", "checksum": "' + b"0" * 64 + b'"}]}'
    raw = _make_zip({"manifest.json": manifest, "a.txt": b"riktigt innehall", "b.txt": b"annat innehall"})
    job = _make_job(db_session, user.id, raw, "checksum-mismatch.zip")

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.partial
    assert job.succeeded_count == 1
    assert job.failed_count == 1
    docs = db_session.query(Document).filter_by(uploaded_by=user.id).all()
    assert {d.original_filename for d in docs} == {"b.txt"}


# --- STEG 11: retry/backoff + distributed lock integration ---


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    """Keeps retry tests fast — the backoff POLICY itself is unit-tested for real timing in
    test_job_retry.py; here only the RETRY BEHAVIOR (does it retry, how many times, does it
    give up) matters."""
    monkeypatch.setattr("app.worker.compute_backoff_seconds", lambda attempt: 0.01)


@pytest.mark.asyncio
async def test_transient_failure_is_retried_and_eventually_succeeds(db_session, make_verified_user, monkeypatch):
    """A per-file extraction error (extract_text raising) is caught INSIDE
    _import_one_file and turned into a FileOutcome — it never reaches the job-level retry
    loop (see the test below, which documents that explicitly). A genuinely job-level
    transient failure — the orchestration itself failing between files, e.g. a dropped DB
    connection — is what app/worker.py's process_claimed_job retry loop actually reacts to,
    so this test injects the failure at that level (patching _import_one_file itself) rather
    than deeper inside it, and drives the retry loop directly (process_claimed_job) instead
    of the single-attempt run_import_job."""
    from app.rag import library_import as li
    from app.worker import process_claimed_job

    user, _ = make_verified_user()
    raw = _make_zip({"a.txt": b"Innehall A"})
    job = _make_job(db_session, user.id, raw, "flaky.zip")

    calls = {"n": 0}
    real_import_one_file = li._import_one_file

    async def _flaky_import_one_file(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("simulerat tillfälligt nätverksfel")
        return await real_import_one_file(*args, **kwargs)

    monkeypatch.setattr(li, "_import_one_file", _flaky_import_one_file)

    await process_claimed_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed
    assert job.attempt_count == 1  # one retry happened
    assert job.last_failure_transient is True
    assert job.succeeded_count == 1


@pytest.mark.asyncio
async def test_permanent_failure_is_not_retried(db_session, make_verified_user):
    """A malformed ZIP (ZipSecurityError, classified permanent — see app/jobs/retry.py)
    must fail immediately, not burn through retry attempts on something retrying can never
    fix."""
    from app.worker import process_claimed_job

    user, _ = make_verified_user()
    raw = _make_zip({"../../etc/passwd": b"pwned"})
    job = _make_job(db_session, user.id, raw, "evil.zip")

    await process_claimed_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.failed
    assert job.attempt_count == 0  # never retried
    assert job.last_failure_transient is False


@pytest.mark.asyncio
async def test_a_persistently_flaky_extractor_degrades_to_a_failed_file_not_a_job_level_retry(db_session, make_verified_user, monkeypatch):
    """A per-file extraction error never reaches the job-level retry loop at all — it's
    caught inside _import_one_file and turned into a FileOutcome(status="failed"), matching
    this codebase's established "one file's error must not corrupt the whole batch"
    principle. attempt_count staying at 0 here is the actual proof that retry/backoff is
    scoped to job-orchestration failures, not per-file ones (contrast with the test below,
    which fails at the job-orchestration level and DOES retry)."""
    from app.rag import library_import as li
    from app.worker import process_claimed_job

    user, _ = make_verified_user()
    raw = _make_zip({"a.txt": b"Innehall A"})
    job = _make_job(db_session, user.id, raw, "always-flaky.zip")

    def _always_flaky(filename, content):
        raise ConnectionError("alltid nätverksfel")

    monkeypatch.setattr(li, "extract_text", _always_flaky)

    await process_claimed_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.failed  # the single file failed, and nothing else succeeded
    assert job.failed_count == 1
    assert job.attempt_count == 0  # never reached the job-level retry loop


@pytest.mark.asyncio
async def test_a_genuinely_job_level_transient_error_exhausts_attempts_and_fails(db_session, make_verified_user, monkeypatch):
    """Unlike per-file extraction errors (caught locally, see the test above), a failure in
    the job-level orchestration itself (e.g. a dropped DB connection between file-loop
    iterations) must be retried up to max_attempts and then permanently fail."""
    from app.rag import library_import as li
    from app.worker import process_claimed_job

    user, _ = make_verified_user()
    raw = _make_zip({"a.txt": b"Innehall A"})
    job = _make_job(db_session, user.id, raw, "job-level-flaky.zip")
    job.max_attempts = 2
    db_session.add(job)
    db_session.commit()

    async def _always_raises(*args, **kwargs):
        raise ConnectionError("databasen svarar inte")

    monkeypatch.setattr(li, "_import_one_file", _always_raises)

    await process_claimed_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.failed
    assert job.attempt_count == job.max_attempts - 1
    assert job.last_failure_transient is True


# --- Life Library upload consolidation package: granular IndexStatus modeling ---


@pytest.mark.asyncio
async def test_extraction_failure_preserves_the_document_row_instead_of_losing_the_upload(
    db_session, make_verified_user, monkeypatch
):
    """Before this fix, extract_text() raising meant _import_one_file returned a FileOutcome
    with no source_id at all — no Document row was ever created for that file, so the
    founder had no way to see the upload was even received. An embedding/extraction outage
    must never make received material disappear: the Document row now exists (created
    before extraction is attempted) and is marked IndexStatus.failed afterwards, preserving
    the checksum/original_filename/received-at record even though indexing never happened —
    and, per the durable-worker package, the original bytes are STILL durably stored and
    verifiable even though extraction never succeeded."""
    from app.rag import library_import as li

    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, b"nagot innehall", "broken.txt")

    def _always_fails(filename, content):
        raise ValueError("kunde inte tolka filen")

    monkeypatch.setattr(li, "extract_text", _always_fails)

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.failed
    assert job.file_results[0]["source_id"] is not None

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="broken.txt").first()
    assert doc is not None
    assert doc.status == IndexStatus.failed
    assert doc.error_message is not None
    assert doc.checksum is not None
    # The original survives even though extraction never got anywhere near succeeding.
    assert doc.storage_key is not None
    assert get_storage().verify(doc.storage_key, expected_sha256=doc.checksum)


@pytest.mark.asyncio
async def test_document_status_passes_through_the_full_granular_pipeline_before_indexed(
    db_session, make_verified_user, monkeypatch
):
    """Life Library durable-worker package: the pipeline must expose granular, steppable
    status (original_storing -> original_stored -> extracting -> extracted -> embedding ->
    indexed) rather than jumping straight from received to indexed, so a restart mid-pipeline
    can resume from exactly where it left off without the UI needing to change shape."""
    from app.providers.openai_provider import OpenAIProvider
    from app.rag import library_import as li

    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, b"text att extrahera och embedda", "granular.txt")

    observed: dict[str, str | None] = {}
    real_extract = li.extract_text
    real_embed = OpenAIProvider.embed
    real_store_bytes = li._store_bytes

    def _current_status():
        doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="granular.txt").first()
        return doc.status.value if doc else None

    def _observing_store_bytes(storage, content, *, max_bytes):
        observed["status_during_store"] = _current_status()
        return real_store_bytes(storage, content, max_bytes=max_bytes)

    def _observing_extract(filename, content):
        observed["status_during_extract"] = _current_status()
        return real_extract(filename, content)

    async def _observing_embed(self, texts, model):
        observed["status_during_embed"] = _current_status()
        return await real_embed(self, texts, model)

    monkeypatch.setattr(li, "_store_bytes", _observing_store_bytes)
    monkeypatch.setattr(li, "extract_text", _observing_extract)
    monkeypatch.setattr(OpenAIProvider, "embed", _observing_embed)

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed
    assert observed["status_during_store"] == IndexStatus.original_storing.value
    assert observed["status_during_extract"] == IndexStatus.extracting.value
    assert observed["status_during_embed"] == IndexStatus.embedding.value

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="granular.txt").first()
    assert doc.status == IndexStatus.indexed
    assert doc.storage_key is not None
    assert doc.stored_at is not None


@pytest.mark.asyncio
async def test_concurrent_duplicate_import_is_protected_by_the_distributed_lock(
    db_session, superuser_db, make_verified_user, monkeypatch
):
    """The "two concurrent workers/import attempts" scenario STEG 11 explicitly asks to be
    tested, at the job-orchestration level (not just the lock primitive in isolation, see
    test_job_lock.py) — two jobs sharing the same source_checksum, run concurrently, must
    not both proceed to do the actual import work.

    The module-level _fake_embedding_provider fixture returns instantly with no real
    suspension point, which means asyncio.gather()-ing two run_import_job() coroutines does
    NOT actually interleave them: the first task runs synchronously start-to-finish
    (including acquiring AND releasing the lock in its `finally` block) before the second
    task's __step() ever gets scheduled, so there is nothing left to race against. A single
    `await asyncio.sleep(...)` genuinely yields control back to the event loop, which is what
    real I/O (a real embedding API call) would also do — so this override makes the fake
    provider behave like a real one just long enough for the second job to reach its own
    lock.acquire() call while the first job still holds the lock, producing a genuine race."""
    import asyncio

    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine
    from app.providers.openai_provider import OpenAIProvider

    EMBED_DIM = get_settings().embedding_dim

    async def _slow_embed(self, texts, model):
        await asyncio.sleep(0.2)
        return [[0.01 * (i + 1)] * EMBED_DIM for i, _ in enumerate(texts)]

    monkeypatch.setattr(OpenAIProvider, "embed", _slow_embed)

    user, _ = make_verified_user()
    raw = _make_zip({"a.txt": b"Innehall A"})
    blob = _store(raw)
    session_factory = sessionmaker(bind=migration_engine)

    def _make_job_with_checksum(owner_id, filename):
        from sqlalchemy import text as sa_text

        session = session_factory()
        session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})
        job = ImportJob(
            owner_id=owner_id,
            status=ImportJobStatus.pending,
            source_filename=filename,
            source_checksum=blob.sha256,
            source_storage_key=blob.storage_key,
            source_size_bytes=blob.size_bytes,
        )
        session.add(job)
        session.commit()
        return session, job

    session_a, job_a = _make_job_with_checksum(user.id, "race-a.zip")
    session_b, job_b = _make_job_with_checksum(user.id, "race-b.zip")

    results = await asyncio.gather(
        run_import_job(session_a, job_a.id, user.id),
        run_import_job(session_b, job_b.id, user.id),
    )

    session_a.refresh(job_a)
    session_b.refresh(job_b)
    statuses = {job_a.status, job_b.status}
    # Exactly one of the two jobs actually did the import work; the other was rejected by
    # the distributed lock rather than both racing to create duplicate Documents.
    lock_rejected = [j for j in (job_a, job_b) if j.failure_reason and "jobblås" in j.failure_reason.lower()]
    assert len(lock_rejected) == 1
    assert ImportJobStatus.completed in statuses

    session_a.close()
    session_b.close()
