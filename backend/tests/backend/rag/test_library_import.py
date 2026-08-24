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

import importlib.util
import io
import threading
import uuid
import zipfile
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.models.document import ActiveTruthStatus, Document, IndexStatus, KnowledgeClassification
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_version import KnowledgeVersion
from app.rag.library_import import _store_bytes_with_reference_lock, run_import_job
from app.storage import StorageError, get_storage
from app.storage.references import acquire_storage_key_lock, delete_if_unreferenced

EMBEDDING_DIM = get_settings().embedding_dim

_APPLY_RUNTIME_PRIVILEGES_PATH = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "security" / "apply_runtime_privileges.py"


def _load_apply_runtime_privileges():
    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True, scope="module")
def _narrow_privileges_before_this_module():
    """Pass 32: _store_bytes_with_reference_lock() now calls storage_key_still_referenced_
    global() (via app/storage/references.py's delete_if_unreferenced()/
    storage_key_still_referenced()), which mainai_app is only granted EXECUTE on via
    apply_runtime_privileges.py/ensure_app_role.py's shared privilege policy -- never
    automatically by tests/conftest.py's _test_database fixture's own blanket table/sequence
    GRANT ALL. Same fixture, same rationale, as tests/backend/test_project_memory.py's and
    tests/backend/rag/test_library_routes.py's identical ones (added there in Pass 30/31 for the same reason);
    this file never needed it before this pass, since _store_bytes() used to call
    storage.write_stream() directly with no reference check at all."""
    module = _load_apply_runtime_privileges()
    module.apply_and_verify(get_settings().database_url)


@pytest.fixture(autouse=True)
def _fake_embedding_provider(monkeypatch):
    """Deterministic, offline embedding — no real provider call, no API key, matches the
    dimension pgvector's column requires (see conftest/other RAG tests for the same
    pattern)."""
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_embed(self, texts, model, **kwargs):
        return [[0.01 * (i + 1)] * EMBEDDING_DIM for i, _ in enumerate(texts)]

    # Import now also runs claim extraction (app/rag/claims.py, STEG 10) right after
    # indexing, which calls the chat provider too — see the identical comment in
    # tests/backend/rag/test_library_routes.py's fixture for why this mock is required here as well.
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
    tests/backend/jobs/test_job_retry.py; here only the RETRY BEHAVIOR (does it retry, how many times, does it
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
    # P1: reclassified from the old undifferentiated IndexStatus.failed — see
    # docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.7.
    assert doc.status == IndexStatus.extraction_failed
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

    async def _observing_embed(self, texts, model, **kwargs):
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
    tests/backend/jobs/test_job_lock.py) — two jobs sharing the same source_checksum, run concurrently, must
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

    async def _slow_embed(self, texts, model, **kwargs):
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


# --- P2: nested ZIP provenance and encrypted-entry wiring through the real pipeline ---
# See tests/backend/rag/test_zip_import_security.py for the exhaustive zip_import.py-level
# coverage of nesting/budget/encryption itself; these confirm the result actually reaches
# ImportJob.file_results, KnowledgeVersion.raw_metadata and job-level counts correctly.


@pytest.mark.asyncio
async def test_nested_zip_file_is_indexed_with_archive_path_in_job_and_version_metadata(db_session, make_verified_user):
    user, _ = make_verified_user()
    inner = _make_zip({"contracts/lease.txt": b"Hyresavtal: 12 manader, 8500 kr/manad."})
    raw = _make_zip({"users/docs.zip": inner})
    job = _make_job(db_session, user.id, raw, "backup.zip")

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed
    assert job.succeeded_count == 1

    expected_path = "backup.zip!/users/docs.zip!/contracts/lease.txt"
    file_result = next(r for r in job.file_results if r["filename"] == "contracts/lease.txt")
    assert file_result["archive_path"] == expected_path
    assert file_result["archive_chain"][0]["filename"] == "backup.zip"
    assert file_result["archive_chain"][-1]["filename"] == "contracts/lease.txt"

    doc = db_session.query(Document).filter_by(uploaded_by=user.id).first()
    version = db_session.query(KnowledgeVersion).filter_by(source_id=doc.id).first()
    assert version.raw_metadata["archive_path"] == expected_path
    assert version.raw_metadata["archive_chain"] == file_result["archive_chain"]


