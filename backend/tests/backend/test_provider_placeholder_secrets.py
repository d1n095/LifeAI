"""Security incident, 2026-07-26: a duplicated GOOGLE_API_KEY line in production's env file
left an unused, obviously-fake placeholder value ("DIN_RIKTIGA_NYCKEL", a "fill this in"
instruction, not a real credential) silently active — is_configured() saw a non-empty string
and reported the provider as configured, so it was attempted and failed confusingly instead
of being cleanly skipped like a genuinely absent key would be.

Covers:
  A. looks_like_placeholder_secret() (app/providers/base.py) — the shared detector.
  B. Every remote provider's is_configured() treats a placeholder value exactly like an
     empty one: not configured, silently skipped by fallback, never attempted.
  C. app/main.py's _warn_placeholder_provider_keys() logs a clear warning (never the value
     itself) and never raises — a placeholder AI provider key must never block startup, since
     chat already degrades cleanly with zero providers configured at all.
"""

import logging

import pytest

from app.config import get_settings
from app.providers.anthropic_provider import AnthropicProvider
from app.providers.base import looks_like_placeholder_secret
from app.providers.deepseek_provider import DeepSeekProvider
from app.providers.gemini_provider import GeminiProvider
from app.providers.openai_provider import OpenAIProvider
from app.providers.openrouter_provider import OpenRouterProvider

# --- A: the shared detector --------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "DIN_RIKTIGA_NYCKEL",
        "din_riktiga_nyckel",
        "changeme",
        "CHANGEME",
        "change-me",
        "your-api-key-here",
        "YOUR_API_KEY_HERE",
        "replace-me",
        "placeholder",
        "example",
        "todo",
        "fixme",
        "xxx",
        "<your key here>",
        "{insert-key}",
        "  changeme  ",  # surrounding whitespace shouldn't defeat the check
    ],
)
def test_looks_like_placeholder_secret_matches_known_placeholders(value):
    assert looks_like_placeholder_secret(value) is True


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "sk-proj-abc123def456ghi789",
        "AIzaSyD-a-real-looking-gemini-key-1234567890",
        "a very long and specific api key that is not a placeholder",
    ],
)
def test_looks_like_placeholder_secret_does_not_flag_real_or_empty_values(value):
    assert looks_like_placeholder_secret(value) is False


# --- B: every remote provider's is_configured() treats a placeholder as absent -------------


@pytest.mark.parametrize(
    "provider_cls, field_name",
    [
        (OpenAIProvider, "openai_api_key"),
        (AnthropicProvider, "anthropic_api_key"),
        (GeminiProvider, "google_api_key"),
        (DeepSeekProvider, "deepseek_api_key"),
        (OpenRouterProvider, "openrouter_api_key"),
    ],
)
def test_provider_is_configured_false_for_placeholder_value(provider_cls, field_name, monkeypatch):
    monkeypatch.setattr(get_settings(), field_name, "DIN_RIKTIGA_NYCKEL")
    assert provider_cls().is_configured() is False


@pytest.mark.parametrize(
    "provider_cls, field_name",
    [
        (OpenAIProvider, "openai_api_key"),
        (AnthropicProvider, "anthropic_api_key"),
        (GeminiProvider, "google_api_key"),
        (DeepSeekProvider, "deepseek_api_key"),
        (OpenRouterProvider, "openrouter_api_key"),
    ],
)
def test_provider_is_configured_true_for_a_real_looking_value(provider_cls, field_name, monkeypatch):
    monkeypatch.setattr(get_settings(), field_name, "a-real-looking-secret-value-1234567890")
    assert provider_cls().is_configured() is True


@pytest.mark.parametrize(
    "provider_cls, field_name",
    [
        (OpenAIProvider, "openai_api_key"),
        (AnthropicProvider, "anthropic_api_key"),
        (GeminiProvider, "google_api_key"),
        (DeepSeekProvider, "deepseek_api_key"),
        (OpenRouterProvider, "openrouter_api_key"),
    ],
)
def test_provider_is_configured_false_for_empty_value_unchanged(provider_cls, field_name, monkeypatch):
    monkeypatch.setattr(get_settings(), field_name, None)
    assert provider_cls().is_configured() is False


# --- C: the startup warning ----------------------------------------------------------------


def test_startup_warns_on_placeholder_key_without_raising(monkeypatch, caplog):
    from app.main import _warn_placeholder_provider_keys

    monkeypatch.setattr(get_settings(), "google_api_key", "DIN_RIKTIGA_NYCKEL")
    with caplog.at_level(logging.WARNING):
        _warn_placeholder_provider_keys()  # must never raise
    assert any("GOOGLE_API_KEY" in record.message for record in caplog.records)
    assert not any("DIN_RIKTIGA_NYCKEL" in record.message for record in caplog.records)


def test_startup_silent_when_no_placeholder_keys_present(monkeypatch, caplog):
    from app.main import _warn_placeholder_provider_keys

    monkeypatch.setattr(get_settings(), "openai_api_key", "a-real-looking-secret-value")
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)
    monkeypatch.setattr(get_settings(), "google_api_key", None)
    monkeypatch.setattr(get_settings(), "deepseek_api_key", None)
    monkeypatch.setattr(get_settings(), "openrouter_api_key", None)
    with caplog.at_level(logging.WARNING):
        _warn_placeholder_provider_keys()
    assert caplog.records == []
