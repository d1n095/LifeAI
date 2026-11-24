"""OpenAI consultation integration — unit + security-regression tests.

All network interaction is mocked with httpx.MockTransport (no outbound traffic, no real
key). The gates exercised here are the ones docs/OPENAI_CONSULT_INTEGRATION.md documents:
config → model/allowlist/pricing → egress → budget reserve → HTTP (safe error mapping) →
settle. The live roundtrip test at the bottom runs ONLY when an operator explicitly sets
OPENAI_CONSULT_LIVE_TEST=1 with a real key, a reachable database and outbound network.
"""

import math
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.openai_consult import budgets as budgets_module
from app.openai_consult.budgets import BudgetExceededError, DailyBudgetStore, from_micros, to_micros
from app.openai_consult.schemas import ConsultIn
from app.openai_consult import service as consult_service
from app.openai_consult.service import (
    ConsultationConfigError,
    ConsultationServiceError,
    build_responses_payload,
    consult_openai,
    resolve_gate,
    resolve_model,
    status_snapshot,
)
from app.providers.base import ProviderError
from app.providers.pricing import estimate_cost

OWNER = uuid.UUID("11111111-1111-1111-1111-111111111111")
TEST_KEY = "sk-live-real-key-shaped-for-tests-0000000000"
DAY1 = datetime(2026, 11, 17, 10, 0, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 11, 18, 9, 0, 0, tzinfo=timezone.utc)


def make_settings(**over) -> Settings:
    base = dict(
        environment="development",
        openai_consult_enabled=True,
        openai_api_key=TEST_KEY,
        openai_consult_allowed_models="gpt-4o-mini,gpt-4o",
        openai_consult_max_output_tokens=1024,
        openai_consult_timeout_seconds=5.0,
        openai_consult_requests_per_minute=10,
        openai_consult_daily_token_budget=100_000,
        openai_consult_daily_cost_budget_usd="5.00",
        openai_consult_web_search_enabled=False,
        openai_consult_store_responses=False,
    )
    base.update(over)
    return Settings(**base)


class RecordingDB:
    """Stands in for a SQLAlchemy Session for enforce_egress_policy: the egress gate only
    ever db.add()s a ProviderDisclosureEvent and db.flush()es it. The test asserts one
    disclosure event per gated call — the audit row is part of the contract."""

    def __init__(self):
        self.events = []

    def add(self, obj):
        self.events.append(obj)

    def flush(self):
        pass
    # Session contract beyond add/flush: the Revenue & Compute Engine's accounting
    # writer reads tenant membership (execute -> no row here) and uses SAVEPOINTs only
    # on its dedup path. This double has no real rows, so those are no-ops.
    def execute(self, *_a, **_k):
        class _R:
            rowcount = 0

            def first(self):
                return None

            def scalars(self):
                return []

        return _R()

    def begin_nested(self):
        import contextlib

        return contextlib.nullcontext()

    def rollback(self):
        pass



def responses_api_body(text="Hello!", in_tok=10, out_tok=20, resp_id="resp_test_1") -> dict:
    """Documented Responses API response shape (output[] messages with output_text content
    parts + a usage object)."""
    return {
        "id": resp_id,
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": in_tok, "output_tokens": out_tok},
    }


def install_transport(monkeypatch, handler) -> list[dict]:
    """Swap consult_service._create_client for a MockTransport-backed factory and return the
    captured calls (parsed JSON bodies + headers) so tests can assert EXACTLY what would
    have left the process."""
    calls: list[dict] = []

    def capturing_handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "json": None if request.content == b"" else __import__("json").loads(request.content),
                "has_auth_header": "authorization" in request.headers,
            }
        )
        return handler(request)

    def factory(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(capturing_handler), timeout=timeout)

    monkeypatch.setattr(consult_service, "_create_client", factory)
    return calls


def budget_store(**over) -> DailyBudgetStore:
    base = dict(token_budget=100_000, cost_budget_usd="5.00", redis_url=None)
    base.update(over)
    return DailyBudgetStore(**base)


async def run_consult(settings=None, payload=None, store=None, db=None):
    settings = settings or make_settings()
    payload = payload or ConsultIn(prompt="hi")
    store = store or budget_store()
    db = db or RecordingDB()
    result, stats = await consult_openai(
        db, owner_id=OWNER, payload_in=payload, settings=settings, budget_store=store
    )
    return result, stats, store, db


# ---------------------------------------------------------------------------
# Gate 1: config — disabled by default, every miss refuses before anything else
# ---------------------------------------------------------------------------

def test_settings_fail_closed_by_default():
    s = Settings()
    assert s.openai_consult_enabled is False
    assert s.openai_consult_allowed_models == ""
    assert s.openai_consult_web_search_enabled is False
    assert s.openai_consult_store_responses is False