@pytest.mark.asyncio
async def test_top_level_zip_file_still_has_no_archive_path_after_p2(db_session, make_verified_user):
    """Regression guard: a plain, non-nested ZIP package must behave byte-for-byte like
    before P2 — no archive_path/archive_chain anywhere for a top-level file."""
    raw = _make_zip({"a.txt": b"top level only, no nesting"})
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, raw, "package.zip")

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    file_result = job.file_results[0]
    assert file_result["archive_path"] is None
    assert file_result["archive_chain"] is None

    doc = db_session.query(Document).filter_by(uploaded_by=user.id).first()
    version = db_session.query(KnowledgeVersion).filter_by(source_id=doc.id).first()
    assert "archive_path" not in version.raw_metadata


@pytest.mark.asyncio
async def test_encrypted_entry_in_a_zip_is_reported_with_its_own_status_and_does_not_fail_the_job(db_session, make_verified_user):
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    if shutil.which("zip") is None:
        pytest.skip("system 'zip' binary not available to build a genuinely encrypted archive")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "secret.txt").write_bytes(b"classified content")
        archive_path = tmp_path / "vault.zip"
        subprocess.run(
            ["zip", "-P", "hunter2", "-r", str(archive_path), "secret.txt"],
            cwd=tmp_path,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        encrypted_inner = archive_path.read_bytes()

    raw = _make_zip({"vault.zip": encrypted_inner, "readable.txt": b"this one is fine"})
    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, raw, "package.zip")

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    # One real success (readable.txt) plus one encrypted entry that could never be
    # attempted — not a failure, so the job still completes rather than going `partial`.
    assert job.status == ImportJobStatus.completed
    assert job.succeeded_count == 1
    assert job.failed_count == 0
    encrypted_result = next(r for r in job.file_results if r["filename"] == "secret.txt")
    assert encrypted_result["status"] == "encrypted"
    assert "lösenordsskyddat" in encrypted_result["reason"].lower()
    # Never indexed as a Document — an encrypted entry is excluded from the batch entirely.
    assert db_session.query(Document).filter_by(uploaded_by=user.id).count() == 1


# --- S1A Pass 19 review: dual-write failure must roll back cleanly, not commit partial ------
# --- writes and must not corrupt the session for the rest of the job/job-completion steps. --


@pytest.mark.asyncio
async def test_first_import_claim_extraction_crash_after_flush_rolls_back_cleanly(db_session, make_verified_user, monkeypatch):
    """First-import call site (_import_one_file). Simulates the exact failure window the
    review flagged: extract_claims_for_document flushes a MemorySourceUnit/DocumentSourceUnit
    pair (uncommitted, SAVEPOINT-scoped) and THEN raises — proving the surrounding
    except-block's db.rollback() undoes that flush, the already-committed indexing result
    survives, the import still reports 'indexed' (claim extraction is best-effort), and the
    session is still usable for every commit run_import_job makes afterward (job-completion
    bookkeeping) — not left in PendingRollback."""
    from datetime import datetime, timezone

    from app.models.document_chunk import DocumentChunk
    from app.models.knowledge_claim import KnowledgeClaim
    from app.models.memory_source_unit import MemorySourceUnit, SnapshotStatus
    from app.rag import library_import as li
    from app.rag.memory_source import DocumentSourceLocator, get_or_create_memory_source_unit

    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, b"Text som extraheras och sen kraschar under claim-extraktion.", "crash.txt")

    async def _flush_then_crash(db, document, owner_id, version_id):
        chunk = db.query(DocumentChunk).filter_by(document_id=document.id, owner_id=owner_id).first()
        assert chunk is not None, "index_document must have already created chunks by this point"
        get_or_create_memory_source_unit(
            db,
            DocumentSourceLocator(
                owner_id=owner_id,
                document_id=document.id,
                version_id=None,
                chunk_id=chunk.id,
                observed_at=datetime.now(timezone.utc),
                content_text=chunk.text,
                snapshot_status=SnapshotStatus.exact,
            ),
        )
        raise RuntimeError("simulated crash after MSU/DSU flush, before claim commit")

    monkeypatch.setattr(li, "extract_claims_for_document", _flush_then_crash)

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed  # job-completion commit worked -- session was usable
    assert job.file_results[0]["status"] == "indexed"

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="crash.txt").first()
    assert doc is not None
    assert doc.status == IndexStatus.indexed  # indexing itself is unaffected by the claim-extraction crash

    assert db_session.query(MemorySourceUnit).filter_by(owner_id=user.id).count() == 0
    assert db_session.query(KnowledgeClaim).filter_by(owner_id=user.id).count() == 0

    # Session usable for a fresh query AND a fresh commit after the rollback.
    assert db_session.query(Document).filter_by(uploaded_by=user.id).count() >= 1
    db_session.add(job)
    db_session.commit()


