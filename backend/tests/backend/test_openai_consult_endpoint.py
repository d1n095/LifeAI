"""HTTP-layer reliability for the consultation endpoint (P7).

A minimal FastAPI app mirrors app/main.py's slowapi wiring, with dependency overrides for
get_db (real in-memory SQLite so the disclosure ledger actually persists) and
require_founder (synthetic founder). The transport is a counting MockTransport — the
network witness for every refusal.
"""

import uuid

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

import app.openai_consult.router as router_module
from app.config import Settings
from app.db import get_db
from app.deps import require_founder
from app.limiter import limiter
from app.openai_consult import service as consult_service_module

TEST_KEY = "sk-live-real-key-shaped-for-tests-0000000000"
FOUNDER_ID = uuid.UUID("5a55b66c-4333-4333-8333-33333333bb11")


def make_settings(**over) -> Settings:
    base = dict(
        environment="development",
        openai_consult_enabled=True,
        openai_api_key=TEST_KEY,
        openai_consult_allowed_models="gpt-4o-mini",
        openai_consult_requests_per_minute=10,
    )
    base.update(over)
    return Settings(**base)


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


def responses_api_body(text="Endpoint answer") -> dict:
    return {
        "id": "resp_endpoint_1",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


def install_transport(handler) -> None:
    def factory(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)

    # router consult path builds the store per request and consult_openai patches nothing:
    # patch where the service creates its client.
    consult_service_module._create_client = factory  # monkeypatch-free: fresh app per test


@pytest.fixture
def client(monkeypatch):
    from app.db import Base
    import app.models.provider_disclosure  # noqa: F401  (ledger table)

    from sqlalchemy.pool import StaticPool

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},  # TestClient worker thread
        poolclass=StaticPool,  # one shared in-memory DB across connections
    )
    Base.metadata.create_all(engine, tables=[__import__("app.models.provider_disclosure", fromlist=["ProviderDisclosureEvent"]).ProviderDisclosureEvent.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()

    settings = make_settings()
    monkeypatch.setattr(router_module, "settings", settings)
    limiter._storage.reset()  # fresh rate-limit windows per test (MemoryStorage.reset)

    app = FastAPI()
    app.include_router(router_module.router)
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_founder] = lambda: type("F", (), {"id": FOUNDER_ID})()

    yield TestClient(app, raise_server_exceptions=False), db

    db.close()
    engine.dispose()


# ---------------------------------------------------------------------------
# P7.1/P7.2 — authentication is wired on the route
# ---------------------------------------------------------------------------

def test_route_requires_founder_dependency():
    found = any(
        getattr(dep, "dependency", None) is require_founder
        or getattr(dep.call, "__name__", "") == "require_founder"
        for dep in router_module.router.dependencies
    )
    assert found, "the consultation route must sit behind require_founder"


# ---------------------------------------------------------------------------
# P7.4/P7.5 — request validation before any gate
# ---------------------------------------------------------------------------

def test_oversized_prompt_rejected_422_before_any_gate(client):
    c, _db = client
    calls = install_transport(lambda request: pytest.fail("network must not be touched"))
    resp = c.post("/api/openai-consult", json={"prompt": "x" * 32_001})
    assert resp.status_code == 422


def test_missing_prompt_rejected_422(client):
    c, _db = client
    install_transport(lambda request: pytest.fail("network must not be touched"))
    resp = c.post("/api/openai-consult", json={"instructions": "no prompt"})
    assert resp.status_code == 422


def test_negative_max_output_tokens_rejected_422(client):
    c, _db = client
    install_transport(lambda request: pytest.fail("network must not be touched"))
    resp = c.post("/api/openai-consult", json={"prompt": "x", "max_output_tokens": 4})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# P7.6 + disabled/budget-unavailable mappings
# ---------------------------------------------------------------------------

def test_disabled_integration_answers_503_without_network(client, monkeypatch):
    c, _db = client
    monkeypatch.setattr(router_module, "settings", make_settings(openai_consult_enabled=False))
    install_transport(lambda request: pytest.fail("network must not be touched"))
    resp = c.post("/api/openai-consult", json={"prompt": "hello"})
    assert resp.status_code == 503


def test_production_without_redis_answers_503(client, monkeypatch):
    c, _db = client
    monkeypatch.setattr(
        router_module, "settings", make_settings(environment="production", redis_url=None)
    )
    install_transport(lambda request: pytest.fail("network must not be touched"))
    resp = c.post("/api/openai-consult", json={"prompt": "hello"})
    assert resp.status_code == 503
    assert "fail" in resp.json()["detail"].lower() or "Redis" in resp.json()["detail"]
    # and the status endpoint fails closed the same way
    assert c.get("/api/openai-consult/status").status_code == 503


def test_successful_consultation_returns_contract_shape(client):
    c, db = client
    install_transport(lambda request: httpx.Response(200, json=responses_api_body()))
    resp = c.post("/api/openai-consult", json={"prompt": "classify this note"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "gpt-4o-mini"
    assert body["output_text"] == "Endpoint answer"
    assert body["usage"]["total_tokens"] == 30
    assert body["cost_usd"]
    assert "sk-" not in str(body)  # P7.8 — no secret material in the response


def test_status_endpoint_never_leaves_secrets(client):
    c, _db = client
    install_transport(lambda request: pytest.fail("network must not be touched"))
    resp = c.get("/api/openai-consult/status")
    assert resp.status_code == 200
    assert TEST_KEY not in resp.text


# ---------------------------------------------------------------------------
# P7.3 — rate limiting at the endpoint (in-memory limiter, per-process stance)
# ---------------------------------------------------------------------------

def test_rate_limit_returns_429(client):
    """The endpoint limiter's limit string is baked at import from settings (10/min by
    default) — so this test uses the REAL production limit. slowapi's in-memory storage
    is per-process (the same documented stance as the dev-mode budget fallback; production
    must pair it with Redis-backed budgets, which DO fail closed)."""
    c, _db = client
    install_transport(lambda request: httpx.Response(200, json=responses_api_body()))
    codes = [c.post("/api/openai-consult", json={"prompt": "x"}).status_code for _ in range(11)]
    assert codes[:10] == [200] * 10
    assert codes[10] == 429  # the 11th call inside the same minute is rate-limited
