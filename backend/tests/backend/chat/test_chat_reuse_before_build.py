"""The reuse-before-build check through the real HTTP chat boundary.

The gate has its own unit tests; this file proves it is actually wired into the live chat:
the model receives the existing-implementation evidence for a build request, the response
carries the structured report with execution_authorized=False, and an ordinary question is
left untouched.
"""

from app.config import get_settings
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


def _capture_chat(captured: dict):
    async def _chat(self, messages, model, **kwargs):
        captured["system"] = messages[0].content
        return ChatResult(
            content="Här är vad som redan finns.",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 3},
        )

    return _chat


def test_build_request_puts_reuse_evidence_in_the_prompt_and_response(client, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(OpenAIProvider, "chat", _capture_chat(captured))
    monkeypatch.setattr("app.routers.chat.retrieve_context", _empty_retrieve)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    headers = {"X-CSRF-Token": _login(client)}
    response = client.post(
        "/api/chat",
        json={"message": "Kan du bygga en minnesfunktion åt mig?"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["assistant_status"] == "succeeded"
    assert "ÅTERANVÄNDNINGSKONTROLL" in captured["system"]
    assert "app." in captured["system"]

    report = body["reuse_check"]
    assert report is not None
    assert report["execution_authorized"] is False
    assert report["read_only"] is True
    assert report["outcome"] in {"reuse", "improve", "investigate"}
    assert any("memory" in module for module in report["existing_implementations"])


def test_ordinary_question_does_not_trigger_the_reuse_check(client, monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(OpenAIProvider, "chat", _capture_chat(captured))
    monkeypatch.setattr("app.routers.chat.retrieve_context", _empty_retrieve)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    headers = {"X-CSRF-Token": _login(client)}
    response = client.post(
        "/api/chat",
        json={"message": "Vad är klockan?"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["reuse_check"] is None
    assert "ÅTERANVÄNDNINGSKONTROLL" not in captured["system"]
