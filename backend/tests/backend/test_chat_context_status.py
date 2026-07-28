"""PR 1 — chat context-status awareness (2026-07-27 incident): a zero-retrieval-hit chat turn
used to always inject the identical fixed string "Ingen relevant kunskap hittades." — whether
the real cause was a stalled worker, a file mid-pipeline, a missing embedding provider, an
outright indexing failure, THIS query's own search call failing, genuinely no matching content,
or no uploaded files at all. A generative provider with no real reason to give then improvised
a plausible-sounding excuse — a confirmed production incident (see docs/BRANCH_REGISTRY.md's
file-ingestion audit), not a hypothetical.

app/rag/context_status.py classifies the real cause from signals that already exist elsewhere
(IndexStatus, ImportJob.last_heartbeat_at, classify_provider_exception) — these tests drive the
real /api/chat endpoint end to end and assert on the response's structured `context_status`
field, never on prompt wording alone."""

import uuid
from datetime import datetime

import httpx
import pytest

from app.config import get_settings
from app.jobs.heartbeat import record_worker_heartbeat
from app.models.document import ActiveTruthStatus, Document, DocumentSource, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim
MATCHING_VECTOR = [0.5] * DIM
FAKE_SECRET = "sk-super-secret-value-that-must-never-reach-the-chat-response"


@pytest.fixture
def _fake_chat_ok(monkeypatch):
    """Fakes BOTH providers so no test ever makes a real network call (this sandbox has no
    outbound access at all, and even with it, tests must be deterministic): the CHAT provider
    always succeeds (so a degraded-retrieval turn still gets a real generated reply — the
    requirement this whole PR is built around), and embed() succeeds by default too, so the
    per-scenario database state (IndexStatus/ImportJob rows) is what actually drives
    build_context_status()'s classification instead of every test accidentally landing on
    `search_provider_unavailable` from a real embed() failure. Tests 3 and 7 override embed()
    again afterwards to specifically exercise that failure path."""
    from app.providers.base import ChatResult

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content="Testsvar.", provider="openai", model=model, raw_usage={"prompt_tokens": 10, "completion_tokens": 5})

    async def _fake_embed(self, texts, model, **kwargs):
        return [MATCHING_VECTOR for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def _founder_id(superuser_db) -> uuid.UUID:
    from app.models.user import User

    return superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).first().id


def _make_document(superuser_db, owner_id, title, *, status: IndexStatus, import_job_id=None) -> Document:
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=ActiveTruthStatus.active,
        checksum=uuid.uuid4().hex,
        status=status,
        import_job_id=import_job_id,
    )
    superuser_db.add(document)
    superuser_db.commit()
    superuser_db.refresh(document)
    return document


def _make_import_job(superuser_db, owner_id, *, status: ImportJobStatus, heartbeat: datetime | None) -> ImportJob:
    job = ImportJob(owner_id=owner_id, status=status, source_filename="test.txt", last_heartbeat_at=heartbeat)
    superuser_db.add(job)
    superuser_db.commit()
    superuser_db.refresh(job)
    return job


def _chat(client, csrf, message="Var är underlaget?"):
    return client.post("/api/chat", json={"message": message}, headers={"X-CSRF-Token": csrf})


# --- 1. Worker offline with a pending document ------------------------------------------------


def test_worker_offline_with_pending_document(client, superuser_db, _fake_chat_ok):
    founder_id = _founder_id(superuser_db)
    job = _make_import_job(superuser_db, founder_id, status=ImportJobStatus.pending, heartbeat=None)
    _make_document(superuser_db, founder_id, "Vantande fil", status=IndexStatus.received, import_job_id=job.id)
    csrf = _login(client)

    res = _chat(client, csrf)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["assistant_status"] == "succeeded"  # chat provider still works
    status = body["context_status"]
    assert status["reason"] == "worker_unavailable"
    assert status["worker_reachable"] is False
    assert status["pending_count"] == 1
    assert "Worker" in status["message"] or "worker" in status["message"].lower()


# --- 1b. Worker idle but healthy — no ImportJob has ever heartbeated, but the process-level
# Redis heartbeat has (2026-07-28 incident) --------------------------------------------------


def test_worker_idle_but_healthy_is_reachable_via_process_heartbeat_even_with_no_job_heartbeat(client, superuser_db, _fake_chat_ok):
    """Before this fix, a worker that simply had nothing to claim yet (no ImportJob ever
    renewed a lease) was indistinguishable from a genuinely dead one — _worker_reachable()
    only had ImportJob.last_heartbeat_at to go on. record_worker_heartbeat() (written by
    app/worker.py's poll loop on EVERY cycle, not just when a job is claimed) now makes this
    case correctly report reachable=True."""
    founder_id = _founder_id(superuser_db)
    job = _make_import_job(superuser_db, founder_id, status=ImportJobStatus.pending, heartbeat=None)
    _make_document(superuser_db, founder_id, "Vantande fil", status=IndexStatus.received, import_job_id=job.id)
    record_worker_heartbeat("test-worker", ttl_seconds=60)
    csrf = _login(client)

    res = _chat(client, csrf)
    assert res.status_code == 200, res.text
    status = res.json()["context_status"]
    # Still "files_processing" (a pending document with a reachable worker), NOT
    # "worker_unavailable" — the whole point of this test.
    assert status["reason"] == "files_processing"
    assert status["worker_reachable"] is True


