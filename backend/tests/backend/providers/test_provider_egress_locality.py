"""Provider egress-locality classification (app/providers/base.py + ollama_provider.py).

The egress gate (app/egress_policy/service.py) records a call as `local` instead of an
external-AI `allowed` disclosure only when the provider reports it stays on-host. These tests
pin the two invariants that make that safe:
  - FAIL-SAFE DEFAULT: every provider is external unless it explicitly proves otherwise, so a
    provider that forgets to declare locality is gated/ledgered as a disclosure, never silently
    treated as "nothing left the machine".
  - ENDPOINT-DERIVED, NOT NAME-DERIVED: Ollama is local only when its CONFIGURED base URL is
    loopback / the in-deployment host. A remote Ollama endpoint IS external egress -- the
    false-negative this classification exists to prevent.
"""

import types

import pytest

from app.providers.anthropic_provider import AnthropicProvider
from app.providers.gemini_provider import GeminiProvider
from app.providers.ollama_provider import OllamaProvider
from app.providers.openai_provider import OpenAIProvider


@pytest.mark.parametrize("provider_cls", [OpenAIProvider, AnthropicProvider, GeminiProvider])
def test_external_providers_report_external_egress(provider_cls):
    assert provider_cls().is_external_egress is True


@pytest.mark.parametrize(
    "base_url",
    [
        "http://ollama:11434",  # the in-deployment compose default
        "http://localhost:11434",
        "http://127.0.0.1:11434",
        "http://[::1]:11434",
    ],
)
def test_ollama_on_local_endpoint_is_not_external_egress(monkeypatch, base_url):
    provider = OllamaProvider()
    monkeypatch.setattr(provider, "settings", types.SimpleNamespace(ollama_base_url=base_url))
    assert provider.is_external_egress is False


@pytest.mark.parametrize(
    "base_url",
    [
        "http://ollama.example.com:11434",  # a real remote host
        "https://my-gpu-box.cloud:11434",
        "http://10.0.0.5:11434",  # a remote LAN host, not loopback
        "not-a-url",  # unparseable -> external (the safe side)
    ],
)
def test_ollama_on_remote_or_unparseable_endpoint_is_external_egress(monkeypatch, base_url):
    provider = OllamaProvider()
    monkeypatch.setattr(provider, "settings", types.SimpleNamespace(ollama_base_url=base_url))
    assert provider.is_external_egress is True
