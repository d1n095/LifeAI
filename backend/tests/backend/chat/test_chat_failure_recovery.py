"""Failure and recovery around the MainAI project conversation.

Every test drives the real HTTP API and the real Postgres test database. The model transport
is the only fake (except where the test deliberately points Ollama at a closed port), and
retrieval is isolated to an empty result so the assertions are about durability, idempotency,
permissions and recovery rather than RAG quality.

What is pinned here, one scenario per test:
  * a stopped Ollama before the send keeps the user's message and can be retried;
  * an interruption mid-generation keeps partial text out of the stored history and blocks
    a blind retry that could double-charge an uncertain external effect;
  * a backend restart preserves the conversation and the next turn receives the history;
  * re-sending the same logical message after a restart replays it without a second call;
  * a missing project is refused before any message is stored;
  * an unavailable project-context derivation degrades the prompt but not the turn;
  * a policy that forbids fallback never reaches an external provider;
  * another founder cannot read the conversation or the project.
"""

from __future__ import annotations

import json
import uuid

from fastapi.testclient import TestClient

from app.config import get_settings
from app.models.conversation import (
    Conversation,
    Message as MessageModel,
    MessageRole,
    MessageStatus,
)
from app.providers.base import ChatResult, ChatStreamChunk, ProviderError
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"
CHAT_MODEL = "qwen2.5:0.5b"
CLOSED_OLLAMA = "http://127.0.0.1:1"  # nothing listens here; connect is refused immediately
REPLY = "Svar från den lokala modellen."


def _login(client: TestClient, email: str = FOUNDER_EMAIL, password: str = FOUNDER_PASSWORD) -> str:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


async def _empty_retrieve(*args, **kwargs):
    return []


def _local_only(monkeypatch, base_url: str) -> None:
    """Same routing shape as production local-first chat: Ollama primary, no fallback."""

    settings = get_settings()
    monkeypatch.setattr(settings, "default_llm_provider", "ollama")
    monkeypatch.setattr(settings, "default_llm_model", CHAT_MODEL)
    monkeypatch.setattr(settings, "ollama_base_url", base_url)
    monkeypatch.setattr(settings, "chat_fallback_order", "")
    monkeypatch.setattr("app.routers.chat.retrieve_context", _empty_retrieve)

    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * settings.embedding_dim for _ in texts]

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)


def _fake_ollama(monkeypatch, content: str = REPLY) -> list[list]:
    calls: list[list] = []

    async def _chat(self, messages, model, **kwargs):
        calls.append(list(messages))
        return ChatResult(
            content=content,
            provider="ollama",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 5},
        )

    monkeypatch.setattr(OllamaProvider, "chat", _chat)
    return calls


def _forbid_external(monkeypatch) -> list[int]:
    calls: list[int] = []

    async def _chat(self, *args, **kwargs):
        calls.append(1)
        raise AssertionError("an external provider must never be reached here")

    monkeypatch.setattr(OpenAIProvider, "chat", _chat)
    return calls