@pytest.mark.asyncio
async def test_resume_claim_extraction_crash_after_flush_rolls_back_cleanly(db_session, make_verified_user, monkeypatch):
    """Second call site (_resume_incomplete_document) — a Document stuck at a RESUMABLE_
    INDEX_STATUSES status (worker died mid-pipeline on an earlier attempt) with an existing
    KnowledgeVersion already committed from that earlier attempt. Re-importing identical
    content resumes it in place. Same crash-after-flush simulation and same assertions as the
    first-import test above, proving the fix covers BOTH call sites, not just one."""
    from datetime import datetime, timezone

    from sqlalchemy import text as sa_text

    from app.models.document import DocumentSource
    from app.models.document_chunk import DocumentChunk
    from app.models.knowledge_claim import KnowledgeClaim
    from app.models.memory_source_unit import MemorySourceUnit, SnapshotStatus
    from app.rag import library_import as li
    from app.rag.memory_source import DocumentSourceLocator, get_or_create_memory_source_unit
    from app.rag.zip_import import sha256_bytes
    from app.request_context import current_user_id as current_user_id_var

    user, _ = make_verified_user()
    content = b"Text som ateruppas efter att workern krashade mitt i extraktionen."
    checksum = sha256_bytes(content)

    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    stuck_document = Document(
        title="resumed.txt",
        source=DocumentSource.upload,
        uploaded_by=user.id,
        checksum=checksum,
        original_filename="resumed.txt",
        status=IndexStatus.extracting,  # a RESUMABLE_INDEX_STATUSES member
    )
    db_session.add(stuck_document)
    db_session.commit()
    version = KnowledgeVersion(
        source_id=stuck_document.id, owner_id=user.id, version_number=1, checksum=checksum, extraction_version="v1"
    )
    db_session.add(version)
    db_session.commit()

    job = _make_job(db_session, user.id, content, "resumed.txt")

    async def _flush_then_crash(db, document, owner_id, version_id):
        chunk = db.query(DocumentChunk).filter_by(document_id=document.id, owner_id=owner_id).first()
        assert chunk is not None, "index_document must have already created chunks by this point"
        get_or_create_memory_source_unit(
            db,
            DocumentSourceLocator(
                owner_id=owner_id,
                document_id=document.id,
                version_id=None,
                chunk_id=chunk.id,
                observed_at=datetime.now(timezone.utc),
                content_text=chunk.text,
                snapshot_status=SnapshotStatus.exact,
            ),
        )
        raise RuntimeError("simulated crash after MSU/DSU flush, before claim commit")

    monkeypatch.setattr(li, "extract_claims_for_document", _flush_then_crash)

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    assert job.status == ImportJobStatus.completed
    assert job.file_results[0]["status"] == "indexed"

    db_session.refresh(stuck_document)
    assert stuck_document.status == IndexStatus.indexed  # resumed indexing succeeded despite the crash

    assert db_session.query(MemorySourceUnit).filter_by(owner_id=user.id).count() == 0
    assert db_session.query(KnowledgeClaim).filter_by(owner_id=user.id).count() == 0

    # Session usable for a fresh query AND a fresh commit after the rollback.
    assert db_session.query(Document).filter_by(uploaded_by=user.id).count() >= 1
    db_session.add(job)
    db_session.commit()


