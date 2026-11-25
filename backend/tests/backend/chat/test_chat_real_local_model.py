"""Real local-model conversation through the production chat path.

Not a mock: Ollama with `qwen2.5:0.5b` must be reachable and have that model installed, or the
module skips with the environmental reason. The embedding role is faked the same way the
persistence suite fakes it, because only the chat model is installed here; retrieval degradation
is a separate, already-covered path and must not decide whether a real reply is possible.

What this proves (Scenario A): login -> POST /api/chat -> the real model answers -> both rows
are persisted with provider/model provenance -> a follow-up in the same conversation works ->
GET /api/conversations/{id} returns the whole history, i.e. the conversation can be resumed.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from app.config import get_settings
from app.models.conversation import Message as MessageModel, MessageRole, MessageStatus
from app.providers.openai_provider import OpenAIProvider

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


def _login(client) -> str:
    response = client.post(
        "/api/auth/login",
        json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD},
    )
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


def test_real_local_model_answers_and_conversation_can_be_resumed(
    client, superuser_db, local_model
):
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}

    first = client.post(
        "/api/chat",
        json={"message": "Svara med en kort hälsning på svenska."},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["assistant_status"] == "succeeded", body
    assert body["reply"].strip(), body
    assert body["provider"] == "ollama", body
    assert body["model"] == CHAT_MODEL, body
    conversation_id = body["conversation_id"]

    rows = (
        superuser_db.query(MessageModel)
        .filter_by(conversation_id=conversation_id)
        .order_by(MessageModel.created_at, MessageModel.id)
        .all()
    )
    assert [row.role for row in rows] == [MessageRole.user, MessageRole.assistant]
    assert rows[1].status == MessageStatus.succeeded
    assert rows[1].provider == "ollama" and rows[1].model == CHAT_MODEL
    assert rows[1].content.strip()

    second = client.post(
        "/api/chat",
        json={
            "message": "Upprepa den hälsningen kort.",
            "conversation_id": conversation_id,
        },
        headers=headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["conversation_id"] == conversation_id
    assert second.json()["assistant_status"] == "succeeded", second.json()

    reloaded = client.get(f"/api/conversations/{conversation_id}")
    assert reloaded.status_code == 200, reloaded.text
    history = reloaded.json()["messages"]
    assert len(history) == 4
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "user",
        "assistant",
    ]
