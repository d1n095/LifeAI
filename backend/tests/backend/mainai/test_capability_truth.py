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


# --- G1: fault injection around the route truth -------------------------------------------


class _StubProvider:
    """A provider whose only job is to be a chain hop; it never talks to anything."""

    is_local = False

    def __init__(self, name: str = "stub", configured: bool = True) -> None:
        self.name = name
        self._configured = configured

    def is_configured(self) -> bool:
        return self._configured


def _chain(monkeypatch, provider, model: str = "stub-model") -> None:
    monkeypatch.setattr(capability_truth, "resolve_chat_chain", lambda db: [(provider, model)])


def _route(db_session, monkeypatch, *, probes=None, client=None):
    from app.providers import circuit_breaker

    _chain(monkeypatch, _StubProvider())
    if client is not None:
        monkeypatch.setattr(circuit_breaker, "_client", lambda: client)
    return capability_truth.chat_route_truth(db_session, probes=probes)


def test_unreachable_breaker_store_is_unknown_not_closed(db_session, monkeypatch):
    from app.providers import circuit_breaker

    _chain(monkeypatch, _StubProvider())
    monkeypatch.setattr(circuit_breaker, "_client", lambda: None)
    route = capability_truth.chat_route_truth(db_session)
    entry = route.entries[0]

    assert route.status == "ok"
    assert entry.circuit_state == "unknown"
    assert entry.circuit_open is None, "a fail-open breaker must not be reported as closed"
    assert entry.usable_now is None
    assert any("breaker-lagringen" in note for note in entry.notes)


def test_breaker_store_timeout_is_unknown(db_session, monkeypatch):
    import redis

    class _TimeoutClient:
        def exists(self, key):  # noqa: ARG002 - the timeout is the point
            raise redis.RedisError("Timeout reading from the socket")

    entry = _route(db_session, monkeypatch, client=_TimeoutClient()).entries[0]

    assert entry.circuit_state == "unknown"
    assert entry.circuit_open is None
    assert entry.usable_now is None


def test_an_open_circuit_is_a_known_blocker(db_session, monkeypatch):
    class _OpenClient:
        def exists(self, key):  # noqa: ARG002
            return 1

    entry = _route(db_session, monkeypatch, client=_OpenClient()).entries[0]

    assert entry.circuit_state == "open"
    assert entry.circuit_open is True
    assert entry.usable_now is False


def test_a_closed_circuit_and_a_key_do_not_mean_the_provider_answers(db_session, monkeypatch):
    class _ClosedClient:
        def exists(self, key):  # noqa: ARG002
            return 0

    without_probe = _route(db_session, monkeypatch, client=_ClosedClient()).entries[0]
    assert without_probe.circuit_state == "closed"
    assert without_probe.circuit_open is False
    assert without_probe.usable_now is None, "closed + configured is not evidence of health"

    with_probe = _route(
        db_session, monkeypatch, client=_ClosedClient(), probes={"stub": True}
    ).entries[0]
    assert with_probe.usable_now is True


def test_a_raising_health_read_is_unknown_and_leaks_no_dsn(db_session, monkeypatch):
    import json

    from sqlalchemy.exc import OperationalError

    def _boom(*args, **kwargs):
        raise OperationalError(
            "SELECT 1",
            {},
            Exception("could not connect: postgresql://mainai_app:super-secret-pw@127.0.0.1/db"),
        )

    monkeypatch.setattr("app.providers.verification.latest_check", _boom)
    route = _route(db_session, monkeypatch)
    entry = route.entries[0]

    assert route.status == "ok"
    assert entry.verification is None
    assert entry.usable_now is None
    assert any("OperationalError" in note for note in entry.notes)
    assert "super-secret-pw" not in json.dumps(route.to_dict())


def test_a_missing_health_check_is_unknown_not_stale(db_session, monkeypatch):
    entry = _route(db_session, monkeypatch).entries[0]

    assert entry.verification is None
    assert entry.verification_age_seconds is None
    assert entry.verification_stale is None
    assert entry.usable_now is None


def _insert_check(db_session, *, checked_at, result=None):
    import uuid

    from app.models.provider_verification import ProviderVerificationCheck, VerificationResult

    row = ProviderVerificationCheck(
        id=uuid.uuid4(),
        provider_name="stub",
        role="chat",
        model="stub-model",
        result=result or VerificationResult.ok,
        message="Verifierad.",
        checked_by="test",
        checked_at=checked_at,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_a_stale_health_check_is_not_current_evidence(db_session, monkeypatch):
    from datetime import datetime, timedelta

    from app.config import get_settings

    stale_after = get_settings().provider_verification_cache_seconds
    _insert_check(db_session, checked_at=datetime.utcnow() - timedelta(seconds=stale_after + 60))

    entry = _route(db_session, monkeypatch).entries[0]

    assert entry.verification == "ok"
    assert entry.verification_stale is True
    assert entry.verification_age_seconds is not None
    assert entry.verification_age_seconds > stale_after
    assert entry.usable_now is None
    assert any("räknas inte som aktuell" in note for note in entry.notes)


def test_a_fresh_health_check_is_reported_fresh_but_still_not_availability(
    db_session, monkeypatch
):
    from datetime import datetime

    _insert_check(db_session, checked_at=datetime.utcnow())

    entry = _route(db_session, monkeypatch).entries[0]

    assert entry.verification == "ok"
    assert entry.verification_stale is False
    assert entry.usable_now is None, "a past check is evidence of a past check, not a live answer"


def test_a_malformed_health_payload_is_unknown(db_session, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.providers.verification.latest_check",
        lambda *args, **kwargs: SimpleNamespace(result="ok", checked_at=None),
    )

    entry = _route(db_session, monkeypatch).entries[0]

    assert entry.verification is None
    assert entry.verification_stale is None
    assert any("okänd resultattyp" in note for note in entry.notes)
    assert any("saknar tidsstämpel" in note for note in entry.notes)


def test_an_unconfigured_provider_is_not_rescued_by_a_probe(db_session, monkeypatch):
    monkeypatch.setattr(capability_truth, "resolve_chat_chain", lambda db: [(_StubProvider(configured=False), "stub-model")])

    entry = capability_truth.chat_route_truth(db_session, probes={"stub": True}).entries[0]

    assert entry.configured is False
    assert entry.usable_now is False


def test_available_and_permitted_are_separate_facts():
    items = {
        item.id: item
        for item in capability_truth.capability_inventory(
            available={"chat.reply": True}, permitted={"chat.reply": False}
        )
    }

    assert items["chat.reply"].available is True
    assert items["chat.reply"].permitted is False
    assert items["chat.reply"].wired is None
