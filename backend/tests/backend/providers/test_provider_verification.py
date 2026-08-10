"""P1 (provider pre-flight verification, docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §0/§4.7)
tests. Covers, in order:

  A. app/providers/verification.py's classification of a REAL minimal call's outcome — never
     just is_configured() — including the specific regression this package exists to close:
     a Gemini failure must never leak the API key (which app/providers/gemini_provider.py
     puts directly in the request URL's query string) into a stored/logged message.
  B. ensure_verified()'s cache.
  C. app/rag/ingest.py's pre-flight gate: awaiting_provider/blocked_provider instead of a
     real embed() attempt, and indexing_failed for a genuinely new post-preflight failure.
  D. app/rag/library_import.py's storage/extraction reclassification.
  E. The full "no re-upload" loop: a blocked document/job resumes and reaches `indexed`
     automatically once the provider verifies ok, reusing the same Document row.
  F. Migration 0013 leaves pre-existing `failed` rows untouched.
  G. Admin -> Providers surfaces real verification status and can trigger one on demand.

No real network call is ever made — every provider call in this file is monkeypatched at the
provider-method or httpx.AsyncClient level, exactly like the rest of the suite (see
tests/backend/rag/test_library_import.py's module docstring)."""

import io
import uuid
import zipfile
from datetime import datetime, timedelta

import httpx
import pytest

from app.config import get_settings
from app.models.document import Document, IndexStatus
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_version import KnowledgeVersion
from app.models.provider_config import ProviderConfig
from app.models.provider_verification import ProviderVerificationCheck, VerificationResult
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import ChatResult, Message, ProviderError
from app.providers.gemini_provider import GeminiProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.verification import classify_provider_exception, ensure_verified, verify_now, verify_provider
from app.rag.ingest import index_document
from app.rag.library_import import _resume_incomplete_document, run_import_job
from app.storage import get_storage

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _status_error(status: int, url: str = "https://api.example.com/v1/chat") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status} error", request=request, response=response)


def _read_chunk_for(data: bytes, size: int = 1 << 16):
    pos = 0

    def _read():
        nonlocal pos
        chunk = data[pos : pos + size]
        pos += len(chunk)
        return chunk

    return _read


def _store(raw: bytes):
    return get_storage().write_stream(_read_chunk_for(raw), max_bytes=max(len(raw), 1))


def _make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _make_job(db_session, owner_id, raw: bytes, filename: str) -> ImportJob:
    from sqlalchemy import text

    from app.request_context import current_user_id as current_user_id_var

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


def _set_rls_owner(db_session, owner_id) -> None:
    """documents is RLS-protected (see migration 0006) — a direct db_session.add(Document())
    in a test needs the same owner context app/deps.py's get_current_user sets for a real
    request, or the INSERT is rejected by the isolation policy. Mirrors app/rag/
    library_import.py's identical _set_rls_owner helper."""
    from sqlalchemy import text

    from app.request_context import current_user_id as current_user_id_var

    current_user_id_var.set(str(owner_id))
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


# --- A. Real-call classification ---------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_provider_makes_a_real_minimal_call_not_just_is_configured(monkeypatch):
    """The core P1 requirement: a valid-looking key string is NOT enough. is_configured()
    would return True here — verify_provider() must still actually call chat()/embed()."""
    called = {}

    async def _fake_chat(self, messages, model, *, timeout=None, **kwargs):
        called["messages"] = messages
        called["model"] = model
        called["timeout"] = timeout
        return ChatResult(content="pong", provider="openai", model=model)

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(get_settings(), "openai_api_key", "fake-but-present-key")

    outcome = await verify_provider(OpenAIProvider(), "gpt-4o-mini", "chat", timeout=5.0)

    assert outcome.result == VerificationResult.ok
    assert called["model"] == "gpt-4o-mini"
    assert called["timeout"] == 5.0
    assert len(called["messages"]) == 1 and called["messages"][0].content  # a real, non-empty probe message


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.asyncio
async def test_401_and_403_classify_as_invalid_key(monkeypatch, status):
    async def _raise(self, messages, model, *, timeout=None, **kwargs):
        raise _status_error(status)

    monkeypatch.setattr(OpenAIProvider, "chat", _raise)
    monkeypatch.setattr(get_settings(), "openai_api_key", "wrong-key")

    outcome = await verify_provider(OpenAIProvider(), "gpt-4o-mini", "chat", timeout=5.0)
    assert outcome.result == VerificationResult.invalid_key


