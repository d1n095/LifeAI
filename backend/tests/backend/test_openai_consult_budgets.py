"""Multi-process budget enforcement — adversarial and concurrency tests.

fakeredis provides REAL Redis semantics (MULTI/EXEC transactional pipelines included) so
the distributed path is exercised in-sandbox without a Redis server. The concurrency test
simulates separate worker processes: independent DailyBudgetStore instances sharing one
fakeredis server, racing on the same key from real threads.
"""

import threading
from datetime import datetime, timezone

import httpx
import pytest
import redis as redis_module
from fakeredis import FakeServer, FakeStrictRedis

import app.openai_consult.service as consult_service_module
from app.config import Settings
from app.openai_consult.budgets import (
    BudgetEnforcementUnavailableError,
    BudgetExceededError,
    DailyBudgetStore,
    build_budget_store_from_settings,
)
from app.openai_consult.schemas import ConsultIn
from app.openai_consult.service import ConsultationServiceError, consult_openai
from app.workforce.activation_gates import (
    REQUIRED_ACTIVATION_GATES,
    mark_safety_gate,
    reset_safety_gates_for_tests,
)
from app.workforce.kill_switch import (
    KillSwitchError,
    activate_kill_switch,
    reset_kill_switch_for_tests,
)

OWNER_A = "owner-a-with-letters"
OWNER_B = "owner-b-with-letters"
TEST_KEY = "sk-live-real-key-shaped-for-tests-0000000000"

UTC_MIDNIGHT_DAY = datetime(2026, 11, 17, 23, 59, 0, tzinfo=timezone.utc)
NEXT_DAY = datetime(2026, 11, 18, 0, 1, 0, tzinfo=timezone.utc)


def make_settings(**over) -> Settings:
    base = dict(
        environment="development",
        openai_consult_enabled=True,
        openai_api_key=TEST_KEY,
        openai_consult_allowed_models="gpt-4o-mini",
        openai_consult_max_output_tokens=1024,
    )
    base.update(over)
    return Settings(**base)


def shared_redis_stores(monkeypatch, **store_kwargs) -> list[DailyBudgetStore]:
    """Patch redis.Redis.from_url so every store builds against ONE shared fakeredis
    server — the standard simulation of N worker processes against one Redis."""
    server = FakeServer()
    built: list[DailyBudgetStore] = []

    def fake_from_url(url, **kw):
        kw["server"] = server
        return FakeStrictRedis(**kw)

    monkeypatch.setattr(redis_module.Redis, "from_url", classmethod(lambda cls, url, **kw: fake_from_url(url, **kw)))

    def build():
        s = DailyBudgetStore(token_budget=store_kwargs.get("token_budget", 100_000),
                             cost_budget_usd=store_kwargs.get("cost_budget_usd", "5.00"),
                             redis_url="redis://shared:6379/0")
        built.append(s)
        return s

    return build  # type: ignore[return-value]


class RecordingDB:
    def add(self, obj):
        pass

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



