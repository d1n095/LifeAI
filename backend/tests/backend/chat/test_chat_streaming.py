"""Streaming chat contract tests.

These tests exercise the shared durable attempt path through the SSE route. They do not
pretend a fake provider is a real model; the provider transport itself has separate parser
tests. A live Ollama/OpenAI run remains a separate, explicitly blocked check.
"""

import json

from app.config import get_settings
from app.models.conversation import Message as MessageModel, MessageRole
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


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in text.split("\n\n"):
        event = "message"
        data = None
        for line in block.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:].strip())
        if data is not None:
            events.append((event, data))
    return events


def test_stream_emits_deltas_and_one_final_message(client, superuser_db, monkeypatch):
    async def _fake_stream(self, messages, model, **kwargs):
        from app.providers.base import ChatStreamChunk

        yield ChatStreamChunk(delta="Hej ", provider="openai", model=model)
        yield ChatStreamChunk(delta="grundare.", provider="openai", model=model)
        yield ChatStreamChunk(
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 3},
            done=True,
        )

    monkeypatch.setattr("app.routers.chat.retrieve_context", _empty_retrieve)
    monkeypatch.setattr(OpenAIProvider, "stream_chat", _fake_stream)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    headers = {"X-CSRF-Token": _login(client)}
    response = client.post(
        "/api/chat/stream",
        json={"message": "streama", "client_message_id": "stream-key-1"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    events = _sse_events(response.text)
    assert [name for name, _ in events] == ["start", "delta", "delta", "done"]
    assert events[1][1]["text"] == "Hej "
    done = events[-1][1]
    assert done["assistant_status"] == "succeeded"
    assert done["reply"] == "Hej grundare."
    assert done["assistant_message_id"]

    rows = superuser_db.query(MessageModel).filter_by(content="Hej grundare.").all()
    assert len(rows) == 1
    assert rows[0].role == MessageRole.assistant


def test_stream_replays_same_key_without_second_provider_call(client, monkeypatch):
    calls: list[str] = []

    async def _fake_stream(self, messages, model, **kwargs):
        from app.providers.base import ChatStreamChunk

        calls.append(model)
        yield ChatStreamChunk(delta="Ett svar.", provider="openai", model=model)
        yield ChatStreamChunk(
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 2},
            done=True,
        )

    monkeypatch.setattr("app.routers.chat.retrieve_context", _empty_retrieve)
    monkeypatch.setattr(OpenAIProvider, "stream_chat", _fake_stream)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    headers = {"X-CSRF-Token": _login(client)}
    payload = {"message": "idempotent stream", "client_message_id": "stream-key-2"}
    first = client.post("/api/chat/stream", json=payload, headers=headers)
    second = client.post("/api/chat/stream", json=payload, headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    first_done = _sse_events(first.text)[-1][1]
    second_done = _sse_events(second.text)[-1][1]
    assert first_done["assistant_message_id"] == second_done["assistant_message_id"]
    assert calls == [get_settings().default_llm_model]


def test_stream_replay_after_disconnect_uses_saved_reply(client, superuser_db, monkeypatch):
    """A saved reply is replayed as SSE without invoking the provider again."""

    async def _fake_stream(self, messages, model, **kwargs):
        from app.providers.base import ChatStreamChunk

        yield ChatStreamChunk(delta="Sparat svar.", provider="openai", model=model)
        yield ChatStreamChunk(
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 2},
            done=True,
        )

    monkeypatch.setattr("app.routers.chat.retrieve_context", _empty_retrieve)
    monkeypatch.setattr(OpenAIProvider, "stream_chat", _fake_stream)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    headers = {"X-CSRF-Token": _login(client)}
    payload = {"message": "replay stream", "client_message_id": "stream-key-3"}
    first = client.post("/api/chat/stream", json=payload, headers=headers)
    assert first.status_code == 200, first.text

    calls: list[str] = []

    async def _must_not_call(self, messages, model, **kwargs):
        calls.append(model)
        yield  # pragma: no cover

    monkeypatch.setattr(OpenAIProvider, "stream_chat", _must_not_call)
    second = client.post("/api/chat/stream", json=payload, headers=headers)
    assert second.status_code == 200, second.text
    assert _sse_events(second.text)[-1][1]["reply"] == "Sparat svar."
    assert calls == []


def test_stream_failure_is_persisted_as_failed_and_retryable(client, superuser_db, monkeypatch):
    async def _failing_stream(self, messages, model, **kwargs):
        from app.providers.base import ProviderError

        raise ProviderError("provider timeout", category="unreachable", provider_request_may_have_left=False)
        yield  # pragma: no cover

    monkeypatch.setattr("app.routers.chat.retrieve_context", _empty_retrieve)
    monkeypatch.setattr(OpenAIProvider, "stream_chat", _failing_stream)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    headers = {"X-CSRF-Token": _login(client)}
    response = client.post(
        "/api/chat/stream",
        json={"message": "stream failure", "client_message_id": "stream-key-4"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    done = _sse_events(response.text)[-1][1]
    assert done["assistant_status"] == "failed"
    assert done["retryable"] is True
    assert done["error_category"] == "unreachable"