# --- 2. Document awaiting provider (worker itself IS reachable) -------------------------------


def test_document_awaiting_provider(client, superuser_db, _fake_chat_ok):
    founder_id = _founder_id(superuser_db)
    job = _make_import_job(superuser_db, founder_id, status=ImportJobStatus.blocked, heartbeat=datetime.utcnow())
    _make_document(superuser_db, founder_id, "Fil utan leverantor", status=IndexStatus.awaiting_provider, import_job_id=job.id)
    csrf = _login(client)

    res = _chat(client, csrf)
    assert res.status_code == 200, res.text
    status = res.json()["context_status"]
    assert status["reason"] == "awaiting_provider"
    assert status["worker_reachable"] is True  # the worker is fine; the provider is the problem
    assert status["awaiting_provider_count"] == 1
    assert "embedding" in status["message"].lower() or "leverantör" in status["message"].lower()


# --- 3. Search provider unavailable at query time ----------------------------------------------


def test_search_provider_unavailable_at_query_time(client, superuser_db, monkeypatch, _fake_chat_ok):
    from app.providers.base import ProviderError

    founder_id = _founder_id(superuser_db)
    # Even a fully indexed, healthy document must not prevent this from being reported —
    # retrieval failing THIS turn takes priority over any persisted document state.
    doc = _make_document(superuser_db, founder_id, "Indexerad fil", status=IndexStatus.indexed)
    superuser_db.add(DocumentChunk(document_id=doc.id, owner_id=founder_id, chunk_index=0, text="Innehall.", embedding=MATCHING_VECTOR))
    superuser_db.commit()

    async def _broken_embed(self, texts, model, **kwargs):
        raise ProviderError("OpenAI API-nyckel saknas.")

    monkeypatch.setattr(OpenAIProvider, "embed", _broken_embed)
    csrf = _login(client)

    res = _chat(client, csrf)
    assert res.status_code == 200, res.text
    body = res.json()
    status = body["context_status"]
    assert status["reason"] == "search_provider_unavailable"
    assert body["sources"] == []


# --- 4. Indexed documents but no relevant match --------------------------------------------


def test_indexed_documents_but_no_chunks_to_match(client, superuser_db, _fake_chat_ok):
    """app/rag/vector_store.py's search() always returns the nearest existing chunks (no
    similarity threshold) — so the only way a real owner with an `indexed` document produces
    zero hits is an indexed document with no retrievable chunk rows (e.g. a file that produced
    no extractable text). A genuine, if uncommon, real data shape — not a synthetic gap."""
    founder_id = _founder_id(superuser_db)
    _make_document(superuser_db, founder_id, "Tom fil", status=IndexStatus.indexed)
    csrf = _login(client)

    res = _chat(client, csrf)
    assert res.status_code == 200, res.text
    status = res.json()["context_status"]
    assert status["reason"] == "no_relevant_match"
    assert status["indexed_count"] == 1


# --- 5. No documents uploaded at all -----------------------------------------------------------


def test_no_documents_uploaded(client, _fake_chat_ok):
    csrf = _login(client)

    res = _chat(client, csrf)
    assert res.status_code == 200, res.text
    body = res.json()
    status = body["context_status"]
    assert status["reason"] == "no_documents"
    assert status["total_document_count"] == 0
    assert "inga uppladdade filer" in status["message"].lower()


# --- 6. Successful retrieval with citations — context_status must be absent --------------------


def test_successful_retrieval_has_no_context_status(client, superuser_db, _fake_chat_ok):
    founder_id = _founder_id(superuser_db)
    doc = _make_document(superuser_db, founder_id, "Aktuellt dokument", status=IndexStatus.indexed)
    superuser_db.add(DocumentChunk(document_id=doc.id, owner_id=founder_id, chunk_index=0, text="Relevant innehall.", embedding=MATCHING_VECTOR))
    superuser_db.commit()
    csrf = _login(client)

    res = _chat(client, csrf)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["context_status"] is None
    assert len(body["sources"]) == 1
    assert body["sources"][0]["document_id"] == str(doc.id)


# --- 7. A raw HTTP provider error never produces a 500 and never leaks the secret --------------


def test_raw_http_provider_error_never_500s_or_leaks_secret(client, monkeypatch, _fake_chat_ok):
    """Real incident, 2026-07-27: a CONFIGURED-but-invalid key raises an UNWRAPPED
    httpx.HTTPStatusError from provider.embed() (only a missing key raises the provider's own
    ProviderError) — chat.py's original `except ProviderError` alone did not catch this,
    reaching this function as an unhandled exception. The URL embedded in httpx's default
    exception message is exactly how a live key/secret has leaked into a log line before (see
    test_chat_fallback_logging.py) — proves neither failure mode survives here."""

    async def _broken_embed(self, texts, model, **kwargs):
        request = httpx.Request("POST", f"https://api.openai.com/v1/embeddings?key={FAKE_SECRET}")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError(f"401 for url with key={FAKE_SECRET}", request=request, response=response)

    monkeypatch.setattr(OpenAIProvider, "embed", _broken_embed)
    csrf = _login(client)

    res = _chat(client, csrf)
    assert res.status_code == 200, res.text
    assert FAKE_SECRET not in res.text
    body = res.json()
    assert body["assistant_status"] == "succeeded"
    assert body["context_status"]["reason"] == "search_provider_unavailable"
    assert FAKE_SECRET not in body["context_status"]["message"]