@pytest.mark.asyncio
async def test_429_classifies_as_rate_limited_not_invalid_key(monkeypatch):
    async def _raise(self, messages, model, *, timeout=None, **kwargs):
        raise _status_error(429)

    monkeypatch.setattr(OpenAIProvider, "chat", _raise)
    monkeypatch.setattr(get_settings(), "openai_api_key", "some-key")

    outcome = await verify_provider(OpenAIProvider(), "gpt-4o-mini", "chat", timeout=5.0)
    assert outcome.result == VerificationResult.rate_limited


@pytest.mark.asyncio
async def test_timeout_classifies_as_unreachable_not_invalid_key(monkeypatch):
    """A temporary network problem must never be reported as a definitively bad key."""

    async def _raise(self, messages, model, *, timeout=None, **kwargs):
        raise httpx.TimeoutException("timed out")

    monkeypatch.setattr(OpenAIProvider, "chat", _raise)
    monkeypatch.setattr(get_settings(), "openai_api_key", "some-key")

    outcome = await verify_provider(OpenAIProvider(), "gpt-4o-mini", "chat", timeout=1.0)
    assert outcome.result == VerificationResult.unreachable


@pytest.mark.asyncio
async def test_connection_error_classifies_as_unreachable(monkeypatch):
    async def _raise(self, messages, model, *, timeout=None, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(OpenAIProvider, "chat", _raise)
    monkeypatch.setattr(get_settings(), "openai_api_key", "some-key")

    outcome = await verify_provider(OpenAIProvider(), "gpt-4o-mini", "chat", timeout=1.0)
    assert outcome.result == VerificationResult.unreachable


@pytest.mark.asyncio
async def test_5xx_classifies_as_unreachable_not_invalid_key(monkeypatch):
    async def _raise(self, messages, model, *, timeout=None, **kwargs):
        raise _status_error(503)

    monkeypatch.setattr(OpenAIProvider, "chat", _raise)
    monkeypatch.setattr(get_settings(), "openai_api_key", "some-key")

    outcome = await verify_provider(OpenAIProvider(), "gpt-4o-mini", "chat", timeout=5.0)
    assert outcome.result == VerificationResult.unreachable


@pytest.mark.asyncio
async def test_a_provider_error_with_a_preset_category_is_trusted_message_and_all(monkeypatch):
    """2026-07-27: app/providers/gemini_provider.py now raises a ProviderError it builds
    itself (carrying Google's own sanitized error body — see that module's
    _safe_error_detail()) instead of letting a raw httpx.HTTPStatusError bubble up. That
    exception's message is safe by construction (never built from the request URL/headers),
    so classify_provider_exception() must trust its pre-set .category AND surface its
    message as-is — unlike a raw httpx exception, or a plain, uncategorized ProviderError
    (see the "unsupported" tests above/below), which are still never echoed directly."""
    from app.providers.base import ProviderError

    async def _raise_rich_error(self, messages, model, *, timeout=None, **kwargs):
        raise ProviderError(
            "Gemini svarade med HTTP 404 för modell 'gemini-2.5-flash': NOT_FOUND: models/gemini-2.5-flash is not found",
            category="unreachable",
        )

    monkeypatch.setattr(GeminiProvider, "chat", _raise_rich_error)
    monkeypatch.setattr(get_settings(), "google_api_key", "some-key")

    outcome = await verify_provider(GeminiProvider(), "gemini-2.5-flash", "chat", timeout=5.0)

    assert outcome.result == VerificationResult.unreachable
    assert "NOT_FOUND" in outcome.message
    assert "gemini-2.5-flash" in outcome.message


@pytest.mark.asyncio
async def test_missing_key_short_circuits_to_not_configured_without_any_network_call(monkeypatch):
    async def _fail_if_called(self, messages, model, *, timeout=None, **kwargs):
        raise AssertionError("chat() must never be called when no key is configured")

    monkeypatch.setattr(OpenAIProvider, "chat", _fail_if_called)
    monkeypatch.setattr(get_settings(), "openai_api_key", None)

    outcome = await verify_provider(OpenAIProvider(), "gpt-4o-mini", "chat", timeout=5.0)
    assert outcome.result == VerificationResult.not_configured


@pytest.mark.asyncio
async def test_embedding_role_against_a_chat_only_provider_classifies_as_unsupported(monkeypatch):
    """Anthropic has no embedding API at all (see app/providers/anthropic_provider.py) — a
    real, immediate ProviderError, no network call, correctly distinguished from an
    auth/network problem."""
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "some-key")

    outcome = await verify_provider(AnthropicProvider(), "claude-sonnet-5", "embedding", timeout=5.0)
    assert outcome.result == VerificationResult.unsupported


