"""Security incident, 2026-07-26: GeminiProvider used to send its API key as a `?key=...` URL
query parameter. An httpx.HTTPStatusError's default message embeds the full request URL, and
app/providers/registry.py's chat_with_fallback() used to log that raw exception on every
provider failure — so a Gemini failure leaked a live key straight into the Docker log. Fixed
by moving the key to the `x-goog-api-key` request header (Google's own documented contract for
this API), which structurally keeps it out of the URL. These tests exercise GeminiProvider
directly, at the httpx call boundary — see test_provider_verification.py for the same
guarantee proven through the classify_provider_exception()/verify_provider() layer instead."""

import httpx
import pytest

from app.config import get_settings
from app.providers.gemini_provider import GeminiProvider

FAKE_KEY = "AQ.Ab8RN6-fake-gemini-auth-key-must-never-appear-in-a-url"


def _capture_request(monkeypatch):
    """Monkeypatches httpx.AsyncClient.post to record (url, headers, json) and return a
    minimal successful Gemini-shaped response, without ever making a real network call."""
    calls = []

    async def _fake_post(self, url, **kwargs):
        calls.append({"url": str(url), "headers": kwargs.get("headers", {}), "json": kwargs.get("json")})
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={
                "candidates": [{"content": {"parts": [{"text": "Hej!"}]}}],
                "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 2},
            },
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)
    return calls


@pytest.mark.asyncio
async def test_chat_sends_key_in_header_never_in_url(monkeypatch):
    from app.providers.base import Message

    monkeypatch.setattr(get_settings(), "google_api_key", FAKE_KEY)
    calls = _capture_request(monkeypatch)

    result = await GeminiProvider().chat([Message(role="user", content="Hej")], model="gemini-2.5-flash")

    assert result.content == "Hej!"
    assert len(calls) == 1
    assert FAKE_KEY not in calls[0]["url"]
    assert "key=" not in calls[0]["url"]
    assert calls[0]["headers"].get("x-goog-api-key") == FAKE_KEY


@pytest.mark.asyncio
async def test_embed_sends_key_in_header_never_in_url(monkeypatch):
    async def _fake_post(self, url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={"embedding": {"values": [0.1, 0.2, 0.3]}})

    monkeypatch.setattr(get_settings(), "google_api_key", FAKE_KEY)
    captured = []

    async def _capturing_post(self, url, **kwargs):
        captured.append({"url": str(url), "headers": kwargs.get("headers", {})})
        return await _fake_post(self, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", _capturing_post)

    vectors = await GeminiProvider().embed(["hej"], model="text-embedding-004")

    assert vectors == [[0.1, 0.2, 0.3]]
    assert len(captured) == 1
    assert FAKE_KEY not in captured[0]["url"]
    assert captured[0]["headers"].get("x-goog-api-key") == FAKE_KEY


@pytest.mark.asyncio
async def test_chat_never_leaks_key_in_response_object(monkeypatch):
    """The ChatResult returned to the rest of the app must never carry the raw key anywhere
    (content, provider, model, raw_usage) — a broader net than just checking the URL."""
    from app.providers.base import Message

    monkeypatch.setattr(get_settings(), "google_api_key", FAKE_KEY)
    _capture_request(monkeypatch)

    result = await GeminiProvider().chat([Message(role="user", content="Hej")], model="gemini-2.5-flash")

    assert FAKE_KEY not in repr(result)
