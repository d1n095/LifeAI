"""Acceptance for the capability/route truth surface.

The point of these tests is what the view refuses to claim: an unanswered probe is never
"available", a vendor's tool claim is never "wired", and building the chat route contacts no
provider.
"""

from __future__ import annotations

from pathlib import Path

from app.context_atlas.runtime import SystemIntelligence
from app.mainai_executive import capability_truth
from app.providers.base import ProviderError

REPO_ROOT = Path(__file__).resolve().parents[4]

_SERVICE: SystemIntelligence | None = None


def _service() -> SystemIntelligence:
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = SystemIntelligence(REPO_ROOT)
    return _SERVICE


def test_unknown_evidence_is_never_rounded_up_to_available():
    inventory = capability_truth.capability_inventory()

    assert inventory
    for item in inventory:
        assert item.wired is None
        assert item.available is None
        assert item.permitted is None
        assert item.gate, f"{item.id} must name the gate that would authorize it"
        assert item.module.startswith("app.")


def test_a_known_failure_stays_false_and_is_not_confused_with_unknown():
    inventory = {
        item.id: item
        for item in capability_truth.capability_inventory(
            available={"chat.reply": False}, permitted={"chat.reply": False}
        )
    }

    assert inventory["chat.reply"].available is False
    assert inventory["chat.reply"].permitted is False
    assert inventory["chat.stream"].available is None


def test_vendor_tool_claims_are_not_reported_as_wired_tool_calling():
    answer = capability_truth.model_tool_calling()

    assert answer["wired"] is False
    assert answer["vendors_claiming_tools"], "the catalog does claim tools; the test is not vacuous"
    assert set(answer["wired_capabilities"]) <= {"chat", "embedding"}
    assert "text" in answer["reason"]


def test_runtime_joins_wired_state_and_route_without_contacting_a_provider(
    db_session, monkeypatch
):
    async def _must_not_be_called(*args, **kwargs):
        raise AssertionError("capability truth must never contact a provider")

    from app.providers.anthropic_provider import AnthropicProvider
    from app.providers.gemini_provider import GeminiProvider
    from app.providers.ollama_provider import OllamaProvider
    from app.providers.openai_provider import OpenAIProvider

    for provider_class in (OpenAIProvider, AnthropicProvider, GeminiProvider, OllamaProvider):
        monkeypatch.setattr(provider_class, "chat", _must_not_be_called)
        monkeypatch.setattr(provider_class, "stream_chat", _must_not_be_called)

    view = _service().capabilities(db_session)

    assert view.execution_authorized is False
    by_id = {tool.id: tool for tool in view.tools}
    assert by_id["chat.reply"].wired is True
    assert by_id["system.assess"].wired is True
    assert by_id["github.status"].wired is not None

    assert view.chat_route.status in {"ok", "unavailable"}
    if view.chat_route.status == "ok":
        assert view.chat_route.entries
        assert view.chat_route.primary is view.chat_route.entries[0]
        for entry in view.chat_route.entries:
            assert entry.usable_now is None or entry.usable_now is False or entry.configured


def test_route_truth_reports_unavailable_instead_of_raising(db_session, monkeypatch):
    def _broken(*args, **kwargs):
        raise ProviderError("no chain in this environment")

    monkeypatch.setattr(capability_truth, "resolve_chat_chain", _broken)

    route = capability_truth.chat_route_truth(db_session)

    assert route.status == "unavailable"
    assert route.entries == ()
    assert route.primary is None
    assert "no chain" in (route.reason or "")
    assert any("ingen leverantör kontaktas" in line for line in route.limitations)


def test_probe_false_overrides_a_configured_provider(db_session, monkeypatch):
    from app.providers.ollama_provider import OllamaProvider

    monkeypatch.setattr(
        capability_truth, "resolve_chat_chain", lambda db: [(OllamaProvider(), "qwen2.5:0.5b")]
    )

    route = capability_truth.chat_route_truth(db_session, probes={"ollama": False})

    assert route.status == "ok"
    assert route.entries[0].usable_now is False