@pytest.mark.asyncio
async def test_gemini_sends_key_only_in_header_never_in_url(monkeypatch):
    """Security incident, 2026-07-26: app/providers/gemini_provider.py used to build its
    request URL as `.../generateContent?key={google_api_key}` — httpx.HTTPStatusError's
    default str() includes the full request URL, and app/providers/registry.py's
    chat_with_fallback() used to log that raw exception on every provider failure, so a
    Gemini failure leaked a live key straight into the Docker log. Fixed by moving the key to
    the `x-goog-api-key` header (Google's own documented contract), which structurally keeps
    it out of the URL — this test proves that placement, not just that the message stays
    safe (see the sibling test below for that layer, kept as defense in depth regardless of
    where any future provider decides to put its credential)."""
    secret_key = "AQ.Ab8RN6-super-secret-gemini-auth-key-must-never-leak-anywhere"
    monkeypatch.setattr(get_settings(), "google_api_key", secret_key)

    captured_urls = []
    captured_headers = []

    async def _raise_with_key_in_url(self, url, **kwargs):
        captured_urls.append(url)
        captured_headers.append(kwargs.get("headers", {}))
        request = httpx.Request("POST", url)
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(httpx.AsyncClient, "post", _raise_with_key_in_url)

    outcome = await verify_provider(GeminiProvider(), "gemini-2.5-flash", "chat", timeout=5.0)

    assert captured_urls and secret_key not in captured_urls[0]
    assert captured_headers and captured_headers[0].get("x-goog-api-key") == secret_key

    assert outcome.result == VerificationResult.invalid_key
    assert secret_key not in outcome.message
    assert "key=" not in outcome.message
    assert "generateContent" not in outcome.message  # no fragment of the URL at all


@pytest.mark.asyncio
async def test_ensure_verified_message_never_contains_the_raw_api_key_when_persisted(db_session, monkeypatch):
    """End-to-end through ensure_verified()'s DB persistence, not just verify_provider()'s
    return value — proves the key never reaches provider_verification_checks.message either."""
    secret_key = "sk-super-secret-gemini-key-persisted-check"
    monkeypatch.setattr(get_settings(), "google_api_key", secret_key)
    db_session.add(ProviderConfig(role="chat", provider="gemini", model="gemini-2.5-flash", is_active=True))
    db_session.commit()

    async def _raise_with_key_in_url(self, url, **kwargs):
        request = httpx.Request("POST", url)
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(httpx.AsyncClient, "post", _raise_with_key_in_url)

    await ensure_verified(db_session, role="chat")

    rows = db_session.query(ProviderVerificationCheck).all()
    assert len(rows) == 1
    assert secret_key not in rows[0].message
    assert secret_key not in (rows[0].message or "")


# --- B. Caching --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_verified_caches_within_ttl_no_second_real_call(db_session, monkeypatch):
    call_count = {"n": 0}

    async def _fake_chat(self, messages, model, *, timeout=None, **kwargs):
        call_count["n"] += 1
        return ChatResult(content="pong", provider="openai", model=model)

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(get_settings(), "openai_api_key", "some-key")
    monkeypatch.setattr(get_settings(), "provider_verification_cache_seconds", 300)

    first = await ensure_verified(db_session, role="chat")
    second = await ensure_verified(db_session, role="chat")

    assert first.result == VerificationResult.ok
    assert second.result == VerificationResult.ok
    assert call_count["n"] == 1, "a fresh real call must not be made again within the cache TTL"
    assert db_session.query(ProviderVerificationCheck).count() == 1


@pytest.mark.asyncio
async def test_ensure_verified_makes_a_fresh_call_once_the_cache_expires(db_session, monkeypatch):
    call_count = {"n": 0}

    async def _fake_chat(self, messages, model, *, timeout=None, **kwargs):
        call_count["n"] += 1
        return ChatResult(content="pong", provider="openai", model=model)

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(get_settings(), "openai_api_key", "some-key")
    monkeypatch.setattr(get_settings(), "provider_verification_cache_seconds", 0)

    await ensure_verified(db_session, role="chat")
    await ensure_verified(db_session, role="chat")

    assert call_count["n"] == 2, "a zero-second cache must trigger a fresh real call every time"