# --- Pass 32 (a seventh founder review round): close _store_bytes()'s missing lock gap ------
#
# _store_bytes() durably writes the worker's per-file blob BEFORE the surrounding Document
# row's storage_key is set and committed -- a founder review pointed out this was a genuine,
# registered-but-unfixed gap in KNOWN_STORAGE_WRITE_PATHS (app/storage/references.py):
# registering a writer as "known but still unsafe" prevents drift, but not data corruption.
# Fixed via _store_bytes_with_reference_lock() (same file), which wraps _store_bytes() in the
# same lock+verify+republish protocol store_content_with_reference_lock() already provides for
# Project Memory (Pass 31). Tests below are the founder's own lettering (F -- no deadlocks --
# is proven implicitly by every threaded test below completing without timing out; G -- the
# write-path registry no longer describing any persistent writer as NO LOCK -- is covered by
# tests/backend/storage/test_source_purge.py's existing write-path-registry drift test, unchanged by
# this pass except for this one entry's description).


def test_store_bytes_with_reference_lock_returns_a_verified_present_blob_in_the_ordinary_case(db_session):
    content = f"pass 32 test A: ordinary case {uuid.uuid4().hex}".encode()

    blob = _store_bytes_with_reference_lock(db_session, get_storage(), content, max_bytes=len(content) + 10)
    db_session.commit()  # releases the storage-key lock

    assert get_storage().exists(blob.storage_key) is True


def test_store_bytes_with_reference_lock_recovers_when_a_real_concurrent_purge_deletes_the_blob_first():
    """Test B (founder's lettering): a REAL two-thread, two-session race where the deleter
    genuinely wins the advisory lock FIRST and purges the (at that moment, correctly
    unreferenced) blob before the writer's own lock acquisition can even run -- mirrors
    tests/backend/test_project_memory.py's identical Pass 31 test for Project Memory's own
    writer, now proving the SAME recovery for the worker's per-file write."""
    content = f"pass 32 test B: real concurrent purge recovers via republish {uuid.uuid4().hex}".encode()
    storage_key = get_storage().write_stream(iter([content, b""]).__next__, max_bytes=len(content)).storage_key
    # Nothing references this key yet -- correctly unreferenced at this point, exactly like a
    # worker's blob before its own Document.storage_key is committed.

    deleter_holds_lock = threading.Event()
    writer_may_proceed = threading.Event()
    deleter_outcome = {}

    def _deleter_thread():
        db = SessionLocal()
        try:
            acquire_storage_key_lock(db, storage_key)
            deleter_holds_lock.set()
            writer_may_proceed.wait(timeout=5)
            deleter_outcome["outcome"] = delete_if_unreferenced(db, get_storage(), storage_key)
            db.commit()
        finally:
            db.close()

    t = threading.Thread(target=_deleter_thread)
    t.start()
    assert deleter_holds_lock.wait(timeout=5), "deleter thread never acquired the lock"
    writer_may_proceed.set()

    writer_db = SessionLocal()
    try:
        blob = _store_bytes_with_reference_lock(writer_db, get_storage(), content, max_bytes=len(content) + 10)
        writer_db.commit()
        assert blob.storage_key == storage_key
        assert get_storage().exists(storage_key) is True, (
            "_store_bytes_with_reference_lock returned successfully but the blob it just "
            "verified/republished isn't actually on disk"
        )
    finally:
        writer_db.close()

    t.join(timeout=5)
    assert not t.is_alive(), "deleter thread never completed -- possible deadlock"
    assert deleter_outcome["outcome"].name == "purged"


@pytest.mark.asyncio
async def test_run_import_job_leaves_no_storage_key_when_the_blob_cannot_be_recovered(db_session, make_verified_user, monkeypatch):
    """Test C (founder's lettering): if even republishing can't recover a vanished blob, the
    Document row must never end up referencing a missing blob -- through the REAL pipeline
    (run_import_job -> _import_one_file), not just the lower-level helper directly."""
    import app.rag.library_import as li

    user, _ = make_verified_user(email="pass32-c@example.com")
    job = _make_job(db_session, user.id, b"pass 32 test C content", "testc.txt")

    def _broken_store_bytes_with_reference_lock(db, storage, content, *, max_bytes):
        raise StorageError("simulated: blob vanished and could not be republished")

    monkeypatch.setattr(li, "_store_bytes_with_reference_lock", _broken_store_bytes_with_reference_lock)

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="testc.txt").first()
    assert doc is not None
    assert doc.status == IndexStatus.storage_failed
    assert doc.storage_key is None


