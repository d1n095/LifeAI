"""Bounded consultation-selection policy (P3) — per-owner daily admission cap.

The policy counter mirrors the budget store's storage stance, so the tests mirror them
too: shared fakeredis server = N worker processes against one Redis; real threads for the
race; production fail-closed; clean refusal errors; cap exhaustion refuses BEFORE any
network call.
"""

from datetime import datetime, timezone

import httpx
import pytest
import redis as redis_module
from fakeredis import FakeServer, FakeStrictRedis

import app.openai_consult.mainai as mainai_module
from app.config import Settings
from app.openai_consult.policy import (
    DailyConsultCounter,
    PolicyEnforcementUnavailableError,
    build_policy_from_settings,
)
from app.openai_consult.service import ConsultationConfigError, ConsultationServiceError
from app.workforce.activation_gates import (
    REQUIRED_ACTIVATION_GATES,
    mark_safety_gate,
    reset_safety_gates_for_tests,
)

OWNER_A = "policy-owner-a"
OWNER_B = "policy-owner-b"
TEST_KEY = "sk-live-real-key-shaped-for-tests-0000000000"

DAY1 = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 11, 18, 12, 0, 0, tzinfo=timezone.utc)


def make_settings(**over) -> Settings:
    base = dict(
        environment="development",
        openai_consult_enabled=True,
        openai_consult_mainai_enabled=True,
        openai_api_key=TEST_KEY,
        openai_consult_allowed_models="gpt-4o-mini",
        openai_consult_mainai_daily_call_cap=3,
    )
    base.update(over)
    return Settings(**base)


class RecordingDB:
    def add(self, obj):
        pass

    def flush(self):
        pass


def responses_api_body(text="Consultation answer") -> dict:
    return {
        "id": "resp_policy_1",
        "object": "response",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }


@pytest.fixture(autouse=True)
def _clean_safety_state():
    reset_safety_gates_for_tests()
    yield
    reset_safety_gates_for_tests()


def verify_all_activation_gates() -> None:
    for gate_key in REQUIRED_ACTIVATION_GATES:
        mark_safety_gate(gate_key)


# ---------------------------------------------------------------------------
# Counter unit behavior
# ---------------------------------------------------------------------------

def test_cap_admits_up_to_limit_then_refuses():
    counter = DailyConsultCounter(daily_call_cap=3, redis_url=None)
    assert counter.check_and_increment(OWNER_A, now=DAY1) == 1
    assert counter.check_and_increment(OWNER_A, now=DAY1) == 2
    assert counter.check_and_increment(OWNER_A, now=DAY1) == 3
    with pytest.raises(ConsultationConfigError, match="policy"):
        counter.check_and_increment(OWNER_A, now=DAY1)
    assert counter.remaining(OWNER_A, now=DAY1) == 0


def test_zero_cap_refuses_every_admission():
    counter = DailyConsultCounter(daily_call_cap=0, redis_url=None)
    with pytest.raises(ConsultationConfigError):
        counter.check_and_increment(OWNER_A, now=DAY1)


def test_cap_is_per_owner_isolated():
    counter = DailyConsultCounter(daily_call_cap=2, redis_url=None)
    counter.check_and_increment(OWNER_A, now=DAY1)
    counter.check_and_increment(OWNER_A, now=DAY1)
    with pytest.raises(ConsultationConfigError):
        counter.check_and_increment(OWNER_A, now=DAY1)
    assert counter.check_and_increment(OWNER_B, now=DAY1) == 1  # B unaffected


def test_cap_resets_on_utc_day_rollover():
    counter = DailyConsultCounter(daily_call_cap=1, redis_url=None)
    counter.check_and_increment(OWNER_A, now=DAY1)
    with pytest.raises(ConsultationConfigError):
        counter.check_and_increment(OWNER_A, now=DAY1)
    assert counter.check_and_increment(OWNER_A, now=DAY2) == 1  # fresh day


def test_policy_enforced_across_worker_instances(monkeypatch):
    """Two 'processes' (independent counters, shared Redis server) share ONE cap."""
    server = FakeServer()
    monkeypatch.setattr(
        redis_module.Redis, "from_url",
        classmethod(lambda cls, url, **kw: FakeStrictRedis(server=server, **kw)),
    )
    c1 = DailyConsultCounter(daily_call_cap=2, redis_url="redis://shared:6379/0")
    c2 = DailyConsultCounter(daily_call_cap=2, redis_url="redis://shared:6379/0")
    c1.check_and_increment(OWNER_A)
    c2.check_and_increment(OWNER_A)  # worker 2 sees worker 1's admission
    with pytest.raises(ConsultationConfigError):
        c2.check_and_increment(OWNER_A)
    assert c1.remaining(OWNER_A) == 0


def test_policy_redis_outage_fails_closed():
    class BrokenRedis:
        def pipeline(self):
            raise redis_module.ConnectionError("simulated outage")

        def hgetall(self, key):
            raise redis_module.ConnectionError("simulated outage")

    counter = DailyConsultCounter(daily_call_cap=5, redis_url=None)
    counter._redis = BrokenRedis()
    with pytest.raises(PolicyEnforcementUnavailableError, match="fail-closed"):
        counter.check_and_increment(OWNER_A)
    with pytest.raises(PolicyEnforcementUnavailableError):
        counter.remaining(OWNER_A)


def test_policy_production_without_redis_fails_closed():
    settings = make_settings(environment="production", redis_url=None)
    with pytest.raises(PolicyEnforcementUnavailableError):
        build_policy_from_settings(settings)


# ---------------------------------------------------------------------------
# End-to-end: the cap gates the real consultation path BEFORE any network call
# ---------------------------------------------------------------------------

def patch_transport(monkeypatch, handler) -> list[int]:
    from app.openai_consult import service as consult_service_module

    calls: list[int] = []

    def wrapping_handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return handler(request)

    def factory(timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(wrapping_handler), timeout=timeout)

    monkeypatch.setattr(consult_service_module, "_create_client", factory)
    return calls


@pytest.mark.asyncio
async def test_cap_exhaustion_refuses_before_network(monkeypatch):
    verify_all_activation_gates()
    monkeypatch.setattr(mainai_module, "get_settings", lambda: make_settings(openai_consult_mainai_daily_call_cap=2))
    calls = patch_transport(monkeypatch, lambda request: httpx.Response(200, json=responses_api_body()))

    async def consult():
        return await mainai_module.consult_for_mainai(
            RecordingDB(), owner_id=OWNER_A, goal_text="classify this note",
        )

    await consult()
    await consult()
    with pytest.raises(ConsultationConfigError, match="policy"):
        await consult()
    assert len(calls) == 2  # the refused third consultation never touched the network


@pytest.mark.asyncio
async def test_empty_answer_contract_refused(monkeypatch):
    """Contract validation: an empty model answer is a malformed response — raised as a
    ConsultationServiceError (which the workforce path treats as provider failure → local
    fallback), never returned to orchestration as a valid result."""
    verify_all_activation_gates()
    monkeypatch.setattr(mainai_module, "get_settings", lambda: make_settings())
    calls = patch_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json=responses_api_body(text="   ")),
    )

    with pytest.raises(ConsultationServiceError, match="tomt"):
        await mainai_module.consult_for_mainai(
            RecordingDB(), owner_id=OWNER_A, goal_text="classify this note",
        )
    assert len(calls) == 1  # the provider answered; the answer was rejected by contract
