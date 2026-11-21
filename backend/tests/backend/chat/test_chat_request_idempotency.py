"""R15 successor: POST /api/chat client idempotency.

A client retry of the same logical send (same ``client_message_id``) must replay the existing
assistant reply and must never create a second user message or a second provider call. Reusing
the key for different content is a conflict, not a silent alias.
"""

from app.config import get_settings
from app.models.conversation import (
    ChatRequestClaim,
    Conversation,
    Message as MessageModel,
    MessageRole,
)
from app.models.user import User
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


def test_claim_without_reply_slot_recovers_after_a_crash(
    client, superuser_db, monkeypatch
):
    """A crash after the durable claim but before the reply-slot claim must be recoverable.

    The original request commits the conversation, user message and ChatRequestClaim, then
    dies before ``_claim_reply_attempt()`` creates the assistant row. A retry with the same
    key used to get a permanent 409 because ``_replay_chat_request`` treated a missing reply
    row as an in-flight request. It must instead re-enter the one shared attempt path.
    """

    calls: list[str] = []

    async def _fake_chat(self, messages, model, **kwargs):
        calls.append(model)
        return ChatResult(
            content="Återställt svar.",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 2},
        )

    monkeypatch.setattr("app.routers.chat.retrieve_context", _empty_retrieve)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    founder = superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).one()
    conversation = Conversation(title="crash-window", user_id=founder.id)
    superuser_db.add(conversation)
    superuser_db.flush()
    user_message = MessageModel(
        conversation_id=conversation.id,
        role=MessageRole.user,
        content="meddelande före kraschen",
    )
    superuser_db.add(user_message)
    superuser_db.flush()
    superuser_db.add(
        ChatRequestClaim(
            owner_id=founder.id,
            client_message_id="crash-window-key",
            conversation_id=conversation.id,
            user_message_id=user_message.id,
        )
    )
    superuser_db.commit()

    headers = {"X-CSRF-Token": _login(client)}
    response = client.post(
        "/api/chat",
        json={
            "message": "meddelande före kraschen",
            "client_message_id": "crash-window-key",
        },
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assistant_status"] == "succeeded"
    assert body["user_message_id"] == str(user_message.id)
    assert body["reply"] == "Återställt svar."
    assert calls == [get_settings().default_llm_model]
    assert (
        superuser_db.query(MessageModel)
        .filter_by(role=MessageRole.user, content="meddelande före kraschen")
        .count()
        == 1
    )
    assert (
        superuser_db.query(MessageModel)
        .filter_by(in_reply_to_id=user_message.id)
        .count()
        == 1
    )
