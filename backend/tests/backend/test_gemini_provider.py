"""Security incident, 2026-07-26: GeminiProvider used to send its API key as a `?key=...` URL
query parameter. An httpx.HTTPStatusError's default message embeds the full request URL, and
app/providers/registry.py's chat_with_fallback() used to log that raw exception on every
provider failure — so a Gemini failure leaked a live key straight into the Docker log. Fixed
by moving the key to the `x-goog-api-key` request header (Google's own documented contract for
this API), which structurally keeps it out of the URL. These tests exercise GeminiProvider
directly, at the httpx call boundary — see test_provider_verification.py for the same
guarantee proven through the classify_provider_exception()/verify_provider() layer instead."""

import logging

import httpx
import pytest

from app.config import get_settings
from app.providers.base import ProviderError
from app.providers.gemini_provider import GeminiProvider, _normalize_model, _safe_error_detail

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


# --- 2026-07-27 incident: model-string normalization -----------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("gemini-2.5-flash", "gemini-2.5-flash"),
        (" gemini-2.5-flash ", "gemini-2.5-flash"),
        ('"gemini-2.5-flash"', "gemini-2.5-flash"),
        ("'gemini-2.5-flash'", "gemini-2.5-flash"),
        (' "gemini-2.5-flash" ', "gemini-2.5-flash"),
    ],
)
def test_normalize_model_strips_whitespace_and_matching_quotes(raw, expected):
    assert _normalize_model(raw) == expected


def test_normalize_model_logs_a_warning_only_when_it_changes_something(caplog):
    with caplog.at_level(logging.WARNING):
        _normalize_model("gemini-2.5-flash")
    assert caplog.records == []

    with caplog.at_level(logging.WARNING):
        _normalize_model('"gemini-2.5-flash"')
    assert len(caplog.records) == 1
    assert "gemini-2.5-flash" in caplog.records[0].getMessage()


@pytest.mark.asyncio
async def test_chat_normalizes_a_quoted_model_before_building_the_url(monkeypatch):
    """Real incident, 2026-07-27: Docker Compose's env_file directive does not strip quote
    characters the way a shell `source` does — DEFAULT_LLM_MODEL="gemini-2.5-flash" in
    lifeai.env reaches this app's environment as the literal, quoted string. Proves the
    normalized (unquoted) model reaches the URL, not the raw configured value."""
    from app.providers.base import Message

    monkeypatch.setattr(get_settings(), "google_api_key", FAKE_KEY)
    calls = _capture_request(monkeypatch)

    await GeminiProvider().chat([Message(role="user", content="Hej")], model='"gemini-2.5-flash"')

    assert calls[0]["url"].endswith("models/gemini-2.5-flash:generateContent")


# --- 2026-07-27 incident: sanitized Google error detail, never a bare "HTTP 404" -------------


def test_safe_error_detail_extracts_googles_own_message():
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent")
    resp = httpx.Response(
        404,
        request=request,
        json={"error": {"code": 404, "message": "models/gemini-2.5-flash is not found for API version v1beta", "status": "NOT_FOUND"}},
    )
    detail = _safe_error_detail(resp)
    assert "NOT_FOUND" in detail
    assert "is not found for API version v1beta" in detail


def test_safe_error_detail_falls_back_gracefully_on_a_non_json_body():
    request = httpx.Request("POST", "https://generativelanguage.googleapis.com/v1beta/models/x:generateContent")
    resp = httpx.Response(404, request=request, content=b"<html>not json</html>")
    detail = _safe_error_detail(resp)
    assert detail  # never raises, never empty


@pytest.mark.asyncio
async def test_chat_failure_raises_provider_error_with_googles_sanitized_message_and_category(monkeypatch):
    from app.providers.base import Message

    monkeypatch.setattr(get_settings(), "google_api_key", FAKE_KEY)

    async def _fake_404(self, url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(
            404,
            request=request,
            json={"error": {"code": 404, "message": "models/gemini-2.5-flash is not found for API version v1beta", "status": "NOT_FOUND"}},
        )

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_404)

    with pytest.raises(ProviderError) as exc_info:
        await GeminiProvider().chat([Message(role="user", content="Hej")], model="gemini-2.5-flash")

    assert exc_info.value.category == "unreachable"
    assert "NOT_FOUND" in str(exc_info.value)
    assert "is not found for API version v1beta" in str(exc_info.value)
    assert "gemini-2.5-flash" in str(exc_info.value)
    assert FAKE_KEY not in str(exc_info.value)


@pytest.mark.asyncio
async def test_chat_failure_classifies_401_as_invalid_key(monkeypatch):
    from app.providers.base import Message

    monkeypatch.setattr(get_settings(), "google_api_key", FAKE_KEY)

    async def _fake_401(self, url, **kwargs):
        request = httpx.Request("POST", url)
        return httpx.Response(401, request=request, json={"error": {"code": 401, "message": "API key not valid.", "status": "UNAUTHENTICATED"}})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_401)

    with pytest.raises(ProviderError) as exc_info:
        await GeminiProvider().chat([Message(role="user", content="Hej")], model="gemini-2.5-flash")

    assert exc_info.value.category == "invalid_key"


@pytest.mark.asyncio
async def test_request_diagnostic_log_never_contains_the_key(monkeypatch, caplog):
    from app.providers.base import Message

    monkeypatch.setattr(get_settings(), "google_api_key", FAKE_KEY)
    _capture_request(monkeypatch)

    with caplog.at_level(logging.INFO):
        await GeminiProvider().chat([Message(role="user", content="Hej")], model="gemini-2.5-flash")

    assert any("generateContent" in r.getMessage() for r in caplog.records)
    for record in caplog.records:
        assert FAKE_KEY not in record.getMessage()