def test_store_bytes_with_reference_lock_and_the_account_erasure_outbox_worker_never_race_unsafely(
    make_verified_user,
):
    """Test D: race write-under-lock against outbox cleanup. After retain-after-reference,
    the durable claim is Document.storage_key in the writer's same transaction — not
    retain-before-commit. Without that reference, writer-wins + empty commit correctly lets
    cleanup purge (crash-before-reference must stay reclaimable)."""
    import hashlib
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine
    from app.models.document import DocumentSource
    from app.models.storage_deletion_task import StorageDeletionTask
    from app.account.erasure import attempt_storage_deletion_task
    from app.request_context import current_user_id as current_user_id_var
    from app.storage.references import enqueue_rejected_upload_cleanup_task, retain_pending_rejected_upload_cleanup_tasks

    _AdminSession = sessionmaker(bind=migration_engine)
    user, _ = make_verified_user()

    for attempt in range(4):
        content = f"pass 32 test D attempt {attempt} {uuid.uuid4().hex}".encode()
        storage_key = get_storage().write_stream(iter([content, b""]).__next__, max_bytes=len(content)).storage_key
        operation_id = enqueue_rejected_upload_cleanup_task(storage_key)

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def _outbox_worker():
            db = _AdminSession()
            try:
                barrier.wait(timeout=5)
                task = db.query(StorageDeletionTask).filter_by(operation_id=operation_id, storage_key=storage_key).one()
                attempt_storage_deletion_task(db, task)
            except Exception as exc:  # noqa: BLE001 - captured for the assertion below
                errors.append(exc)
            finally:
                db.close()

        def _writer():
            db = SessionLocal()
            try:
                barrier.wait(timeout=5)
                current_user_id_var.set(str(user.id))
                db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
                blob = _store_bytes_with_reference_lock(db, get_storage(), content, max_bytes=len(content) + 10)
                db.add(
                    Document(
                        title=f"race-d-{attempt}.txt",
                        source=DocumentSource.upload,
                        uploaded_by=user.id,
                        checksum=hashlib.sha256(content).hexdigest(),
                        original_filename=f"race-d-{attempt}.txt",
                        status=IndexStatus.original_stored,
                        storage_key=blob.storage_key,
                        size_bytes=blob.size_bytes,
                    )
                )
                db.commit()
                retain_pending_rejected_upload_cleanup_tasks(blob.storage_key)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                db.close()

        t1 = threading.Thread(target=_outbox_worker)
        t2 = threading.Thread(target=_writer)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not t1.is_alive() and not t2.is_alive(), f"attempt {attempt}: a race participant never finished -- possible deadlock"
        assert errors == [], f"attempt {attempt}: unexpected exception(s): {errors}"
        assert get_storage().exists(storage_key) is True, (
            f"attempt {attempt}: writer with durable Document.storage_key must leave the blob on disk"
        )