# --- C. Pipeline pre-flight gate (app/rag/ingest.py) ------------------------------------


@pytest.mark.asyncio
async def test_a_document_with_no_provider_configured_gets_awaiting_provider_not_failed(db_session, make_verified_user, monkeypatch):
    monkeypatch.setattr(get_settings(), "openai_api_key", None)
    user, _ = make_verified_user()
    _set_rls_owner(db_session, user.id)
    doc = Document(uploaded_by=user.id, title="t.txt", original_filename="t.txt", checksum="a" * 64)
    db_session.add(doc)
    db_session.commit()

    await index_document(db_session, doc, "Riktigt textinnehall att indexera.")

    db_session.refresh(doc)
    assert doc.status == IndexStatus.awaiting_provider
    assert doc.error_message is not None
    assert "lagrad" in doc.error_message  # reassures the founder nothing was lost


@pytest.mark.asyncio
async def test_a_document_with_an_invalid_key_gets_blocked_provider_not_failed(db_session, make_verified_user, monkeypatch):
    # The embedding-role pre-flight probe calls provider.embed(), not .chat() — see
    # app/providers/verification.py's verify_provider().
    async def _raise_401(self, texts, model, *, timeout=None, **kwargs):
        raise _status_error(401)

    monkeypatch.setattr(OpenAIProvider, "embed", _raise_401)
    monkeypatch.setattr(get_settings(), "openai_api_key", "invalid-key")

    user, _ = make_verified_user()
    _set_rls_owner(db_session, user.id)
    doc = Document(uploaded_by=user.id, title="t.txt", original_filename="t.txt", checksum="b" * 64)
    db_session.add(doc)
    db_session.commit()

    await index_document(db_session, doc, "Riktigt textinnehall att indexera.")

    db_session.refresh(doc)
    assert doc.status == IndexStatus.blocked_provider
    assert doc.error_message is not None


@pytest.mark.asyncio
async def test_embed_is_only_called_once_for_the_preflight_probe_when_verification_fails(
    db_session, make_verified_user, monkeypatch
):
    """Proves this is a genuine PAUSE, not a failed real embed() attempt: embed() is called
    exactly once (the pre-flight probe itself, ["ping"]) — the real chunk-embedding call site
    is never separately reached after a failed pre-flight check."""
    call_count = {"n": 0}

    async def _raise_401(self, texts, model, *, timeout=None, **kwargs):
        call_count["n"] += 1
        raise _status_error(401)

    monkeypatch.setattr(OpenAIProvider, "embed", _raise_401)
    monkeypatch.setattr(get_settings(), "openai_api_key", "invalid-key")

    user, _ = make_verified_user()
    _set_rls_owner(db_session, user.id)
    doc = Document(uploaded_by=user.id, title="t.txt", original_filename="t.txt", checksum="c" * 64)
    db_session.add(doc)
    db_session.commit()

    await index_document(db_session, doc, "Riktigt textinnehall att indexera.")

    db_session.refresh(doc)
    assert doc.status == IndexStatus.blocked_provider
    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_a_genuine_post_preflight_embed_failure_gets_indexing_failed(db_session, make_verified_user, monkeypatch):
    """Pre-flight passes (the ["ping"] probe embed() call succeeds), but the real embed()
    call for the actual chunks still fails — a distinct, presumably rarer case, not
    automatically retried."""
    from app.providers.verification import VERIFICATION_PROBE_TEXT

    async def _embed_probe_ok_real_chunks_fail(self, texts, model, *, timeout=None, **kwargs):
        if texts == [VERIFICATION_PROBE_TEXT]:
            return [[0.01] * get_settings().embedding_dim]
        raise _status_error(500)

    monkeypatch.setattr(OpenAIProvider, "embed", _embed_probe_ok_real_chunks_fail)
    monkeypatch.setattr(get_settings(), "openai_api_key", "some-key")

    user, _ = make_verified_user()
    _set_rls_owner(db_session, user.id)
    doc = Document(uploaded_by=user.id, title="t.txt", original_filename="t.txt", checksum="d" * 64)
    db_session.add(doc)
    db_session.commit()

    await index_document(db_session, doc, "Riktigt textinnehall att indexera.")

    db_session.refresh(doc)
    assert doc.status == IndexStatus.indexing_failed
    assert doc.error_message is not None


# --- D. library_import.py reclassification ------------------------------------------------


