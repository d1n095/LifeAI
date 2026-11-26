"""End-to-end continuity: project, conversation, reload, new session, another founder denied.

Uses the real HTTP API and the real Postgres test database. Only the model transport is
faked, because no external provider is available in this environment; retrieval is isolated
to an empty result so the assertions are about persistence and project context, not RAG.
"""

import uuid

from app.config import get_settings
from app.models.conversation import Message as MessageModel, MessageRole
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client, email: str = FOUNDER_EMAIL, password: str = FOUNDER_PASSWORD) -> str:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


async def _empty_retrieve(*args, **kwargs):
    return []


def _capture_chat(captured: list[list]):
    async def _chat(self, messages, model, **kwargs):
        captured.append(list(messages))
        return ChatResult(
            content="Vi fortsätter från projektets sparade läge.",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 4},
        )

    return _chat


def test_project_conversation_survives_reload_and_a_new_session_while_another_founder_is_denied(
    client, superuser_db, make_verified_user, monkeypatch
):
    captured: list[list] = []
    monkeypatch.setattr(OpenAIProvider, "chat", _capture_chat(captured))
    monkeypatch.setattr("app.routers.chat.retrieve_context", _empty_retrieve)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}

    project = client.post(
        "/api/projects",
        json={"name": "MainAI kontinuitet", "description": "Ett verkligt projekt", "status": "active"},
        headers=headers,
    )
    assert project.status_code == 200, project.text
    project_id = project.json()["id"]

    task = client.post(
        "/api/projects/tasks",
        json={
            "title": "Verifiera återupptagning",
            "description": "Samma uppgift får inte skapas två gånger.",
            "project_id": project_id,
            "status": "todo",
        },
        headers=headers,
    )
    assert task.status_code == 201, task.text
    task_id = task.json()["id"]

    # The same logical task sent again must reuse the existing row, not duplicate it.
    duplicate = client.post(
        "/api/projects/tasks",
        json={
            "title": "  Verifiera   återupptagning ",
            "description": "Samma uppgift får inte skapas två gånger.",
            "project_id": project_id,
            "status": "todo",
        },
        headers=headers,
    )
    assert duplicate.status_code == 201, duplicate.text
    assert duplicate.json()["id"] == task_id

    first = client.post(
        "/api/chat",
        json={
            "message": "Var är vi i projektet?",
            "project_id": project_id,
            "client_message_id": "resume-project-1",
        },
        headers=headers,
    )
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["assistant_status"] == "succeeded"
    conversation_id = first_body["conversation_id"]

    # The model actually received the project context: name and the open task.
    system_prompt = captured[0][0].content
    assert "MainAI kontinuitet" in system_prompt
    assert "Verifiera återupptagning" in system_prompt
    assert "[todo]" in system_prompt

    # A genuinely fresh read, as after a page reload.
    reloaded = client.get(f"/api/conversations/{conversation_id}")
    assert reloaded.status_code == 200, reloaded.text
    messages = reloaded.json()["messages"]
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "Var är vi i projektet?"

    # A new session continues the same conversation: history reaches the model and no
    # second copy of the first user message is created.
    second = client.post(
        "/api/chat",
        json={
            "message": "Fortsätt där vi slutade.",
            "conversation_id": conversation_id,
            "client_message_id": "resume-project-2",
        },
        headers=headers,
    )
    assert second.status_code == 200, second.text
    history_contents = [message.content for message in captured[1]]
    assert "Var är vi i projektet?" in history_contents
    assert "Vi fortsätter från projektets sparade läge." in history_contents

    stored = (
        superuser_db.query(MessageModel)
        .filter(MessageModel.conversation_id == uuid.UUID(conversation_id))
        .order_by(MessageModel.created_at.asc(), MessageModel.id.asc())
        .all()
    )
    user_rows = [row for row in stored if row.role == MessageRole.user]
    assert [row.content for row in user_rows] == [
        "Var är vi i projektet?",
        "Fortsätt där vi slutade.",
    ]

    # A second account must not reach the conversation or the project. This deployment is
    # founder-only, so `require_founder` denies the request before any owner check runs; a
    # 403 is the real boundary here, and the per-row owner checks remain covered by the
    # conversation/task isolation tests.
    other, other_password = make_verified_user(
        email="other-founder-resume@example.com", role="founder"
    )
    other_csrf = _login(client, other.email, other_password)
    other_headers = {"X-CSRF-Token": other_csrf}

    assert client.get(f"/api/conversations/{conversation_id}").status_code == 403
    assert client.get(f"/api/projects/{project_id}").status_code == 403
    denied_chat = client.post(
        "/api/chat",
        json={"message": "Fortsätt", "conversation_id": conversation_id},
        headers=other_headers,
    )
    assert denied_chat.status_code == 403
    denied_workspace = client.post(
        "/api/chat",
        json={"message": "Fortsätt", "project_id": project_id},
        headers=other_headers,
    )
    assert denied_workspace.status_code == 403