def test_store_bytes_with_reference_lock_writer_wins_lock_first_outbox_worker_must_not_delete(
    make_verified_user,
):
    """Runtime clock sweep / Pass 33 rewrite: the durable claim is Document.storage_key (or
    equivalent), not retain-before-reference. Writer holds the advisory lock through verify,
    commits a real DB reference in the same transaction, then retain supersedes stale cleanup.
    Retaining under lock *before* any reference (the old shape) left unreferenced blobs as
    retained_shared forever if the writer crashed mid-transaction."""
    import hashlib
    from sqlalchemy import text as sa_text
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine
    from app.models.document import DocumentSource
    from app.models.storage_deletion_task import StorageDeletionStatus, StorageDeletionTask
    from app.account.erasure import attempt_storage_deletion_task
    from app.request_context import current_user_id as current_user_id_var
    from app.storage.references import enqueue_rejected_upload_cleanup_task, retain_pending_rejected_upload_cleanup_tasks

    _AdminSession = sessionmaker(bind=migration_engine)
    user, _ = make_verified_user()

    content = f"pass 33 test D-prime writer wins first {uuid.uuid4().hex}".encode()
    storage_key = get_storage().write_stream(iter([content, b""]).__next__, max_bytes=len(content)).storage_key
    operation_id = enqueue_rejected_upload_cleanup_task(storage_key)

    writer_verified = threading.Event()
    writer_may_commit = threading.Event()
    worker_outcome: dict[str, StorageDeletionStatus] = {}
    writer_errors: list[BaseException] = []

    def _writer():
        db = SessionLocal()
        try:
            current_user_id_var.set(str(user.id))
            db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
            blob = _store_bytes_with_reference_lock(db, get_storage(), content, max_bytes=len(content) + 10)
            db.add(
                Document(
                    title="retain-after-ref.txt",
                    source=DocumentSource.upload,
                    uploaded_by=user.id,
                    checksum=hashlib.sha256(content).hexdigest(),
                    original_filename="retain-after-ref.txt",
                    status=IndexStatus.original_stored,
                    storage_key=blob.storage_key,
                    size_bytes=blob.size_bytes,
                )
            )
            writer_verified.set()
            writer_may_commit.wait(timeout=5)
            db.commit()
            retain_pending_rejected_upload_cleanup_tasks(blob.storage_key)
        except BaseException as exc:  # noqa: BLE001
            writer_errors.append(exc)
            db.rollback()
        finally:
            db.close()

    def _outbox_worker():
        db = _AdminSession()
        try:
            assert writer_verified.wait(timeout=5), "writer never reached verify under lock"
            task = db.query(StorageDeletionTask).filter_by(operation_id=operation_id, storage_key=storage_key).one()
            attempt_storage_deletion_task(db, task)
            db.refresh(task)
            worker_outcome["status"] = task.status
        finally:
            db.close()

    t_writer = threading.Thread(target=_writer)
    t_worker = threading.Thread(target=_outbox_worker)
    t_writer.start()
    t_worker.start()
    assert writer_verified.wait(timeout=5)
    writer_may_commit.set()
    t_writer.join(timeout=10)
    t_worker.join(timeout=10)

    assert writer_errors == [], writer_errors
    assert not t_writer.is_alive() and not t_worker.is_alive()
    assert get_storage().exists(storage_key) is True
    assert worker_outcome["status"] == StorageDeletionStatus.retained_shared


def test_store_bytes_with_reference_lock_rollback_without_reference_allows_cleanup():
    """Crash/rollback after store-under-lock but before Document.storage_key must leave the
    blob reclaimable — retain must NOT have marked cleanup retained_shared already."""
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine
    from app.models.storage_deletion_task import StorageDeletionStatus, StorageDeletionTask
    from app.account.erasure import attempt_storage_deletion_task
    from app.storage.references import enqueue_rejected_upload_cleanup_task

    _AdminSession = sessionmaker(bind=migration_engine)
    content = f"retain-after-ref rollback orphan check {uuid.uuid4().hex}".encode()
    storage_key = get_storage().write_stream(iter([content, b""]).__next__, max_bytes=len(content)).storage_key
    operation_id = enqueue_rejected_upload_cleanup_task(storage_key)

    db = SessionLocal()
    try:
        _store_bytes_with_reference_lock(db, get_storage(), content, max_bytes=len(content) + 10)
        db.rollback()
    finally:
        db.close()

    admin = _AdminSession()
    try:
        task = admin.query(StorageDeletionTask).filter_by(operation_id=operation_id, storage_key=storage_key).one()
        assert task.status == StorageDeletionStatus.pending
        attempt_storage_deletion_task(admin, task)
        admin.refresh(task)
        assert task.status == StorageDeletionStatus.purged
        assert get_storage().exists(storage_key) is False
    finally:
        admin.close()


