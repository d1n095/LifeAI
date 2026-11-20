"""R15 successor: POST /api/chat client idempotency.

A client retry of the same logical send (same ``client_message_id``) must replay the existing
assistant reply and must never create a second user message or a second provider call. Reusing
the key for different content is a conflict, not a silent alias.
"""

from app.config import get_settings
from app.models.conversation import Message as MessageModel, MessageRole
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    response = client.post(
        "/api/auth/login",
        json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


async def _empty_retrieve(*args, **kwargs):
    return []


def test_same_client_message_id_replays_without_second_provider_call(
    client, superuser_db, monkeypatch
):
    calls: list[str] = []

    async def _fake_chat(self, messages, model, **kwargs):
        calls.append(model)
        return ChatResult(
            content="Svar.",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 2},
        )

    monkeypatch.setattr("app.routers.chat.retrieve_context", _empty_retrieve)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    headers = {"X-CSRF-Token": _login(client)}
    payload = {"message": "idem-message", "client_message_id": "idem-key-1"}

    first = client.post("/api/chat", json=payload, headers=headers)
    second = client.post("/api/chat", json=payload, headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["assistant_status"] == "succeeded"
    assert second.json()["assistant_status"] == "succeeded"
    assert first.json()["assistant_message_id"] == second.json()["assistant_message_id"]
    assert calls == [get_settings().default_llm_model]
    assert (
        superuser_db.query(MessageModel)
        .filter_by(role=MessageRole.user, content="idem-message")
        .count()
        == 1
    )


def test_same_client_message_id_with_different_content_is_a_conflict(
    client, monkeypatch
):
    calls: list[str] = []

    async def _fake_chat(self, messages, model, **kwargs):
        calls.append(model)
        return ChatResult(
            content="Svar.",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 2},
        )

    monkeypatch.setattr("app.routers.chat.retrieve_context", _empty_retrieve)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    headers = {"X-CSRF-Token": _login(client)}
    first = client.post(
        "/api/chat",
        json={"message": "first-content", "client_message_id": "idem-key-2"},
        headers=headers,
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/chat",
        json={"message": "different-content", "client_message_id": "idem-key-2"},
        headers=headers,
    )
    assert second.status_code == 409
    assert calls == [get_settings().default_llm_model]