def responses_api_body(text="ok") -> dict:
    return {
        "id": "resp_budget_1",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


def patch_transport(monkeypatch, handler) -> list[int]:
    calls: list[int] = []

    def wrapping_handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return handler(request)

    def factory(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(wrapping_handler), timeout=timeout)

    monkeypatch.setattr(consult_service_module, "_create_client", factory)
    return calls


@pytest.fixture(autouse=True)
def _clean_safety_state():
    reset_safety_gates_for_tests()
    reset_kill_switch_for_tests()
    yield
    reset_safety_gates_for_tests()
    reset_kill_switch_for_tests()


# ---------------------------------------------------------------------------
# P2.2 + P2.5 — production fail-closed when distributed enforcement is unavailable
# ---------------------------------------------------------------------------

def test_production_without_redis_fails_closed():
    settings = make_settings(environment="production", redis_url=None)
    with pytest.raises(BudgetEnforcementUnavailableError):
        build_budget_store_from_settings(settings)


def test_production_with_redis_builds_store(monkeypatch):
    settings = make_settings(environment="production", redis_url="redis://shared:6379/0")
    shared_redis_stores(monkeypatch)  # patch from_url for fakeredis
    store = build_budget_store_from_settings(settings)
    assert store._redis is not None


def test_development_without_redis_keeps_in_memory_fallback():
    store = build_budget_store_from_settings(make_settings(redis_url=None))
    assert store._redis is None
    store.reserve(OWNER_A, estimated_tokens=1, estimated_cost_usd="0.000001")
    tokens_left, _ = store.remaining(OWNER_A)
    assert tokens_left < store.token_budget


# ---------------------------------------------------------------------------
# P2.1 + P2.10 — cross-worker enforcement and concurrency
# ---------------------------------------------------------------------------

def test_budget_enforced_across_store_instances(monkeypatch):
    """Two 'worker processes' (separate store instances, shared Redis): a reservation made
    through one is immediately visible to the other — no per-process counters."""
    build = shared_redis_stores(monkeypatch, token_budget=100)
    worker1, worker2 = build(), build()
    worker1.reserve(OWNER_A, estimated_tokens=60, estimated_cost_usd="0.01")
    tokens_left, _ = worker2.remaining(OWNER_A)
    assert tokens_left == 40  # worker2 SEES worker1's spend


def test_concurrent_reserves_never_exceed_budget(monkeypatch):
    """16 threads race reserves against a 10-unit ceiling through independent store
    instances sharing one Redis. Atomic MULTI/EXEC increments mean EXACTLY 10 succeed."""
    build = shared_redis_stores(monkeypatch, token_budget=10)
    stores = [build() for _ in range(16)]
    admitted, denied = [], []
    lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            stores[i].reserve(OWNER_A, estimated_tokens=1, estimated_cost_usd="0.000001")
            with lock:
                admitted.append(i)
        except BudgetExceededError:
            with lock:
                denied.append(i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(admitted) == 10
    assert len(denied) == 6
    tokens_left, _ = stores[0].remaining(OWNER_A)
    assert tokens_left == 0


def test_budget_isolation_across_owners(monkeypatch):
    """P2.8 — owner A exhausting their ceiling never touches owner B's budget."""
    from decimal import Decimal

    build = shared_redis_stores(monkeypatch, token_budget=10)
    store = build()
    store.reserve(OWNER_A, estimated_tokens=10, estimated_cost_usd="5.00")
    tokens_left_a, cost_left_a = store.remaining(OWNER_A)
    tokens_left_b, cost_left_b = store.remaining(OWNER_B)
    assert tokens_left_a == 0 and cost_left_a == 0
    assert tokens_left_b == 10 and cost_left_b == Decimal("5.00")


# ---------------------------------------------------------------------------
# P2.3 + P2.6 — atomic reserve/settle, rollover, expiry
# ---------------------------------------------------------------------------

def test_settle_reconciles_to_real_usage_not_reserved(monkeypatch):
    from decimal import Decimal

    build = shared_redis_stores(monkeypatch, token_budget=100_000)
    store = build()
    store.reserve(OWNER_A, estimated_tokens=1000, estimated_cost_usd="0.50")
    store.settle(OWNER_A, reserved_tokens=1000, actual_tokens=42,
                 reserved_cost_usd="0.50", actual_cost_usd="0.01")
    tokens_used, cost_left = store.remaining(OWNER_A)
    assert tokens_used == 100_000 - 42  # settled to actual, no duplicate charge
    assert cost_left == Decimal("4.99")  # $5.00 ceiling minus $0.01 actual (net of reserve+settle)


def test_utc_day_rollover_gives_fresh_budget(monkeypatch):
    build = shared_redis_stores(monkeypatch, token_budget=10)
    store = build()
    store.reserve(OWNER_A, estimated_tokens=10, estimated_cost_usd="1.00", now=UTC_MIDNIGHT_DAY)
    assert store.remaining(OWNER_A, now=UTC_MIDNIGHT_DAY)[0] == 0
    tokens_left_next_day, _ = store.remaining(OWNER_A, now=NEXT_DAY)
    assert tokens_left_next_day == 10  # new UTC day, fresh ceiling


def test_key_ttl_expires_just_past_utc_midnight(monkeypatch):
    from app.openai_consult.budgets import _seconds_until_utc_midnight

    build = shared_redis_stores(monkeypatch)
    store = build()
    store.reserve(OWNER_A, estimated_tokens=1, estimated_cost_usd="0.000001", now=UTC_MIDNIGHT_DAY)
    ttl = store._redis.ttl(f"openai_consult:budget:{OWNER_A}:20261117")
    assert ttl == _seconds_until_utc_midnight(UTC_MIDNIGHT_DAY)  # 60s to midnight + 60s buffer
    assert ttl > 0


# ---------------------------------------------------------------------------
# P2.4 — Redis disconnections and partial failures fail closed, cleanly
# ---------------------------------------------------------------------------

class BrokenRedis:
    """Simulates a Redis outage: every touch raises the real driver ConnectionError."""

    def pipeline(self):
        raise redis_module.ConnectionError("simulated redis outage")

    def hgetall(self, key):
        raise redis_module.ConnectionError("simulated redis outage")


def test_redis_outage_at_reserve_fails_closed_with_safe_error():
    store = DailyBudgetStore(token_budget=100, cost_budget_usd="5.00", redis_url=None)
    store._redis = BrokenRedis()  # force the redis path with a broken client
    with pytest.raises(BudgetEnforcementUnavailableError, match="fail-closed"):
        store.reserve(OWNER_A, estimated_tokens=1, estimated_cost_usd="0.000001")


def test_redis_outage_at_remaining_fails_closed_with_safe_error():
    store = DailyBudgetStore(token_budget=100, cost_budget_usd="5.00", redis_url=None)
    store._redis = BrokenRedis()
    with pytest.raises(BudgetEnforcementUnavailableError):
        store.remaining(OWNER_A)


# ---------------------------------------------------------------------------
# P2.7 — no duplicate billing: failed calls release, retries re-reserve exactly once
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_provider_call_releases_reservation_http_error(monkeypatch):
    """Provider 500: zero tokens consumed → the worst-case reservation is RELEASED, not
    leaked. Remaining is fully restored; an immediate retry can reserve again."""
    store = DailyBudgetStore(token_budget=100_000, cost_budget_usd="5.00", redis_url=None)
    calls = patch_transport(monkeypatch, lambda request: httpx.Response(500, json={"error": "boom"}))
    with pytest.raises(ConsultationServiceError):
        await consult_openai(
            RecordingDB(), owner_id=OWNER_A, payload_in=ConsultIn(prompt="classify this"),
            settings=make_settings(), budget_store=store,
        )
    assert len(calls) == 1
    tokens_left, cost_left = store.remaining(OWNER_A)
    assert tokens_left == 100_000 and cost_left == 5.0


@pytest.mark.asyncio
async def test_failed_provider_call_releases_reservation_timeout(monkeypatch):
    store = DailyBudgetStore(token_budget=100_000, cost_budget_usd="5.00", redis_url=None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    patch_transport(monkeypatch, handler)
    with pytest.raises(ConsultationServiceError, match="timeout"):
        await consult_openai(
            RecordingDB(), owner_id=OWNER_A, payload_in=ConsultIn(prompt="x"),
            settings=make_settings(), budget_store=store,
        )
    tokens_left, _ = store.remaining(OWNER_A)
    assert tokens_left == 100_000


@pytest.mark.asyncio
async def test_cancelled_call_releases_reservation(monkeypatch):
    """P4/P8 cancellation race: a call cancelled mid-flight (client disconnect, harness
    shutdown) must release its worst-case reservation — CancelledError inherits from
    BaseException and would otherwise bypass every except-Exception handler."""
    import asyncio

    store = DailyBudgetStore(token_budget=100_000, cost_budget_usd="5.00", redis_url=None)

    class FakeSlowClient:
        def __init__(self, timeout: float):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            await asyncio.sleep(10)  # cancelled here
            raise AssertionError("must not complete")

    monkeypatch.setattr(consult_service_module, "_create_client", lambda timeout: FakeSlowClient(timeout))

    task = asyncio.create_task(
        consult_openai(
            RecordingDB(), owner_id=OWNER_A, payload_in=ConsultIn(prompt="x"),
            settings=make_settings(), budget_store=store,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    tokens_left, _ = store.remaining(OWNER_A)
    assert tokens_left == 100_000  # released, not leaked


@pytest.mark.asyncio
async def test_malformed_provider_body_fails_closed_and_releases(monkeypatch):
    """A 200 with a non-JSON body is a malformed provider response: fail closed with a
    safe error, release the reservation, never leak raw body content to the caller."""
    store = DailyBudgetStore(token_budget=100_000, cost_budget_usd="5.00", redis_url=None)
    calls = patch_transport(
        monkeypatch, lambda request: httpx.Response(200, content=b"<<<not-json{{{>>>")
    )
    with pytest.raises(ConsultationServiceError, match="unreachable"):
        await consult_openai(
            RecordingDB(), owner_id=OWNER_A, payload_in=ConsultIn(prompt="x"),
            settings=make_settings(), budget_store=store,
        )
    assert len(calls) == 1
    tokens_left, _ = store.remaining(OWNER_A)
    assert tokens_left == 100_000


@pytest.mark.asyncio
async def test_release_then_retry_charges_exactly_once(monkeypatch):
    """Fail → release → successful retry: the day ends with EXACTLY the real usage, not
    reservation + usage."""
    store = DailyBudgetStore(token_budget=100_000, cost_budget_usd="5.00", redis_url=None)
    state = {"fail_first": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail_first"]:
            state["fail_first"] = False
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(200, json=responses_api_body())

    patch_transport(monkeypatch, handler)
    with pytest.raises(ConsultationServiceError):
        await consult_openai(
            RecordingDB(), owner_id=OWNER_A, payload_in=ConsultIn(prompt="x"),
            settings=make_settings(), budget_store=store,
        )
    result, stats = await consult_openai(
        RecordingDB(), owner_id=OWNER_A, payload_in=ConsultIn(prompt="x"),
        settings=make_settings(), budget_store=store,
    )
    tokens_left, _ = store.remaining(OWNER_A)
    assert tokens_left == 100_000 - 30  # exactly one settled call (10 in + 20 out)


# ---------------------------------------------------------------------------
# P2.9 — emergency shutdown immediately blocks new consultations
# ---------------------------------------------------------------------------

def _minimal_sqlite_db():
    from sqlalchemy import create_engine
    from sqlalchemy.dialects.postgresql import JSONB
    from sqlalchemy.ext.compiler import compiles
    from sqlalchemy.orm import sessionmaker

    @compiles(JSONB, "sqlite")
    def _jsonb_sqlite(type_, compiler, **kw):
        return "JSON"

    from app.db import Base
    import app.models.user  # noqa: F401
    import app.models.agent_coordination  # noqa: F401
    import app.models.workforce  # noqa: F401

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_kill_switch_blocks_mainai_consultation(monkeypatch):
    """Emergency shutdown: after activate_kill_switch, even a fully authorized MainAI
    consultation is refused BEFORE any network call."""
    db = _minimal_sqlite_db()
    for gate_key in REQUIRED_ACTIVATION_GATES:
        mark_safety_gate(gate_key)
    calls = patch_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))

    import app.openai_consult.mainai as mainai_module

    monkeypatch.setattr(mainai_module, "get_settings", lambda: make_settings(openai_consult_mainai_enabled=True))

    activate_kill_switch(db, owner_id=123, reason="founder emergency stop")
    with pytest.raises(KillSwitchError):
        import asyncio

        asyncio.run(
            mainai_module.consult_for_mainai(
                RecordingDB(), owner_id=OWNER_A, goal_text="classify this",
            )
        )
    assert calls == []  # shut down means NOTHING reaches OpenAI
