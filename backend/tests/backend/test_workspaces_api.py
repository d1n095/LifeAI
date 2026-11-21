"""Life Workspaces API: persistent project composition, owner scoping and soft retention.

These tests use the real restricted runtime role and the real PostgreSQL schema (see
tests/conftest.py), so the workspace joins exercise the same RLS/ownership boundaries as
production rather than a mocked repository.
"""

import uuid
from datetime import datetime

from app.config import get_settings
from app.models.conversation import Conversation
from app.models.document import Document, DocumentSource
from app.models.project import Project
from app.models.user import User
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _headers(csrf: str) -> dict:
    return {"X-CSRF-Token": csrf}


def _founder_id(superuser_db) -> uuid.UUID:
    return superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).first().id


def _make_document(superuser_db, *, owner_id, title, project_id=None) -> Document:
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        project_id=project_id,
        checksum=uuid.uuid4().hex,
    )
    superuser_db.add(document)
    superuser_db.commit()
    return document


def test_workspace_lifecycle_scoped_collections_and_context(client, superuser_db):
    csrf = _login(client)
    headers = _headers(csrf)

    created = client.post(
        "/api/workspaces",
        json={
            "name": "Rapportpipeline",
            "description": "Gör rapportflödet reproducerbart.",
            "purpose": "Leverera en granskningsbar rapport varje vecka.",
            "goals": ["Automatisera insamlingen", "Behåll spårbarhet"],
            "status": "active",
            "settings": {"timezone": "Europe/Stockholm"},
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    workspace = created.json()
    workspace_id = workspace["id"]
    assert workspace["purpose"].startswith("Leverera")
    assert workspace["goals"] == ["Automatisera insamlingen", "Behåll spårbarhet"]

    task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Samla in veckodata", "status": "todo", "priority": "high"},
        headers=headers,
    )
    assert task.status_code == 200, task.text
    assert task.json()["project_id"] == workspace_id

    decision = client.post(
        f"/api/workspaces/{workspace_id}/decisions",
        json={
            "title": "Veckovis publicering",
            "decision": "Rapporten publiceras varje fredag.",
            "rationale": "Grundaren vill ha en fast granskningsrytm.",
            "status": "accepted",
        },
        headers=headers,
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "accepted"

    founder_id = _founder_id(superuser_db)
    source = _make_document(superuser_db, owner_id=founder_id, title="Veckorapport 2026-11-21")
    associated = client.post(
        f"/api/workspaces/{workspace_id}/documents/{source.id}",
        headers=headers,
    )
    assert associated.status_code == 200, associated.text
    assert associated.json()["project_id"] == workspace_id

    knowledge = client.get(f"/api/workspaces/{workspace_id}/knowledge")
    assert knowledge.status_code == 200, knowledge.text
    assert [item["id"] for item in knowledge.json()] == [str(source.id)]

    detail = client.get(f"/api/workspaces/{workspace_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == workspace_id
    assert [item["title"] for item in body["tasks"]] == ["Samla in veckodata"]
    assert [item["title"] for item in body["decisions"]] == ["Veckovis publicering"]
    assert [item["id"] for item in body["sources"]] == [str(source.id)]
    assert body["context"]["objectives"][0].startswith("Leverera")
    assert body["context"]["outstanding_tasks"][0]["title"] == "Samla in veckodata"
    assert body["context"]["recent_changes"]

    updated = client.patch(
        f"/api/workspaces/{workspace_id}",
        json={"name": "Rapportpipeline v2", "goals": ["Automatisera", "Mät kvalitet"]},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["goals"] == ["Automatisera", "Mät kvalitet"]

    archived = client.post(f"/api/workspaces/{workspace_id}/archive", headers=headers)
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None
    assert all(item["id"] != workspace_id for item in client.get("/api/workspaces").json())
    assert any(
        item["id"] == workspace_id
        for item in client.get("/api/workspaces?include_archived=true").json()
    )

    restored = client.post(f"/api/workspaces/{workspace_id}/restore", headers=headers)
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived_at"] is None

    deleted = client.delete(f"/api/workspaces/{workspace_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["retained"] is True
    assert client.get(f"/api/workspaces/{workspace_id}").status_code == 404
    assert superuser_db.query(Project).filter_by(id=uuid.UUID(workspace_id)).one().deleted_at is not None


def test_workspace_owner_scope_hides_foreign_project_and_document(client, superuser_db, make_verified_user):
    csrf = _login(client)
    headers = _headers(csrf)
    other_user, _ = make_verified_user(email="workspace-other@example.com")

    foreign = Project(
        name="Annan ägares workspace",
        created_by=other_user.id,
        status="active",
        goals=[],
        settings={},
        last_activity_at=datetime.utcnow(),
    )
    superuser_db.add(foreign)
    superuser_db.commit()

    assert client.get(f"/api/workspaces/{foreign.id}").status_code == 404
    assert client.patch(
        f"/api/workspaces/{foreign.id}", json={"name": "kapad"}, headers=headers
    ).status_code == 404
    assert client.post(f"/api/workspaces/{foreign.id}/archive", headers=headers).status_code == 404

    own = client.post(
        "/api/workspaces",
        json={"name": "Egen workspace", "status": "active"},
        headers=headers,
    ).json()
    foreign_document = _make_document(
        superuser_db, owner_id=other_user.id, title="Annan ägares källa"
    )
    assert client.post(
        f"/api/workspaces/{own['id']}/documents/{foreign_document.id}", headers=headers
    ).status_code == 404


def test_workspace_conversation_is_persisted_with_the_workspace_link(client):
    csrf = _login(client)
    headers = _headers(csrf)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Samtalsworkspace", "status": "active"},
        headers=headers,
    ).json()

    conversation = client.post(
        f"/api/workspaces/{workspace['id']}/conversations", headers=headers
    )
    assert conversation.status_code == 200, conversation.text
    conversation_body = conversation.json()
    assert conversation_body["project_id"] == workspace["id"]

    listed = client.get(f"/api/workspaces/{workspace['id']}/conversations")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [conversation_body["id"]]

    detail = client.get(f"/api/conversations/{conversation_body['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["project_id"] == workspace["id"]


def test_chat_binds_new_conversation_to_workspace_and_scopes_retrieval(
    client, superuser_db, monkeypatch
):
    csrf = _login(client)
    headers = _headers(csrf)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Chatt-workspace", "status": "active"},
        headers=headers,
    ).json()
    other_workspace = client.post(
        "/api/workspaces",
        json={"name": "Annat chatt-workspace", "status": "active"},
        headers=headers,
    ).json()

    retrieval_calls: list[dict] = []

    async def _fake_retrieve(_db, _owner_id, _query, **kwargs):
        retrieval_calls.append(kwargs)
        return []

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(
            content="Workspace-svar.",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 2},
        )

    monkeypatch.setattr("app.routers.chat.retrieve_context", _fake_retrieve)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    response = client.post(
        "/api/chat",
        json={"message": "Vad vet workspaceet?", "project_id": workspace["id"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    conversation = superuser_db.query(Conversation).filter_by(id=uuid.UUID(body["conversation_id"])).one()
    assert conversation.project_id == uuid.UUID(workspace["id"])
    assert retrieval_calls and retrieval_calls[0]["project_id"] == uuid.UUID(workspace["id"])

    # A conversation bound to one workspace cannot be silently re-used for another one.
    conflict = client.post(
        "/api/chat",
        json={
            "message": "Fortsätt i fel workspace",
            "conversation_id": body["conversation_id"],
            "project_id": other_workspace["id"],
        },
        headers=headers,
    )
    assert conflict.status_code == 409, conflict.text
