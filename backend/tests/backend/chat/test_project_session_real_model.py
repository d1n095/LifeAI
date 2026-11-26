"""Real local-model acceptance for the MainAI project conversation.

This extends `test_project_session_resume.py` rather than replacing it: the same HTTP +
Postgres journey (login, open project, load tasks, chat with project context, reload, resume,
deny another founder), but the assistant reply is produced by a real Ollama model instead of
a fake transport. When the model is not installed the module skips with the environmental
reason, exactly like `test_chat_real_local_model.py`.

The assertions are deliberately split:
  * model-controlled: the reply is non-empty and carries ollama/<model> provenance;
  * deterministic: the project context reached the model, both rows are durable, a backend
    lifespan restart and two real worker cycles change nothing, the resumed turn receives the
    prior exchange, and another founder is denied.

No parallel chat service is introduced: this drives the same `POST /api/chat`,
`GET /api/conversations/{id}` and `app.worker.Worker` used in production.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
import uuid
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.models.conversation import Message as MessageModel, MessageRole, MessageStatus
from app.models.provider_verification import VerificationResult
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider
from app.worker import Worker

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
OLLAMA_URL = "http://127.0.0.1:11434"
CHAT_MODEL = "qwen2.5:0.5b"


def _installed_models() -> set[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as response:
            payload = json.loads(response.read())
    except Exception:
        return set()
    return {str(model.get("name", "")) for model in payload.get("models", [])}


pytestmark = pytest.mark.skipif(
    CHAT_MODEL not in _installed_models(),
    reason=f"real local model {CHAT_MODEL} not installed at {OLLAMA_URL}",
)


def _login(client: TestClient, email: str = FOUNDER_EMAIL, password: str = FOUNDER_PASSWORD) -> str:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


@pytest.fixture
def local_model(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "default_llm_provider", "ollama")
    monkeypatch.setattr(settings, "default_llm_model", CHAT_MODEL)
    monkeypatch.setattr(settings, "ollama_base_url", OLLAMA_URL)
    # Never let a failed local call silently become a paid API call.
    monkeypatch.setattr(settings, "chat_fallback_order", "")

    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * settings.embedding_dim for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    return settings


@pytest.fixture
def captured_real_chat(monkeypatch):
    """Record the exact prompt each real Ollama call receives, then delegate to Ollama."""

    captured: list[list] = []
    original = OllamaProvider.chat

    async def _chat(self, messages, model, **kwargs):
        captured.append(list(messages))
        return await original(self, messages, model=model, **kwargs)

    monkeypatch.setattr(OllamaProvider, "chat", _chat)
    return captured


@pytest.fixture
def worker_without_embedding_probe(monkeypatch):
    """A real worker cycle, with only the embedding verification network call stubbed.

    The worker's blocked-job requeue asks the active embedding provider for a real
    verification check. This environment has no embedding model installed, and the point of
    this test is the worker's own start/stop cycle, not an external provider probe.
    """

    async def _not_configured(db, role, **kwargs):
        return SimpleNamespace(result=VerificationResult.not_configured, message="test")

    monkeypatch.setattr("app.worker.ensure_verified", _not_configured)


def test_project_journey_with_real_model_survives_backend_and_worker_restart(
    restartable_client,
    superuser_db,
    make_verified_user,
    local_model,
    captured_real_chat,
    worker_without_embedding_probe,
):
    client = restartable_client()
    headers = {"X-CSRF-Token": _login(client)}

    project = client.post(
        "/api/projects",
        json={
            "name": "MainAI verklig modell",
            "description": "Ett projekt som ska överleva omstart",
            "status": "active",
        },
        headers=headers,
    )
    assert project.status_code == 200, project.text
    project_id = project.json()["id"]

    task = client.post(
        "/api/projects/tasks",
        json={
            "title": "Verifiera verklig modell",
            "description": "Kontexten ska nå en riktig lokal modell.",
            "project_id": project_id,
            "status": "todo",
        },
        headers=headers,
    )
    assert task.status_code == 201, task.text

    first = client.post(
        "/api/chat",
        json={
            "message": "Var är vi i projektet? Svara kort på svenska.",
            "project_id": project_id,
            "client_message_id": "real-model-project-1",
        },
        headers=headers,
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["assistant_status"] == "succeeded", first_body
    assert first_body["provider"] == "ollama", first_body
    assert first_body["model"] == CHAT_MODEL, first_body
    assert first_body["reply"].strip(), first_body
    conversation_id = first_body["conversation_id"]

    # Deterministic: the real model's prompt contained the project name and the open task.
    system_prompt = captured_real_chat[0][0].content
    assert "MainAI verklig modell" in system_prompt
    assert "Verifiera verklig modell" in system_prompt
    assert "[todo]" in system_prompt

    # Deterministic: the real reply is what was persisted, with real provenance.
    stored = (
        superuser_db.query(MessageModel)
        .filter_by(conversation_id=uuid.UUID(conversation_id))
        .order_by(MessageModel.created_at.asc(), MessageModel.id.asc())
        .all()
    )
    assert [row.role for row in stored] == [MessageRole.user, MessageRole.assistant]
    assert stored[0].content == "Var är vi i projektet? Svara kort på svenska."
    assert stored[1].content == first_body["reply"]
    assert stored[1].status == MessageStatus.succeeded
    assert stored[1].provider == "ollama" and stored[1].model == CHAT_MODEL

    reloaded = client.get(f"/api/conversations/{conversation_id}")
    assert reloaded.status_code == 200, reloaded.text
    assert [item["role"] for item in reloaded.json()["messages"]] == ["user", "assistant"]

    # Worker restart: two real, complete poll cycles in two separate Worker instances.
    first_cycle = asyncio.run(Worker().run_once())
    second_cycle = asyncio.run(Worker().run_once())
    assert first_cycle is False and second_cycle is False
    assert (
        superuser_db.query(MessageModel)
        .filter_by(conversation_id=uuid.UUID(conversation_id))
        .count()
        == 2
    )

    # End the test session's read transaction first: apply_rls() at startup takes table
    # locks, and an open transaction here would block the restart exactly as a stray client
    # would in production.
    superuser_db.rollback()

    # Backend restart: the fixture closes the previous lifespan before opening the next,
    # exactly like a real process restart.
    restarted = restartable_client()
    with nullcontext(restarted) as restarted:
        restarted_headers = {"X-CSRF-Token": _login(restarted)}

        reloaded = restarted.get(f"/api/conversations/{conversation_id}")
        assert reloaded.status_code == 200, reloaded.text
        history = reloaded.json()["messages"]
        assert [item["role"] for item in history] == ["user", "assistant"]
        assert history[0]["content"] == "Var är vi i projektet? Svara kort på svenska."
        assert history[1]["content"] == first_body["reply"]

        second = restarted.post(
            "/api/chat",
            json={
                "message": "Fortsätt där vi slutade. Svara kort.",
                "conversation_id": conversation_id,
                "client_message_id": "real-model-project-2",
            },
            headers=restarted_headers,
        )
        assert second.status_code == 200, second.text
        second_body = second.json()
        assert second_body["assistant_status"] == "succeeded", second_body
        assert second_body["reply"].strip(), second_body
        assert second_body["provider"] == "ollama", second_body
        assert second_body["conversation_id"] == conversation_id

        # Deterministic: the resumed turn sent the pre-restart exchange to the real model.
        resumed_prompt = [message.content for message in captured_real_chat[1]]
        assert "Var är vi i projektet? Svara kort på svenska." in resumed_prompt
        assert first_body["reply"] in resumed_prompt

        final = restarted.get(f"/api/conversations/{conversation_id}")
        assert final.status_code == 200
        assert [item["role"] for item in final.json()["messages"]] == [
            "user",
            "assistant",
            "user",
            "assistant",
        ]

        # Another founder cannot reach the conversation or the project.
        other, other_password = make_verified_user(
            email="other-founder-real-model@example.com", role="founder"
        )
        other_headers = {"X-CSRF-Token": _login(restarted, other.email, other_password)}
        assert restarted.get(f"/api/conversations/{conversation_id}").status_code == 403
        assert restarted.get(f"/api/projects/{project_id}").status_code == 403
        denied = restarted.post(
            "/api/chat",
            json={"message": "Fortsätt", "conversation_id": conversation_id},
            headers=other_headers,
        )
        assert denied.status_code == 403

    user_rows = (
        superuser_db.query(MessageModel)
        .filter_by(role=MessageRole.user)
        .order_by(MessageModel.created_at.asc(), MessageModel.id.asc())
        .all()
    )
    assert [row.content for row in user_rows] == [
        "Var är vi i projektet? Svara kort på svenska.",
        "Fortsätt där vi slutade. Svara kort.",
    ]