@pytest.mark.asyncio
async def test_storage_failure_gets_storage_failed_not_generic_failed(db_session, make_verified_user, monkeypatch):
    from app.rag import library_import as li
    from app.storage.base import StorageError

    def _always_fails(storage, content, *, max_bytes):
        raise StorageError("disk full (simulerat)")

    monkeypatch.setattr(li, "_store_bytes", _always_fails)

    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, b"nagot innehall", "broken.txt")

    await run_import_job(db_session, job.id, user.id)

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="broken.txt").first()
    assert doc is not None
    assert doc.status == IndexStatus.storage_failed


# --- E. No re-upload: resume + automatic worker requeue -----------------------------------


@pytest.mark.asyncio
async def test_resuming_a_blocked_provider_document_reuses_the_same_row_no_duplicate_created(
    db_session, make_verified_user, monkeypatch
):
    async def _raise_401(self, texts, model, *, timeout=None, **kwargs):
        raise _status_error(401)

    monkeypatch.setattr(OpenAIProvider, "embed", _raise_401)
    monkeypatch.setattr(get_settings(), "openai_api_key", "invalid-key")

    user, _ = make_verified_user()
    job = _make_job(db_session, user.id, b"Text som forst blockeras pa providern.", "resume.txt")
    await run_import_job(db_session, job.id, user.id)

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="resume.txt").first()
    assert doc.status == IndexStatus.blocked_provider
    first_doc_id = doc.id
    version_count_before = db_session.query(KnowledgeVersion).filter_by(source_id=first_doc_id).count()
    assert version_count_before == 1

    # Now the key is fixed — resume directly (the same code path app/worker.py's requeue
    # eventually drives, see the worker test below), passing the SAME content bytes the job
    # already had durably stored (never a fresh upload).
    async def _fake_chat_ok(self, messages, model, *, timeout=None, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 1, "completion_tokens": 1})

    async def _fake_embed_ok(self, texts, model, *, timeout=None, **kwargs):
        return [[0.01] * get_settings().embedding_dim for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok)
    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed_ok)
    monkeypatch.setattr(get_settings(), "provider_verification_cache_seconds", 0)

    outcome = await _resume_incomplete_document(db_session, doc, user.id, "resume.txt", b"Text som forst blockeras pa providern.")

    assert outcome.status == "indexed"
    assert outcome.source_id == str(first_doc_id)
    assert db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="resume.txt").count() == 1
    assert db_session.query(KnowledgeVersion).filter_by(source_id=first_doc_id).count() == 1

    db_session.refresh(doc)
    assert doc.status == IndexStatus.indexed
    assert doc.id == first_doc_id


@pytest.mark.asyncio
async def test_worker_requeues_blocked_jobs_once_provider_verifies_ok_and_reaches_indexed(
    db_session, superuser_db, make_verified_user, monkeypatch
):
    """The full required end-to-end proof: (1) invalid embedding key, (2) the file becomes
    blocked_provider, (3) the original stays in durable storage, (4) the key is corrected,
    (5) the worker's automatic requeue picks the job back up with NO re-upload, (6) the SAME
    document row reaches `indexed`, (7) no duplicate Document/KnowledgeVersion is created."""
    from app.worker import Worker

    async def _raise_401(self, texts, model, *, timeout=None, **kwargs):
        raise _status_error(401)

    monkeypatch.setattr(OpenAIProvider, "embed", _raise_401)
    monkeypatch.setattr(get_settings(), "openai_api_key", "invalid-key")
    monkeypatch.setattr(get_settings(), "provider_verification_cache_seconds", 0)  # re-verify every poll in this test

    user, _ = make_verified_user()
    raw = b"Innehall som forst blockeras, sedan lyckas efter en nyckelrattning."
    job = _make_job(db_session, user.id, raw, "e2e.txt")

    # (1)+(2) first worker cycle: invalid key -> blocked.
    worked = await Worker().run_once()
    assert worked is True

    superuser_db.expire_all()
    job_row = superuser_db.get(ImportJob, job.id)
    assert job_row.status == ImportJobStatus.blocked
    assert job_row.blocked_count == 1

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="e2e.txt").first()
    assert doc.status == IndexStatus.blocked_provider
    first_doc_id = doc.id

    # (3) the original is still durably stored, independent of the provider failure.
    assert doc.storage_key is not None
    with get_storage().open_read(doc.storage_key) as f:
        assert f.read() == raw

    # (4) the founder fixes the key.
    async def _fake_chat_ok(self, messages, model, *, timeout=None, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 1, "completion_tokens": 1})

    async def _fake_embed_ok(self, texts, model, *, timeout=None, **kwargs):
        return [[0.02] * get_settings().embedding_dim for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok)
    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed_ok)

    # (5)+(6) the NEXT poll cycle requeues the blocked job (_requeue_blocked_jobs) and
    # reprocesses it — reading the original from storage again, never a fresh HTTP upload —
    # reaching `indexed` on the SAME document row.
    worked_again = await Worker().run_once()
    assert worked_again is True

    superuser_db.expire_all()
    job_row2 = superuser_db.get(ImportJob, job.id)
    assert job_row2.status == ImportJobStatus.completed
    assert job_row2.succeeded_count == 1

    db_session.expire_all()
    doc2 = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="e2e.txt").first()
    assert doc2.status == IndexStatus.indexed
    assert doc2.id == first_doc_id  # (7) same row, not a new one

    # (7) no duplicate.
    assert db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="e2e.txt").count() == 1
    assert db_session.query(KnowledgeVersion).filter_by(source_id=first_doc_id).count() == 1