def test_gate_disabled_by_default_refuses():
    with pytest.raises(ConsultationConfigError):
        resolve_gate(Settings())  # nothing configured at all


def test_gate_requires_real_key():
    s = make_settings(openai_api_key=None)
    with pytest.raises(ConsultationConfigError):
        resolve_gate(s)
    s2 = make_settings(openai_api_key="changeme")  # template leftover — counts as unconfigured
    with pytest.raises(ConsultationConfigError):
        resolve_gate(s2)


def test_gate_requires_nonempty_allowlist():
    s = make_settings(openai_consult_allowed_models="")
    with pytest.raises(ConsultationConfigError):
        resolve_gate(s)


def test_gate_passes_when_fully_configured():
    key, web = resolve_gate(make_settings())
    assert key == TEST_KEY
    assert web is False


# ---------------------------------------------------------------------------
# Gate 2: model allowlist + pricing (unpriceable = unbudgetable = refused)
# ---------------------------------------------------------------------------

def test_model_must_be_allowlisted():
    with pytest.raises(ConsultationServiceError):
        resolve_model(make_settings(), "gpt-9-not-allowlisted")


def test_model_resolution_defaults_to_first_allowlisted():
    assert resolve_model(make_settings(), None) == "gpt-4o-mini"


def test_unpriced_model_refused():
    s = make_settings(openai_consult_allowed_models="totally-unpriced-model")
    with pytest.raises(ConsultationServiceError, match="prisdata"):
        resolve_model(s, None)


def test_priced_allowlisted_models_resolve():
    assert resolve_model(make_settings(), "gpt-4o") == "gpt-4o"


# ---------------------------------------------------------------------------
# Gate 3: egress — default-deny BEFORE the network; only redacted content is sent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_never_egress_content_denied_before_any_network(monkeypatch):
    calls = install_transport(monkeypatch, lambda request: responses_api_response_ok())
    payload = ConsultIn(prompt="summarize NEVER_EGRESS: the recovery manifest")
    with pytest.raises(Exception) as excinfo:
        await run_consult(payload=payload)
    assert "egress denied" in str(excinfo.value)
    assert calls == []  # the request never left the process


def responses_api_response_ok(text="Hello!") -> httpx.Response:
    return httpx.Response(200, json=responses_api_body(text=text))


@pytest.mark.asyncio
async def test_secrets_redacted_before_send(monkeypatch):
    calls = install_transport(monkeypatch, lambda request: responses_api_response_ok())
    payload = ConsultIn(prompt="here api_key=hunter2secret99 use it")
    result, _stats, _store, db = await run_consult(payload=payload)
    sent_prompt = calls[0]["json"]["input"][0]["content"]
    assert "hunter2secret99" not in sent_prompt
    assert "[REDACTED]" in sent_prompt
    assert result.output_text == "Hello!"
    # exactly one disclosure-ledger row per gated call
    # The RecordingDB now also collects the platform-ledger UsageLog rows (Revenue &
    # Compute Engine); the disclosure contract counts ProviderDisclosureEvents only.
    disclosure_events = [e for e in db.events if type(e).__name__ == "ProviderDisclosureEvent"]
    assert len(disclosure_events) == 1


# ---------------------------------------------------------------------------
# Gates 4-6: budget reserve/settle, HTTP, parsing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_success_flow_body_shape_and_settle(monkeypatch):
    calls = install_transport(monkeypatch, lambda request: responses_api_response_ok())
    store = budget_store()
    payload = ConsultIn(prompt="hi", max_output_tokens=256)
    result, stats, store, _db = await run_consult(payload=payload, store=store)

    body = calls[0]["json"]
    assert body["model"] == "gpt-4o-mini"
    assert body["max_output_tokens"] == 256
    assert body["store"] is False
    assert "tools" not in body
    assert "instructions" not in body
    assert calls[0]["has_auth_header"] is True

    assert result.model == "gpt-4o-mini"
    assert result.output_text == "Hello!"
    assert result.structured is False
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 20
    assert result.request_id == "resp_test_1"

    actual_cost = estimate_cost("openai", "gpt-4o-mini", 10, 20)
    assert result.cost_usd == str(actual_cost)
    # settle releases the reservation and charges real usage — the NET charge for the day
    # is exactly the actual usage, not the (larger) worst-case reservation
    tokens_left, cost_left = store.remaining(str(OWNER))
    assert tokens_left == 100_000 - 30
    assert cost_left == from_micros(to_micros("5.00") - to_micros(actual_cost))
    assert stats["reserved_tokens"] >= stats["settled_in"] + stats["settled_out"]


