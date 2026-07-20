"""API-level tests for DEL 6 (källgrundad MainAI-chatt) — drives the real /api/chat endpoint
with a deterministic fake chat+embedding provider (never a real AI key) and documents/chunks
inserted directly via the superuser connection (bypasses RLS entirely — see conftest.py's
superuser_db docstring) so each test controls exactly which sources exist and how well they
match the query, independent of the real embedding model's behavior."""

import uuid

import pytest

from app.config import get_settings
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.source_relationship import RelationshipType, SourceRelationship

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim
MATCHING_VECTOR = [0.5] * DIM  # identical query/chunk vector -> cosine similarity 1.0


@pytest.fixture
def _captured_chat_messages(monkeypatch):
    """Fakes both the chat call and the embedding call (see run_e2e_backend.py's identical
    pattern for the E2E suite) and records every system-role message the router builds, so
    tests can assert on the actual prompt content the trust engine produced — not just the
    response's own confidence/conflicts fields, which could theoretically be right by
    accident if the instruction text were never actually built into what's sent."""
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    captured = []

    async def _fake_chat(self, messages, model, **kwargs):
        captured.append(messages)
        return ChatResult(content="Testsvar.", provider="openai", model=model, raw_usage={"prompt_tokens": 10, "completion_tokens": 5})

    async def _fake_embed(self, texts, model):
        return [MATCHING_VECTOR for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)
    return captured


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def _make_source(superuser_db, owner_id, title, *, status=ActiveTruthStatus.active, vector=None) -> Document:
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=status,
        checksum=uuid.uuid4().hex,
    )
    superuser_db.add(document)
    superuser_db.commit()
    superuser_db.add(
        DocumentChunk(document_id=document.id, owner_id=owner_id, chunk_index=0, text=f"Innehall i {title}.", embedding=vector or MATCHING_VECTOR)
    )
    superuser_db.commit()
    return document


def _founder_id(superuser_db) -> uuid.UUID:
    from app.models.user import User

    return superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).first().id


def test_single_active_source_yields_high_confidence_no_conflicts(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    _make_source(superuser_db, founder_id, "Aktuellt dokument")
    csrf = _login(client)

    res = client.post("/api/chat", json={"message": "Vad sager dokumentet?"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["confidence"] == "high"
    assert body["conflicts_detected"] is False
    assert body["sources"][0]["active_truth_status"] == "active"


def test_historical_top_source_caps_confidence_and_warns_in_prompt(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    _make_source(superuser_db, founder_id, "Gammalt dokument", status=ActiveTruthStatus.historical)
    csrf = _login(client)

    res = client.post("/api/chat", json={"message": "Vad sager det gamla dokumentet?"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["confidence"] == "medium"  # capped from what would otherwise be "high"
    assert body["sources"][0]["active_truth_status"] == "historical"

    system_message = _captured_chat_messages[0][0].content
    assert "HISTORISK" in system_message


def test_disputed_top_source_caps_confidence(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    _make_source(superuser_db, founder_id, "Omtvistat dokument", status=ActiveTruthStatus.disputed)
    csrf = _login(client)

    res = client.post("/api/chat", json={"message": "Fraga om det omtvistade"}, headers={"X-CSRF-Token": csrf})
    body = res.json()
    assert body["confidence"] == "medium"
    assert body["sources"][0]["active_truth_status"] == "disputed"


def test_conflicting_sources_are_flagged_and_warned_in_prompt(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    doc_a = _make_source(superuser_db, founder_id, "Källa A")
    doc_b = _make_source(superuser_db, founder_id, "Källa B")
    superuser_db.add(
        SourceRelationship(owner_id=founder_id, from_source_id=doc_a.id, to_source_id=doc_b.id, relationship_type=RelationshipType.contradicts)
    )
    superuser_db.commit()
    csrf = _login(client)

    res = client.post("/api/chat", json={"message": "Vad galler egentligen?"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["conflicts_detected"] is True

    system_message = _captured_chat_messages[0][0].content
    assert "MOTSÄGELSER" in system_message


def test_no_relationship_means_no_conflict_flagged_even_with_multiple_sources(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    _make_source(superuser_db, founder_id, "Källa A")
    _make_source(superuser_db, founder_id, "Källa B")
    csrf = _login(client)

    res = client.post("/api/chat", json={"message": "Fraga"}, headers={"X-CSRF-Token": csrf})
    assert res.json()["conflicts_detected"] is False


def test_deleted_source_is_never_used_by_chat(client, superuser_db, _captured_chat_messages):
    founder_id = _founder_id(superuser_db)
    from datetime import datetime

    doc = _make_source(superuser_db, founder_id, "Raderad kalla")
    doc.deleted_at = datetime.utcnow()
    superuser_db.add(doc)
    superuser_db.commit()
    csrf = _login(client)

    res = client.post("/api/chat", json={"message": "Fraga om raderat"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    body = res.json()
    assert body["sources"] == []
    assert body["confidence"] == "none"


def test_chat_never_surfaces_another_users_source(client, superuser_db, make_verified_user, _captured_chat_messages):
    other_user, _ = make_verified_user()
    _make_source(superuser_db, other_user.id, "Annan anvandares hemlighet")
    csrf = _login(client)

    res = client.post("/api/chat", json={"message": "Fraga"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200
    body = res.json()
    assert body["sources"] == []
    assert not any("hemlighet" in m.content.lower() for m in _captured_chat_messages[0] if m.role == "system")


def test_chat_response_includes_context_resolver_classification(client, superuser_db, _captured_chat_messages):
    """Confirms app/context/resolver.py (DEL 7) is actually wired into the live /api/chat
    endpoint, not just unit-tested in isolation — a brand-new conversation's first message
    has no history, so it must resolve as a new topic."""
    founder_id = _founder_id(superuser_db)
    _make_source(superuser_db, founder_id, "Aktuellt dokument")
    csrf = _login(client)

    res = client.post("/api/chat", json={"message": "Berätta om kvantdatorer."}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["context_intent"] == "new_topic"
    assert body["context_confidence"] in ("high", "medium", "low")