@pytest.mark.asyncio
async def test_worker_requeues_a_partial_job_and_never_loses_the_real_failure(db_session, superuser_db, make_verified_user, monkeypatch):
    """2026-07-28 incident: a ZIP job with ONE file that genuinely fails (a manifest checksum
    mismatch — real, permanent, nothing to do with the provider) and ANOTHER file that pauses
    on an invalid embedding key rolls up to `ImportJobStatus.partial`, not `blocked` — the old
    `_requeue_blocked_jobs` only matched `status = 'blocked'`, so this job's stuck file sat
    forever even once the key was fixed. Proves both halves of the fix together: (1) the
    requeue now picks up a `partial` job with `blocked_count > 0`, and (2) the genuinely
    failed file is still reported as failed on the second pass — not silently reclassified as
    `duplicate`, which would have erased it from the job's own count and let the job flip
    straight to `completed` with the original failure never surfaced again."""
    from app.worker import Worker

    async def _raise_401(self, texts, model, *, timeout=None, **kwargs):
        raise _status_error(401)

    monkeypatch.setattr(OpenAIProvider, "embed", _raise_401)
    monkeypatch.setattr(get_settings(), "openai_api_key", "invalid-key")
    monkeypatch.setattr(get_settings(), "provider_verification_cache_seconds", 0)

    user, _ = make_verified_user()
    manifest = b'{"documents": [{"file": "bad-checksum.txt", "checksum": "' + b"0" * 64 + b'"}]}'
    raw = _make_zip(
        {
            "manifest.json": manifest,
            "bad-checksum.txt": b"Den har filen har fel deklarerad checksumma i manifestet.",
            "blocked-by-key.txt": b"Den har filen pausar forst pa en ogiltig leverantorsnyckel.",
        }
    )
    job = _make_job(db_session, user.id, raw, "partial.zip")

    # First cycle: one real failure (checksum mismatch, nothing to do with the provider), one
    # provider-blocked file -> job rolls up to `partial`, not `blocked`.
    worked = await Worker().run_once()
    assert worked is True

    superuser_db.expire_all()
    job_row = superuser_db.get(ImportJob, job.id)
    assert job_row.status == ImportJobStatus.partial
    assert job_row.failed_count == 1
    assert job_row.blocked_count == 1
    assert job_row.succeeded_count == 0

    blocked_doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="blocked-by-key.txt").first()
    assert blocked_doc.status == IndexStatus.blocked_provider
    blocked_doc_id = blocked_doc.id

    # The founder fixes the key.
    async def _fake_chat_ok(self, messages, model, *, timeout=None, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 1, "completion_tokens": 1})

    async def _fake_embed_ok(self, texts, model, *, timeout=None, **kwargs):
        return [[0.03] * get_settings().embedding_dim for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok)
    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed_ok)

    # Second cycle: _requeue_blocked_jobs must now match this `partial` job (blocked_count >
    # 0), not just ones already at `blocked`.
    worked_again = await Worker().run_once()
    assert worked_again is True

    superuser_db.expire_all()
    job_row2 = superuser_db.get(ImportJob, job.id)
    # Still `partial` — one file really did fail and that must never disappear — but now with
    # the previously-blocked file counted as succeeded, not as blocked or, worse, silently as
    # a duplicate that erases the original failure from this pass's tally.
    assert job_row2.status == ImportJobStatus.partial
    assert job_row2.succeeded_count == 1
    assert job_row2.blocked_count == 0
    assert job_row2.failed_count == 1  # the real failure is still counted, not lost

    db_session.expire_all()
    resumed_doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="blocked-by-key.txt").first()
    assert resumed_doc.status == IndexStatus.indexed
    assert resumed_doc.id == blocked_doc_id  # same row, no duplicate

    # The genuinely-failed file was never touched by the resume — no Document row was ever
    # created for it in the first place (the checksum mismatch is rejected before that point).
    assert db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="bad-checksum.txt").count() == 0