def test_store_bytes_with_reference_lock_outbox_worker_wins_lock_first_writer_must_republish():
    """Deterministic regression for Test D's OTHER failure mode (the mirror of the
    writer-wins-first test above): the outbox worker acquires the lock, finds no DB reference
    yet, and physically deletes the blob -- fully completing and committing -- BEFORE the
    writer even attempts to reclaim the same content-addressed key. Proves
    `_store_bytes_with_reference_lock()`'s own "lost the race, republish under lock" fallback
    (app/rag/library_import.py) correctly recovers the blob regardless of which side actually
    wins the advisory lock, not just the writer-wins ordering Pass 33's own deterministic test
    already covers. Run sequentially (worker fully finishes before the writer starts) rather
    than with real thread concurrency -- this is the maximally deterministic version of
    "worker wins," and removes timing as a variable entirely: if this passes reliably but the
    original barrier-based Test D still flakes in CI, the flake is not this specific ordering
    going wrong, narrowing where a future investigation should look instead of leaving it as an
    unexamined assumption."""
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine
    from app.models.storage_deletion_task import StorageDeletionStatus, StorageDeletionTask
    from app.account.erasure import attempt_storage_deletion_task
    from app.storage.references import enqueue_rejected_upload_cleanup_task

    _AdminSession = sessionmaker(bind=migration_engine)

    content = f"pass 33-prime worker wins first {uuid.uuid4().hex}".encode()
    storage_key = get_storage().write_stream(iter([content, b""]).__next__, max_bytes=len(content)).storage_key
    operation_id = enqueue_rejected_upload_cleanup_task(storage_key)

    db_worker = _AdminSession()
    try:
        task = db_worker.query(StorageDeletionTask).filter_by(operation_id=operation_id, storage_key=storage_key).one()
        attempt_storage_deletion_task(db_worker, task)
        db_worker.refresh(task)
        worker_status = task.status
    finally:
        db_worker.close()

    # The worker must have genuinely, physically deleted the blob for this to be a real test
    # of the reclaim path below -- not a no-op.
    assert worker_status == StorageDeletionStatus.purged
    assert get_storage().exists(storage_key) is False

    db_writer = SessionLocal()
    try:
        blob = _store_bytes_with_reference_lock(db_writer, get_storage(), content, max_bytes=len(content) + 10)
        db_writer.commit()
    finally:
        db_writer.close()

    assert blob.storage_key == storage_key
    assert get_storage().exists(storage_key) is True, (
        "the writer's own reclaim-under-lock fallback must republish a blob the outbox worker "
        "already deleted before the writer ever started"
    )


@pytest.mark.asyncio
async def test_two_concurrent_jobs_uploading_identical_content_both_succeed_without_deadlock(db_session, make_verified_user):
    """Test E (founder's lettering): two REAL, fully concurrent run_import_job() calls (two
    different owners) uploading byte-identical content -- content-addressing means they target
    the exact SAME storage_key. Proves the new per-write storage-key lock doesn't introduce
    lock contention/deadlock for the ordinary, legitimate "two unrelated writers, same content"
    case, and that both Documents end up referencing a blob that genuinely exists."""
    import asyncio

    owner_a, _ = make_verified_user(email="pass32-e-a@example.com")
    owner_b, _ = make_verified_user(email="pass32-e-b@example.com")
    content = f"pass 32 test E: two concurrent legitimate writers {uuid.uuid4().hex}".encode()

    db_a = SessionLocal()
    db_b = SessionLocal()
    job_a = _make_job(db_a, owner_a.id, content, "shared-a.txt")
    job_b = _make_job(db_b, owner_b.id, content, "shared-b.txt")

    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def _run(db, job, owner):
        try:
            barrier.wait(timeout=5)
            asyncio.run(run_import_job(db, job.id, owner.id))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            db.close()

    t1 = threading.Thread(target=_run, args=(db_a, job_a, owner_a))
    t2 = threading.Thread(target=_run, args=(db_b, job_b, owner_b))
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)

    assert not t1.is_alive() and not t2.is_alive(), "a concurrent job never finished -- possible deadlock"
    assert errors == [], f"unexpected exception(s): {errors}"

    # RLS scopes `documents` per-owner via app.current_user_id -- an ordinary SessionLocal()
    # here would see neither owner's row. Bypass RLS with a session bound to migration_engine,
    # same pattern as Test D's _AdminSession above.
    from sqlalchemy.orm import sessionmaker

    from app.db import migration_engine

    check_db = sessionmaker(bind=migration_engine)()
    try:
        doc_a = check_db.query(Document).filter_by(uploaded_by=owner_a.id, original_filename="shared-a.txt").first()
        doc_b = check_db.query(Document).filter_by(uploaded_by=owner_b.id, original_filename="shared-b.txt").first()
        assert doc_a is not None and doc_b is not None
        assert doc_a.status == IndexStatus.indexed
        assert doc_b.status == IndexStatus.indexed
        assert doc_a.storage_key == doc_b.storage_key  # content-addressed: same bytes, same key
        assert get_storage().exists(doc_a.storage_key) is True
    finally:
        check_db.close()


