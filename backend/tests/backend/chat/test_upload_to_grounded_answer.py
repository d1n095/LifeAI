"""E1/E2: an uploaded document grounds a later chat answer, end to end.

The existing grounding suite seeds Document/DocumentChunk rows directly, and the durable
upload suite never asks a question. Neither proves the product path the founder actually
takes: upload through the real HTTP route, index through the real worker claim clock and
ingest path, then ask about it in chat.

The embedding and chat providers are deterministic fakes (never a real key), but everything
between them -- storage, ImportJob claim, chunking, embedding-space stamping, retrieval,
trust assessment and the prompt the router builds -- is the production path.
"""

from __future__ import annotations

import pytest

from app.config import get_settings
from app.models.document import Document, IndexStatus
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim
DISTINCTIVE = "Kvantflugor kalibrerar sensorn vid minus tjugo grader."
DOCUMENT_TITLE = "kvantflugeprotokoll.txt"


@pytest.fixture(autouse=True)
def _deterministic_providers(monkeypatch):
    """One fixed vector for every text, and a chat double that is valid claim JSON.

    Ingest runs claim extraction through the chat provider, so the double returns ``[]``
    (a valid, empty extraction) rather than prose that would fail that step. Every prompt
    the router builds is captured, including the answer prompt this test asserts on.
    """

    captured: list[list] = []

    async def _embed(self, texts, model, **kwargs):
        return [[0.25] * DIM for _ in texts]

    async def _chat(self, messages, model, **kwargs):
        captured.append(messages)
        return ChatResult(
            content="[]",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 3},
        )

    from app.providers.ollama_provider import OllamaProvider

    # Chat retrieval runs under effect_guard=True, which refuses an external query
    # embedding; the grounding suite uses the same local-runtime fake for that reason.
    monkeypatch.setattr(get_settings(), "default_embedding_provider", "ollama")
    monkeypatch.setattr(get_settings(), "default_embedding_model", "nomic-embed-text")
    monkeypatch.setattr(OllamaProvider, "embed", _embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    # This test is about the local pipeline, never about provider fallback.
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")
    return captured


def _login(client) -> str:
    res = client.post(
        "/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD}
    )
    assert res.status_code == 200
    return res.json()["csrf_token"]


def _founder_id(superuser_db):
    from app.models.user import User

    return superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).first().id


@pytest.mark.asyncio
async def test_uploaded_document_is_indexed_and_then_grounds_a_chat_answer(
    client, superuser_db, _deterministic_providers
):
    from sqlalchemy.orm import sessionmaker

    from app.db import SessionLocal, migration_engine
    from app.jobs.lease import claim_next_job
    from app.worker import process_claimed_job

    csrf = _login(client)

    upload = client.post(
        "/api/documents/upload",
        files={"file": (DOCUMENT_TITLE, DISTINCTIVE.encode("utf-8"), "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert upload.status_code == 200, upload.text
    document_id = upload.json()["id"]

    owner_id = _founder_id(superuser_db)
    superuser_db.expire_all()
    document = superuser_db.get(Document, document_id)
    assert document is not None
    assert document.import_job_id is not None
    job_id = document.import_job_id
    # Durable before any indexing: the HTTP success alone leaves a claimable job.
    assert superuser_db.get(ImportJob, job_id).status.value == "pending"

    # The real worker claim clock, not a direct run_import_job() call.
    claim_db = sessionmaker(bind=migration_engine)()
    try:
        claimed = claim_next_job(claim_db, "worker-e2e", 120)
    finally:
        claim_db.close()
    assert claimed == (job_id, owner_id)

    worker_db = SessionLocal()
    try:
        await process_claimed_job(worker_db, job_id, owner_id)
    finally:
        worker_db.close()

    superuser_db.expire_all()
    indexed = superuser_db.get(Document, document_id)
    assert indexed is not None and indexed.status == IndexStatus.indexed
    chunks = superuser_db.query(DocumentChunk).filter_by(document_id=document_id).all()
    assert chunks, "the real ingest path produced no chunk"
    assert all(chunk.embedding_model for chunk in chunks), (
        "every ingested chunk must be stamped with the active embedding space, or the "
        "active-space-scoped retrieval can never rank it"
    )

    answer = client.post(
        "/api/chat",
        json={"message": "Vad anvands kvantflugor till?"},
        headers={"X-CSRF-Token": csrf},
    )
    assert answer.status_code == 200, answer.text
    body = answer.json()

    assert body["grounding_status"] == "grounded"
    assert body["sources"], "a grounded answer must cite the uploaded document"
    assert body["sources"][0]["title"] == DOCUMENT_TITLE
    assert body["sources"][0]["document_id"] == document_id

    prompts = [
        message.content
        for messages in _deterministic_providers
        for message in messages
        if message.role == "system"
    ]
    assert any(DISTINCTIVE in prompt for prompt in prompts), (
        "the uploaded document's text never reached the model prompt"
    )


@pytest.mark.asyncio
async def test_upload_indexing_is_owner_scoped_and_invisible_to_another_owner(
    client, superuser_db, make_verified_user, _deterministic_providers
):
    """The same pipeline must not leak the document into another owner's grounding."""

    csrf = _login(client)
    upload = client.post(
        "/api/documents/upload",
        files={"file": (DOCUMENT_TITLE, DISTINCTIVE.encode("utf-8"), "text/plain")},
        headers={"X-CSRF-Token": csrf},
    )
    assert upload.status_code == 200, upload.text
    document_id = upload.json()["id"]

    other_user, _ = make_verified_user()
    superuser_db.expire_all()
    document = superuser_db.get(Document, document_id)
    from sqlalchemy.orm import sessionmaker

    from app.db import SessionLocal, migration_engine
    from app.jobs.lease import claim_next_job
    from app.worker import process_claimed_job

    claim_db = sessionmaker(bind=migration_engine)()
    try:
        claimed = claim_next_job(claim_db, "worker-e2e-2", 120)
    finally:
        claim_db.close()
    assert claimed == (document.import_job_id, document.uploaded_by)

    worker_db = SessionLocal()
    try:
        await process_claimed_job(worker_db, document.import_job_id, document.uploaded_by)
    finally:
        worker_db.close()

    superuser_db.expire_all()
    assert superuser_db.get(Document, document_id).status == IndexStatus.indexed

    # Another owner's query must not rank the founder's chunk.
    from app.rag.vector_store import hybrid_search

    rows = hybrid_search(
        superuser_db,
        other_user.id,
        [0.25] * DIM,
        "kvantflugor",
        top_k=5,
        embedding_model=get_settings().default_embedding_model,
        embedding_native_dim=DIM,
    )
    assert all(str(row["document_id"]) != str(document_id) for row in rows)