# --- F. Migration safety -------------------------------------------------------------------


def test_old_pre_p1_failed_rows_are_untouched(superuser_db, make_verified_user):
    """Migration 0013 is purely additive (ADD VALUE / CREATE TABLE / ADD COLUMN) — a document
    already sitting in the old, undifferentiated `failed` state before P1 must not be
    silently reclassified or have its error_message touched."""
    user, _ = make_verified_user()
    doc = Document(
        uploaded_by=user.id,
        title="gammal.txt",
        original_filename="gammal.txt",
        checksum="e" * 64,
        status=IndexStatus.failed,
        error_message="Ett gammalt, odifferentierat fel fran fore P1.",
    )
    superuser_db.add(doc)
    superuser_db.commit()
    superuser_db.refresh(doc)

    assert doc.status == IndexStatus.failed
    assert doc.error_message == "Ett gammalt, odifferentierat fel fran fore P1."


# --- G. Admin API ----------------------------------------------------------------------


def test_admin_providers_status_exposes_verification_per_role(client, db_session):
    _login(client)
    db_session.add(
        ProviderVerificationCheck(
            id=uuid.uuid4(),
            provider_name="openai",
            role="chat",
            model="gpt-4o-mini",
            result=VerificationResult.ok,
            message="Verifierad.",
            checked_by="system",
        )
    )
    db_session.commit()

    res = client.get("/api/admin/providers/status")
    assert res.status_code == 200
    openai_row = next(p for p in res.json() if p["name"] == "openai")
    assert openai_row["chat_verification"] is not None
    assert openai_row["chat_verification"]["result"] == "ok"
    assert openai_row["embedding_verification"] is None  # never verified for this role yet