@pytest.mark.asyncio
async def test_budget_exhaustion_blocks_before_network(monkeypatch):
    calls = install_transport(monkeypatch, lambda request: responses_api_response_ok())
    store = budget_store(token_budget=10)  # the 1024-token reservation alone busts this
    with pytest.raises(BudgetExceededError):
        await run_consult(store=store)
    assert calls == []


@pytest.mark.asyncio
async def test_missing_usage_fails_and_keeps_reservation(monkeypatch):
    install_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"id": "resp_x", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Hello!"}]}
        ]}),
    )
    store = budget_store()
    with pytest.raises(ConsultationServiceError, match="usage"):
        await run_consult(store=store)
    # conservative: the worst-case reservation stays charged when usage can't be verified
    tokens_left, _cost_left = store.remaining(str(OWNER))
    est_prompt = math.ceil(len("hi") / consult_service._CHARS_PER_TOKEN)
    assert tokens_left == 100_000 - (est_prompt + 1024)


@pytest.mark.asyncio
async def test_structured_output_valid_json(monkeypatch):
    install_transport(monkeypatch, lambda request: responses_api_response_ok(text='{"answer": 42}'))
    payload = ConsultIn(prompt="hi", response_schema={"type": "object", "properties": {}})
    result, _s, _st, _db = await run_consult(payload=payload)
    assert result.structured is True
    assert result.structured_json == {"answer": 42}


@pytest.mark.asyncio
async def test_structured_output_invalid_json_fails_closed(monkeypatch):
    install_transport(monkeypatch, lambda request: responses_api_response_ok(text="not json at all"))
    payload = ConsultIn(prompt="hi", response_schema={"type": "object", "properties": {}})
    with pytest.raises(ConsultationServiceError):
        await run_consult(payload=payload)


def test_response_schema_must_be_object():
    with pytest.raises(ValueError):
        ConsultIn(prompt="hi", response_schema={"type": "array"})


# ---------------------------------------------------------------------------
# Tools: structural impossibility + the single, gated web_search exception
# ---------------------------------------------------------------------------

def test_client_tools_are_structurally_impossible():
    assert "tools" not in ConsultIn.model_fields
    assert "tool_choice" not in ConsultIn.model_fields


@pytest.mark.asyncio
async def test_web_search_not_attached_when_flag_off(monkeypatch):
    calls = install_transport(monkeypatch, lambda request: responses_api_response_ok())
    await run_consult(payload=ConsultIn(prompt="hi", allow_web_search=True))
    assert "tools" not in calls[0]["json"]


@pytest.mark.asyncio
async def test_web_search_attached_only_when_enabled(monkeypatch):
    calls = install_transport(monkeypatch, lambda request: responses_api_response_ok())
    settings = make_settings(openai_consult_web_search_enabled=True)
    await run_consult(settings=settings, payload=ConsultIn(prompt="hi", allow_web_search=True))
    assert calls[0]["json"]["tools"] == [{"type": "web_search"}]


def test_payload_builder_cannot_express_other_tools():
    body = build_responses_payload(
        model="gpt-4o-mini",
        prompt="p",
        instructions=None,
        max_output_tokens=64,
        store=False,
        web_search=False,
        response_schema=None,
        response_schema_name="response",
    )
    assert "tools" not in body
    # nothing in the builder's signature accepts a client tool list — asserted by inspecting it
    import inspect

    sig = inspect.signature(build_responses_payload)
    assert not any("tool" in name and name != "web_search" for name in sig.parameters)


