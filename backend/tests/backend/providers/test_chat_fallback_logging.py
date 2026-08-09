"""Security incident, 2026-07-26: app/providers/registry.py's chat_with_fallback() used to log
a provider failure as `logger.warning("Chat provider %s failed: %s", provider.name, exc)` —
interpolating the raw exception directly. An httpx.HTTPStatusError's default message embeds
the full request URL, and app/providers/gemini_provider.py used to put its API key in that
URL's query string, so a single Gemini failure wrote a live key straight into the Docker log.
Fixed on two independent layers (either one closes the leak; both are kept as defense in
depth): GeminiProvider now sends the key via header instead of URL (test_gemini_provider.py),
and registry.py now routes every per-attempt log line through
classify_provider_exception() — this file proves that second layer directly, with a
provider whose exception message contains a fake secret regardless of URL/header placement."""

import logging

import httpx
import pytest

from app.config import get_settings
from app.providers.base import Message, ProviderError
from app.providers.gemini_provider import GeminiProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.registry import chat_with_fallback

FAKE_SECRET = "sk-super-secret-value-that-must-never-reach-a-log-line"


@pytest.mark.asyncio
async def test_provider_failure_log_line_never_contains_a_leaked_secret(db_session, monkeypatch, caplog):
    # Only Gemini configured — the fallback chain has exactly one entry, so the failure path
    # (not the success path) is what's exercised.
    monkeypatch.setattr(get_settings(), "default_llm_provider", "gemini")
    monkeypatch.setattr(get_settings(), "google_api_key", "irrelevant-for-this-test")
    monkeypatch.setattr(get_settings(), "openai_api_key", None)
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    async def _raise_with_secret_in_message(self, messages, model, **kwargs):
        # Simulates the exact historical bug shape: an httpx exception whose default str()
        # contains a secret (previously via the request URL) — regardless of which provider
        # or which future URL/header shape ever produces this, the log line must stay safe.
        request = httpx.Request("POST", f"https://example.invalid/v1/chat?key={FAKE_SECRET}")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError(f"401 for url with key={FAKE_SECRET}", request=request, response=response)

    monkeypatch.setattr(GeminiProvider, "chat", _raise_with_secret_in_message)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(ProviderError) as exc_info:
            await chat_with_fallback(db_session, [Message(role="user", content="hej")])

    assert FAKE_SECRET not in str(exc_info.value)
    for record in caplog.records:
        assert FAKE_SECRET not in record.getMessage()


@pytest.mark.asyncio
async def test_fallback_to_a_working_provider_still_succeeds_and_stays_clean(db_session, monkeypatch, caplog):
    """The logging fix must not change chat_with_fallback's actual behavior — a failing
    primary still falls through to a working secondary, and that secondary's success is
    still reported."""
    monkeypatch.setattr(get_settings(), "default_llm_provider", "gemini")
    monkeypatch.setattr(get_settings(), "google_api_key", "irrelevant-for-this-test")
    monkeypatch.setattr(get_settings(), "openai_api_key", "also-irrelevant")
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "openai")

    async def _raise_with_secret_in_message(self, messages, model, **kwargs):
        request = httpx.Request("POST", f"https://example.invalid/v1/chat?key={FAKE_SECRET}")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError(f"401 for url with key={FAKE_SECRET}", request=request, response=response)

    async def _fake_openai_chat(self, messages, model, **kwargs):
        from app.providers.base import ChatResult

        return ChatResult(content="Fallback svar.", provider="openai", model=model, raw_usage={})

    monkeypatch.setattr(GeminiProvider, "chat", _raise_with_secret_in_message)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_openai_chat)

    with caplog.at_level(logging.WARNING):
        result, attempted = await chat_with_fallback(db_session, [Message(role="user", content="hej")])

    assert result.content == "Fallback svar."
    assert attempted == ["gemini", "openai"]
    for record in caplog.records:
        assert FAKE_SECRET not in record.getMessage()