# --- Pass 32 blocker 2 (an eighth founder review round): verify hash, not just existence -----
#
# store_content_with_reference_lock()/_store_bytes_with_reference_lock() used to check
# storage.exists() after acquiring the DB storage-key lock -- a founder review pointed out this
# can't tell a genuinely-published blob apart from a same-path file whose bytes don't actually
# match its own sha256 (disk corruption, or a manual out-of-band edit). Both now call
# storage.verify(expected_sha256=..., expected_size=...) instead. Tests below are the founder's
# fresh lettering for this blocker, applied to library_import.py's own persistent-writer helper
# (Test B, the Project Memory equivalent, lives in test_project_memory.py); E (concurrent
# writers never accept a corrupt existing blob) and F (delete/write race stays deadlock-free)
# are covered by tests/backend/storage/test_storage_local_fs.py's existing concurrency tests, since this blocker's
# fix added no new locking -- only a hash check inside an already-locked critical section.


def test_store_bytes_with_reference_lock_repairs_a_corrupt_same_size_existing_blob():
    """Test C (founder's Pass 32 blocker-2 lettering): the library_import worker's own
    persistent-writer helper must not accept a same-path, same-size, WRONG-content existing
    blob either -- same contract as Project Memory's store_content_with_reference_lock(), now
    verified independently for this second persistent writer."""
    content = f"pass 32 blocker 2 test C: library_import repairs corrupt blob {uuid.uuid4().hex}".encode()
    storage = get_storage()
    reader = io.BytesIO(content)
    first = storage.write_stream(lambda: reader.read(1 << 20), max_bytes=len(content) + 10)

    disk_path = Path(get_settings().storage_root) / first.storage_key
    corrupted = bytes(b ^ 0xFF for b in content)
    assert len(corrupted) == len(content)
    disk_path.write_bytes(corrupted)
    assert storage.verify(first.storage_key, expected_sha256=first.sha256, expected_size=first.size_bytes) is False

    db = SessionLocal()
    try:
        blob = _store_bytes_with_reference_lock(db, storage, content, max_bytes=len(content) + 10)
        db.commit()
    finally:
        db.close()

    assert blob.storage_key == first.storage_key
    assert disk_path.read_bytes() == content
    assert storage.verify(blob.storage_key, expected_sha256=blob.sha256, expected_size=blob.size_bytes) is True


@pytest.mark.asyncio
async def test_run_import_job_leaves_no_storage_key_when_verification_never_succeeds(db_session, make_verified_user, monkeypatch):
    """Test D (founder's Pass 32 blocker-2 lettering): if storage.verify() never succeeds even
    after republishing (a pathological repeat-corruption, simulated here by forcing verify()
    itself to always return False -- proving the REAL write->lock->verify->republish->verify
    code path fails closed, not just a fully-swapped fake storage backend), no
    Document.storage_key / DB reference is ever committed."""
    user, _ = make_verified_user(email="pass32-blocker2-d@example.com")
    content = f"pass 32 blocker 2 test D: verification never succeeds {uuid.uuid4().hex}".encode()
    job = _make_job(db_session, user.id, content, "testd.txt")

    monkeypatch.setattr(get_storage(), "verify", lambda *a, **kw: False)

    await run_import_job(db_session, job.id, user.id)

    db_session.refresh(job)
    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="testd.txt").first()
    assert doc is not None
    assert doc.status == IndexStatus.storage_failed
    assert doc.storage_key is None
