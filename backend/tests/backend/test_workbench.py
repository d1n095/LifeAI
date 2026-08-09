"""API-level tests for DEL 9 (Founder Workbench, app/routers/workbench.py). Same fake
chat+embedding provider pattern as tests/backend/chat/test_chat_source_grounding.py — a deterministic fake
response with an explicit KRITIK/ALTERNATIV: marker so the conclusion/critique split can be
asserted on directly, never a real AI key."""

import uuid

import pytest

from app.config import get_settings
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.user import User

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
DIM = get_settings().embedding_dim
MATCHING_VECTOR = [0.5] * DIM

FAKE_ANSWER = "SLUTSATS: Detta ar den grundade slutsatsen.\nKRITIK/ALTERNATIV: Men det finns en alternativ tolkning att overvaga."


@pytest.fixture
def _fake_provider(monkeypatch):
    from app.providers.base import ChatResult
    from app.providers.openai_provider import OpenAIProvider

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(content=FAKE_ANSWER, provider="openai", model=model, raw_usage={"prompt_tokens": 12, "completion_tokens": 8})

    async def _fake_embed(self, texts, model, **kwargs):
        return [MATCHING_VECTOR for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(OpenAIProvider, "embed", _fake_embed)


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def _founder_id(superuser_db) -> uuid.UUID:
    return superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).first().id


def _make_source(superuser_db, owner_id, title, *, project_id=None, status=ActiveTruthStatus.active) -> Document:
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        project_id=project_id,
        active_truth_status=status,
        checksum=uuid.uuid4().hex,
    )
    superuser_db.add(document)
    superuser_db.commit()
    superuser_db.add(
        DocumentChunk(document_id=document.id, owner_id=owner_id, chunk_index=0, text=f"Innehall i {title}.", embedding=MATCHING_VECTOR)
    )
    superuser_db.commit()
    return document


def test_analyze_requires_founder(client, make_verified_user):
    user, password = make_verified_user()
    login = client.post("/api/auth/login", json={"email": user.email, "password": password})
    assert login.status_code == 200
    res = client.post("/api/workbench/analyze", json={"question": "Vad galler?"})
    assert res.status_code == 403


def test_analyze_splits_conclusion_and_critique(client, superuser_db, _fake_provider):
    founder_id = _founder_id(superuser_db)
    _make_source(superuser_db, founder_id, "Analyskalla")
    csrf = _login(client)

    res = client.post("/api/workbench/analyze", json={"question": "Vad bor vi gora?"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text
    body = res.json()
    assert "Detta ar den grundade slutsatsen." in body["conclusion"]
    assert "KRITIK/ALTERNATIV" not in body["conclusion"]
    assert body["critique"] is not None
    assert "alternativ tolkning" in body["critique"]
    assert len(body["sources"]) == 1
    assert body["confidence"] == "high"


def test_analyze_scoped_to_a_single_document_ignores_other_sources(client, superuser_db, _fake_provider):
    founder_id = _founder_id(superuser_db)
    target = _make_source(superuser_db, founder_id, "Rätt källa")
    _make_source(superuser_db, founder_id, "Fel källa")
    csrf = _login(client)

    res = client.post(
        "/api/workbench/analyze",
        json={"question": "Fraga om en specifik kalla", "document_id": str(target.id)},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert len(body["sources"]) == 1
    assert body["sources"][0]["document_id"] == str(target.id)


def test_analyze_with_unknown_document_id_is_404(client, superuser_db, _fake_provider):
    _founder_id(superuser_db)
    csrf = _login(client)
    res = client.post(
        "/api/workbench/analyze",
        json={"question": "test", "document_id": str(uuid.uuid4())},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 404


def test_save_creates_searchable_document_with_correct_label_mapping(client, superuser_db, _fake_provider):
    founder_id = _founder_id(superuser_db)
    source = _make_source(superuser_db, founder_id, "Ursprungskalla")
    csrf = _login(client)

    res = client.post(
        "/api/workbench/save",
        json={
            "question": "Ska vi lansera nu?",
            "conclusion": "Ja, lansera i augusti.",
            "critique": "Risk: marknadsforingen ar inte klar.",
            "label": "decision",
            "source_document_ids": [str(source.id)],
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["classification"] == "decisions"
    assert body["active_truth_status"] == "active"
    assert body["source"] == "manual"

    detail = client.get(f"/api/library/{body['id']}")
    assert detail.status_code == 200
    relationships = detail.json()["relationships"]
    assert len(relationships) == 1
    assert relationships[0]["relationship_type"] == "derived_from"
    assert relationships[0]["to_source_id"] == str(source.id)


def test_save_idea_and_history_labels_map_to_proposed_and_historical(client, superuser_db, _fake_provider):
    _founder_id(superuser_db)
    csrf = _login(client)

    idea = client.post(
        "/api/workbench/save",
        json={"question": "En ide", "conclusion": "Kanske detta.", "label": "idea"},
        headers={"X-CSRF-Token": csrf},
    )
    assert idea.status_code == 200
    assert idea.json()["active_truth_status"] == "proposed"

    history = client.post(
        "/api/workbench/save",
        json={"question": "Ett gammalt beslut", "conclusion": "Sa gjorde vi da.", "label": "history"},
        headers={"X-CSRF-Token": csrf},
    )
    assert history.status_code == 200
    assert history.json()["active_truth_status"] == "historical"
    assert history.json()["classification"] == "history"


def test_save_ignores_source_ids_not_owned_by_caller(client, superuser_db, make_verified_user, _fake_provider):
    founder_id = _founder_id(superuser_db)
    other, _ = make_verified_user(email="workbench-other@example.com")
    others_doc = _make_source(superuser_db, other.id, "Nagon annans kalla")
    csrf = _login(client)

    res = client.post(
        "/api/workbench/save",
        json={"question": "test", "conclusion": "test", "label": "idea", "source_document_ids": [str(others_doc.id)]},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200, res.text

    detail = client.get(f"/api/library/{res.json()['id']}")
    assert detail.json()["relationships"] == []  # no relationship created to a source we don't own


def test_save_invalid_label_is_rejected(client, superuser_db, _fake_provider):
    _founder_id(superuser_db)
    csrf = _login(client)
    res = client.post(
        "/api/workbench/save",
        json={"question": "test", "conclusion": "test", "label": "not-a-real-label"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 422