def _create_project(client: TestClient, headers: dict, name: str = "MainAI återhämtning") -> str:
    response = client.post(
        "/api/projects",
        json={"name": name, "description": "Ett verkligt projekt", "status": "active"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _sse_events(payload: str) -> list[tuple[str | None, dict | None]]:
    events: list[tuple[str | None, dict | None]] = []
    for block in payload.split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        event = next((line[7:] for line in lines if line.startswith("event: ")), None)
        data = next((line[6:] for line in lines if line.startswith("data: ")), None)
        events.append((event, json.loads(data) if data is not None else None))
    return events


def test_ollama_down_before_send_keeps_the_message_and_retry_recovers(
    client, superuser_db, monkeypatch
):
    _local_only(monkeypatch, CLOSED_OLLAMA)
    _forbid_external(monkeypatch)
    headers = {"X-CSRF-Token": _login(client)}

    response = client.post(
        "/api/chat",
        json={"message": "Första frågan", "client_message_id": "down-before-send"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assistant_status"] == "failed", body
    # The connection was never established, so this is a retryable outage — not an
    # unresolved external effect that would block the founder forever.
    assert body["error_category"] == "unreachable", body
    assert body["retryable"] is True, body
    assert body["provider"] is None, body

    conversation_id = body["conversation_id"]
    user_message_id = body["user_message_id"]

    reloaded = client.get(f"/api/conversations/{conversation_id}")
    assert reloaded.status_code == 200, reloaded.text
    messages = reloaded.json()["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Första frågan"
    assert messages[1]["status"] == "failed"
    assert messages[1]["content"] == ""

    # Ollama comes back; the same durable user message is retried, not re-created.
    calls = _fake_ollama(monkeypatch)
    retried = client.post(f"/api/chat/messages/{user_message_id}/retry", headers=headers)
    assert retried.status_code == 200, retried.text
    retried_body = retried.json()
    assert retried_body["assistant_status"] == "succeeded", retried_body
    assert retried_body["assistant_message_id"] == body["assistant_message_id"]
    assert retried_body["reply"] == REPLY
    assert len(calls) == 1

    assert superuser_db.query(MessageModel).filter_by(role=MessageRole.user).count() == 1
    assert (
        superuser_db.query(MessageModel)
        .filter_by(in_reply_to_id=uuid.UUID(user_message_id))
        .count()
        == 1
    )


def test_ollama_interrupted_mid_generation_keeps_partial_text_out_of_history(
    client, superuser_db, monkeypatch
):
    _local_only(monkeypatch, "http://127.0.0.1:11434")
    headers = {"X-CSRF-Token": _login(client)}

    async def _interrupted_stream(self, messages, model, **kwargs):
        yield ChatStreamChunk(delta="Delvis")
        raise ProviderError(
            "Ollama avbröts under generering.",
            category="unreachable",
            provider_request_may_have_left=True,
        )

    monkeypatch.setattr(OllamaProvider, "stream_chat", _interrupted_stream)

    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"message": "Strömma ett svar", "client_message_id": "stream-interrupted"},
        headers=headers,
    ) as response:
        assert response.status_code == 200, response.text
        payload = "".join(response.iter_text())

    events = _sse_events(payload)
    deltas = [data["text"] for event, data in events if event == "delta"]
    done = [data for event, data in events if event == "done"]
    assert deltas == ["Delvis"]
    assert done, payload
    assert done[-1]["assistant_status"] == "failed", done[-1]
    assert done[-1]["error_category"] == "external_effect_unknown", done[-1]
    assert done[-1]["retryable"] is False, done[-1]

    conversation_id = done[-1]["conversation_id"]
    user_message_id = done[-1]["user_message_id"]

    stored = (
        superuser_db.query(MessageModel)
        .filter_by(conversation_id=uuid.UUID(conversation_id))
        .order_by(MessageModel.created_at.asc(), MessageModel.id.asc())
        .all()
    )
    assert [row.role for row in stored] == [MessageRole.user, MessageRole.assistant]
    assert stored[0].content == "Strömma ett svar"
    assert stored[1].status == MessageStatus.failed
    assert stored[1].content == ""  # partial text was shown, never stored as an answer

    # A blind retry and a duplicate send are both refused while the outcome is unresolved.
    assert client.post(f"/api/chat/messages/{user_message_id}/retry", headers=headers).status_code == 409
    duplicate = client.post(
        "/api/chat",
        json={"message": "Strömma ett svar", "client_message_id": "stream-interrupted"},
        headers=headers,
    )
    assert duplicate.status_code == 409
    assert superuser_db.query(MessageModel).filter_by(role=MessageRole.user).count() == 1


def test_backend_restart_keeps_the_conversation_and_the_next_turn_gets_history(
    restartable_client, superuser_db, make_verified_user, monkeypatch
):
    _local_only(monkeypatch, "http://127.0.0.1:11434")
    calls = _fake_ollama(monkeypatch)
    client = restartable_client()
    headers = {"X-CSRF-Token": _login(client)}
    project_id = _create_project(client, headers)

    first = client.post(
        "/api/chat",
        json={
            "message": "Var är vi i projektet?",
            "project_id": project_id,
            "client_message_id": "restart-first",
        },
        headers=headers,
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["assistant_status"] == "succeeded", first_body
    conversation_id = first_body["conversation_id"]

    # The model received the project context before the restart.
    assert "MainAI återhämtning" in calls[0][0].content

    # A fresh TestClient closes the previous lifespan and runs startup/shutdown again: the
    # backend process is restarted while the conversation is already durable in Postgres.
    restarted = restartable_client()
    restarted_headers = {"X-CSRF-Token": _login(restarted)}

    reloaded = restarted.get(f"/api/conversations/{conversation_id}")
    assert reloaded.status_code == 200, reloaded.text
    history = reloaded.json()["messages"]
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[0]["content"] == "Var är vi i projektet?"
    assert history[1]["content"] == REPLY

    second = restarted.post(
        "/api/chat",
        json={
            "message": "Fortsätt där vi slutade.",
            "conversation_id": conversation_id,
            "client_message_id": "restart-second",
        },
        headers=restarted_headers,
    )
    assert second.status_code == 200, second.text
    assert second.json()["assistant_status"] == "succeeded", second.json()

    # The resumed turn carried the pre-restart exchange into the model prompt.
    resumed_prompt = [message.content for message in calls[1]]
    assert "Var är vi i projektet?" in resumed_prompt
    assert REPLY in resumed_prompt

    # Another founder cannot read the conversation or the project after the restart.
    other, other_password = make_verified_user(
        email="other-founder-failure@example.com", role="founder"
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

    stored_users = (
        superuser_db.query(MessageModel).filter_by(role=MessageRole.user).all()
    )
    assert [row.content for row in stored_users] == [
        "Var är vi i projektet?",
        "Fortsätt där vi slutade.",
    ]
    assert superuser_db.query(Conversation).count() == 1


def test_resending_the_same_message_after_restart_replays_without_a_second_call(
    restartable_client, superuser_db, monkeypatch
):
    _local_only(monkeypatch, "http://127.0.0.1:11434")
    calls = _fake_ollama(monkeypatch)
    client = restartable_client()
    headers = {"X-CSRF-Token": _login(client)}
    payload = {"message": "Samma meddelande", "client_message_id": "restart-idempotent"}

    first = client.post("/api/chat", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    assert first.json()["assistant_status"] == "succeeded"

    restarted = restartable_client()
    restarted_headers = {"X-CSRF-Token": _login(restarted)}
    second = restarted.post("/api/chat", json=payload, headers=restarted_headers)
    assert second.status_code == 200, second.text
    assert second.json()["assistant_message_id"] == first.json()["assistant_message_id"]
    assert second.json()["reply"] == REPLY

    assert len(calls) == 1
    assert superuser_db.query(MessageModel).filter_by(role=MessageRole.user).count() == 1
    assert superuser_db.query(MessageModel).filter_by(role=MessageRole.assistant).count() == 1


def test_missing_project_is_refused_before_any_message_is_stored(
    client, superuser_db, monkeypatch
):
    _local_only(monkeypatch, CLOSED_OLLAMA)
    headers = {"X-CSRF-Token": _login(client)}

    response = client.post(
        "/api/chat",
        json={"message": "Var är vi?", "project_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert superuser_db.query(Conversation).count() == 0
    assert superuser_db.query(MessageModel).count() == 0


def test_unavailable_project_context_degrades_without_losing_the_turn(
    client, superuser_db, monkeypatch
):
    _local_only(monkeypatch, "http://127.0.0.1:11434")
    calls = _fake_ollama(monkeypatch)
    headers = {"X-CSRF-Token": _login(client)}
    project_id = _create_project(client, headers)

    def _unavailable(*args, **kwargs):
        raise RuntimeError("project register is unavailable")

    monkeypatch.setattr("app.routers.chat.format_project_context_instructions", _unavailable)

    response = client.post(
        "/api/chat",
        json={"message": "Var är vi?", "project_id": project_id},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assistant_status"] == "succeeded", body
    assert body["reply"] == REPLY
    # The block failed open: the turn still ran, just without the project context.
    assert "PROJEKTKONTEXT:" not in calls[0][0].content

    detail = client.get(f"/api/conversations/{body['conversation_id']}")
    assert detail.status_code == 200
    assert [item["role"] for item in detail.json()["messages"]] == ["user", "assistant"]


def test_policy_that_forbids_fallback_never_reaches_an_external_provider(
    client, superuser_db, monkeypatch
):
    _local_only(monkeypatch, CLOSED_OLLAMA)
    external_calls = _forbid_external(monkeypatch)
    headers = {"X-CSRF-Token": _login(client)}

    response = client.post(
        "/api/chat",
        json={"message": "Fråga utan fallback", "client_message_id": "no-fallback"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assistant_status"] == "failed", body
    assert body["error_category"] == "unreachable", body
    assert body["retryable"] is True, body
    assert external_calls == []

    # Message ids are random UUIDs, so role order is asserted by count, not by id order.
    user_rows = superuser_db.query(MessageModel).filter_by(role=MessageRole.user).all()
    assistant_rows = superuser_db.query(MessageModel).filter_by(role=MessageRole.assistant).all()
    assert len(user_rows) == 1 and len(assistant_rows) == 1
    assert user_rows[0].content == "Fråga utan fallback"
    assert assistant_rows[0].status == MessageStatus.failed