def test_admin_verify_endpoint_triggers_a_real_uncached_check_and_persists_it(client, monkeypatch):
    csrf = _login(client)

    async def _fake_chat(self, messages, model, *, timeout=None, **kwargs):
        return ChatResult(content="pong", provider="openai", model=model)

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(get_settings(), "openai_api_key", "some-key")

    res = client.post(
        "/api/admin/providers/verify", json={"provider": "openai", "role": "chat"}, headers={"X-CSRF-Token": csrf}
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["result"] == "ok"
    assert body["checked_by"] == "founder"

    status_res = client.get("/api/admin/providers/status")
    openai_row = next(p for p in status_res.json() if p["name"] == "openai")
    assert openai_row["chat_verification"]["checked_by"] == "founder"


# --- H. Crash/resume recovery (2026-07-28 permanent fix) -----------------------------------
#
# Confirmed production incident: MAINAI_CONTEXT_BUNDLE.md's Document row was left at
# `embedding` forever because the worker PROCESS itself died mid-step (no exception for Python
# to catch), while its ImportJob had already reached `completed` ("Klar") with an empty queue
# — nothing left to ever naturally revisit it. See app/models/document.py's
# RESUMABLE_INDEX_STATUSES, app/rag/library_import.py's _import_one_file/_resume_incomplete_
# document/_run_once, and app/worker.py's _reconcile_orphaned_documents.


@pytest.mark.asyncio
async def test_worker_crash_mid_embedding_is_resumed_to_indexed_before_job_completes(
    db_session, superuser_db, make_verified_user, monkeypatch
):
    """Founder's point 6: simulates a REAL worker process crash, not a caught exception — the
    embed() call raises a BaseException that is deliberately NOT an Exception subclass, so it
    is caught by nothing (not app/rag/ingest.py's `except Exception`, not app/worker.py's
    process_claimed_job's `except Exception`), exactly like a real SIGKILL/OOM would leave
    nothing to run past the point the Document row's `embedding` status was last committed.
    Proves the full recovery chain end to end: the reclaimed job resumes the SAME Document row
    (never a duplicate), reaches `indexed` with real chunks, and the job's own status only
    becomes `completed` once that has actually happened — never before."""
    from app.providers.verification import VERIFICATION_PROBE_TEXT
    from app.worker import Worker

    class _SimulatedProcessKill(BaseException):
        """Deliberately NOT an Exception subclass — see this test's docstring."""

    async def _crash_mid_embed(self, texts, model, **kwargs):
        # The pre-flight verification probe (both app/worker.py's _requeue_blocked_jobs,
        # called BEFORE a job is even claimed, and app/rag/ingest.py's per-document pre-flight
        # gate) must keep succeeding — only the REAL chunk-embedding call simulates the crash,
        # exactly like test_a_genuine_post_preflight_embed_failure_gets_indexing_failed above
        # distinguishes the two.
        if texts == [VERIFICATION_PROBE_TEXT]:
            return [[0.01] * get_settings().embedding_dim]
        raise _SimulatedProcessKill("simulated hard process kill mid-embed()")

    monkeypatch.setattr(OpenAIProvider, "embed", _crash_mid_embed)

    user, _ = make_verified_user()
    raw = b"Innehall som krashar workern mitt i embedding-steget."
    job = _make_job(db_session, user.id, raw, "crash-mid-embedding.txt")

    with pytest.raises(_SimulatedProcessKill):
        await Worker().run_once()

    superuser_db.expire_all()
    job_row = superuser_db.get(ImportJob, job.id)
    # Nothing downstream of the crash ever ran to finalize the job row — it is left exactly
    # where claim_next_job put it before processing began.
    assert job_row.status == ImportJobStatus.running

    doc = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="crash-mid-embedding.txt").first()
    assert doc is not None
    assert doc.status == IndexStatus.embedding  # exactly where the crash left it
    stuck_doc_id = doc.id

    # Simulate the abandoned lease expiring, exactly like tests/backend/jobs/test_worker.py's reclaim test —
    # a real restart/second worker eventually notices this the same way.
    job_row.lease_expires_at = datetime.utcnow() - timedelta(seconds=5)
    superuser_db.add(job_row)
    superuser_db.commit()

    async def _fake_chat_ok(self, messages, model, *, timeout=None, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 1, "completion_tokens": 1})

    async def _fake_embed_ok(self, texts, model, *, timeout=None, **kwargs):
        return [[0.04] * get_settings().embedding_dim for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok)
    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed_ok)

    worked = await Worker().run_once()
    assert worked is True

    superuser_db.expire_all()
    job_row2 = superuser_db.get(ImportJob, job.id)
    assert job_row2.status == ImportJobStatus.completed  # only AFTER the document actually resolved
    assert job_row2.succeeded_count == 1

    db_session.expire_all()
    doc2 = db_session.query(Document).filter_by(uploaded_by=user.id, original_filename="crash-mid-embedding.txt").first()
    assert doc2.id == stuck_doc_id  # same row, never duplicated
    assert doc2.status == IndexStatus.indexed
    assert doc2.chunk_count > 0


@pytest.mark.asyncio
async def test_reconciliation_repairs_a_terminal_job_with_a_document_still_stuck_in_embedding(
    db_session, superuser_db, make_verified_user, monkeypatch
):
    """Founder's point 7: regression test for the exact confirmed production incident,
    reproduced directly rather than via the crash mechanics of the test above — a job already
    at `completed`, a linked live Document already stuck at `embedding`, and an otherwise empty
    queue (nothing else pending to ever naturally trigger a revisit). Proves
    app/worker.py's _reconcile_orphaned_documents repairs it automatically on the very next
    poll cycle, with no manual intervention."""
    from app.rag.zip_import import sha256_bytes
    from app.worker import Worker

    user, _ = make_verified_user()
    raw = b"Innehall som redan hann bli rapporterat klart i produktion."
    job = _make_job(db_session, user.id, raw, "already-completed.txt")

    doc = Document(
        uploaded_by=user.id,
        title="already-completed.txt",
        original_filename="already-completed.txt",
        checksum=sha256_bytes(raw),  # must match what _import_one_file recomputes on resume
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

    async def _fake_chat_ok(self, messages, model, *, timeout=None, **kwargs):
        return ChatResult(content="[]", provider="openai", model=model, raw_usage={"prompt_tokens": 1, "completion_tokens": 1})

    async def _fake_embed_ok(self, texts, model, *, timeout=None, **kwargs):
        return [[0.05] * get_settings().embedding_dim for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat_ok)
    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed_ok)

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