# ---------------------------------------------------------------------------
# HTTP failure mapping: safe categories, never URLs, never keys, never raw bodies
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_timeout_error_is_safe(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout(f"timed out connecting to {request.url}")  # message embeds the URL

    install_transport(monkeypatch, handler)
    with pytest.raises(ConsultationServiceError) as excinfo:
        await run_consult()
    assert "api.openai.com" not in str(excinfo.value)
    assert TEST_KEY not in str(excinfo.value)


@pytest.mark.parametrize(
    ("status", "expected_fragment"),
    [(401, "autentisering"), (403, "autentisering"), (429, "rate_limited"), (500, "unreachable")],
)
@pytest.mark.asyncio
async def test_http_errors_map_to_safe_categories(monkeypatch, status, expected_fragment):
    # the provider's error body deliberately CONTAINS the key — a regression guard: the
    # surfaced message must never echo provider bodies, URLs or credentials
    install_transport(
        monkeypatch,
        lambda request, status=status: httpx.Response(
            status, json={"error": {"message": f"denied, your key was {TEST_KEY}"}}
        ),
    )
    with pytest.raises(ConsultationServiceError) as excinfo:
        await run_consult()
    message = str(excinfo.value)
    assert TEST_KEY not in message
    assert "api.openai.com" not in message
    assert expected_fragment in message


# ---------------------------------------------------------------------------
# Budgets: unit behavior of the store itself
# ---------------------------------------------------------------------------

def test_micros_roundtrip():
    assert to_micros("5.00") == 5_000_000
    assert from_micros(5_000_000) == Decimal("5.000000")
    assert to_micros(Decimal("0.0000135")) == 14  # ceiling, never under-counts


def test_budget_reserve_and_settle_math():
    store = budget_store()
    store.reserve(str(OWNER), estimated_tokens=1000, estimated_cost_usd="1.00", now=DAY1)
    tokens_left, cost_left = store.remaining(str(OWNER), now=DAY1)
    assert tokens_left == 99_000
    assert cost_left == Decimal("4.000000")
    store.settle(
        str(OWNER),
        reserved_tokens=1000,
        actual_tokens=300,
        reserved_cost_usd="1.00",
        actual_cost_usd="0.25",
        now=DAY1,
    )
    tokens_left, cost_left = store.remaining(str(OWNER), now=DAY1)
    assert tokens_left == 99_700
    assert cost_left == Decimal("4.750000")


def test_budget_reserve_over_ceiling_raises():
    store = budget_store(token_budget=50)
    with pytest.raises(BudgetExceededError):
        store.reserve(str(OWNER), estimated_tokens=51, estimated_cost_usd="0.00", now=DAY1)


def test_budget_cost_ceiling_raises():
    store = budget_store(cost_budget_usd="1.00")
    with pytest.raises(BudgetExceededError):
        store.reserve(str(OWNER), estimated_tokens=1, estimated_cost_usd="1.01", now=DAY1)


def test_budget_day_rollover_restores_full_ceiling():
    store = budget_store()
    store.reserve(str(OWNER), estimated_tokens=90_000, estimated_cost_usd="4.99", now=DAY1)
    tokens_left, _c = store.remaining(str(OWNER), now=DAY1)
    assert tokens_left == 10_000
    tokens_left, cost_left = store.remaining(str(OWNER), now=DAY2)
    assert tokens_left == 100_000
    assert cost_left == Decimal("5.000000")


# ---------------------------------------------------------------------------
# Status surface: operator visibility with zero secret material
# ---------------------------------------------------------------------------

def test_status_snapshot_never_contains_key():
    s = make_settings()
    snap = status_snapshot(s, budget_store(), OWNER)
    rendered = snap.model_dump_json()
    assert TEST_KEY not in rendered
    assert "sk-" not in rendered
    assert snap.enabled is True
    assert snap.key_configured is True
    assert snap.allowed_models == ["gpt-4o-mini", "gpt-4o"]
    assert snap.tokens_used_today == 0
    assert snap.cost_used_today_usd == "0.000000"


def test_status_snapshot_shows_disabled_when_off():
    snap = status_snapshot(make_settings(openai_consult_enabled=False), budget_store(), OWNER)
    assert snap.enabled is False
    assert snap.key_configured is True  # visible as configured-but-off, still no key material


def test_status_snapshot_hides_key_configured_for_placeholder():
    snap = status_snapshot(make_settings(openai_consult_enabled=False, openai_api_key="changeme"), budget_store(), OWNER)
    assert snap.key_configured is False


# ---------------------------------------------------------------------------
# Router mapping contract (the error classes the router translates)
# ---------------------------------------------------------------------------

def test_error_types_are_distinct_for_router_mapping():
    assert not issubclass(ConsultationConfigError, ConsultationServiceError)
    assert issubclass(BudgetExceededError, RuntimeError)


# ---------------------------------------------------------------------------
# Integration test — runs ONLY when the operator explicitly asks for it
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("OPENAI_CONSULT_LIVE_TEST") != "1",
    reason="live OpenAI roundtrip requires OPENAI_CONSULT_LIVE_TEST=1, a real key, an "
    "outbound network and a reachable database (egress disclosure ledger)",
)
def test_live_responses_api_roundtrip():
    """End-to-end proof against the real API: real key from the environment, real session
    for the disclosure ledger. Skipped in CI/sandbox by design."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    database_url = os.environ.get("APP_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("no APP_DATABASE_URL configured")
    engine = create_engine(database_url)
    try:
        with engine.connect():
            pass
    except Exception:
        pytest.skip("database not reachable from this environment")
    Session = sessionmaker(bind=engine)
    db = Session()
    settings = Settings()  # the operator's real configuration, whatever it is
    payload = ConsultIn(prompt="Reply with exactly: OK")
    import asyncio

    result, _stats, _store, _db = asyncio.run(
        consult_openai(db, owner_id=uuid.uuid4(), payload_in=payload, settings=settings)
    )
    assert result.output_text
    db.rollback()  # the live test's own disclosure row is proof-by-running, not state to keep
