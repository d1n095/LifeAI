"""R15-11: exercise the PRODUCTION Redis budget path, not only the in-memory fallback.

The rest of this directory is deliberately DB-free. These tests run whenever ``REDIS_URL``
points at a disposable Redis (the CI ``r15-security-tests`` job does) and skip with an
explicit reason otherwise — they never silently downgrade to the in-memory store.

    REDIS_URL=redis://localhost:6379/15 python -m pytest \
      tests/security_r15_successor/test_r15_11_redis_store.py -q \
      --confcutdir=tests/security_r15_successor
"""

import threading
import uuid
from decimal import Decimal

import pytest

from app.billing.enforcement import BillingEnforcementUnavailableError, ComputeBudgetStore
from app.config import get_settings


@pytest.fixture()
def redis_url():
    pytest.importorskip("redis")
    url = get_settings().redis_url
    if not url:
        pytest.skip("REDIS_URL is not configured; Redis-backed budget path not exercised")
    import redis

    try:
        client = redis.from_url(url, socket_connect_timeout=2)
        client.ping()
        client.flushdb()
    except Exception as exc:  # noqa: BLE001 - skip must state the real connection error
        pytest.skip(f"Redis is not reachable at the configured REDIS_URL: {exc}")
    else:
        client.close()
    yield url


def _store(redis_url: str) -> ComputeBudgetStore:
    return ComputeBudgetStore(redis_url=redis_url)


def _remaining(store: ComputeBudgetStore, subject: str):
    return store.remaining(
        subject_type="user",
        subject_id=subject,
        token_caps=(1000, 1000),
        cost_caps_micros=(1_000_000, 1_000_000),
    )


def test_parallel_identical_reservations_charge_once_in_redis(redis_url):
    store = _store(redis_url)
    subject = f"r15-redis-{uuid.uuid4().hex}"
    barrier = threading.Barrier(8)
    results, errors = [], []

    def worker():
        try:
            barrier.wait()
            results.append(
                store.reserve(
                    subject_type="user",
                    subject_id=subject,
                    estimated_tokens=100,
                    estimated_cost_usd=Decimal("0.01"),
                    idempotency_key="same-request",
                )
            )
        except Exception as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 8
    assert len({item.reservation_id for item in results}) == 1
    assert _remaining(store, subject)["day"] == (900, 990_000)
    assert _remaining(store, subject)["month"] == (900, 990_000)


def test_settlement_is_idempotent_and_release_after_settle_is_refused(redis_url):
    store = _store(redis_url)
    subject = f"r15-redis-{uuid.uuid4().hex}"
    reservation = store.reserve(
        subject_type="user",
        subject_id=subject,
        estimated_tokens=100,
        estimated_cost_usd=Decimal("0.01"),
    )

    store.settle(reservation, actual_tokens=40, actual_cost_usd=Decimal("0.004"))
    # A duplicate settlement of the SAME reservation must be a no-op, not a double charge.
    store.settle(reservation, actual_tokens=40, actual_cost_usd=Decimal("0.004"))

    assert _remaining(store, subject)["day"] == (960, 996_000)
    with pytest.raises(BillingEnforcementUnavailableError):
        store.release(reservation)


def test_tenant_subjects_do_not_share_redis_counters(redis_url):
    store = _store(redis_url)
    tenant_a = f"r15-redis-a-{uuid.uuid4().hex}"
    tenant_b = f"r15-redis-b-{uuid.uuid4().hex}"

    store.reserve(
        subject_type="tenant",
        subject_id=tenant_a,
        estimated_tokens=400,
        estimated_cost_usd=Decimal("0.01"),
    )
    store.reserve(
        subject_type="tenant",
        subject_id=tenant_b,
        estimated_tokens=100,
        estimated_cost_usd=Decimal("0.01"),
    )

    def remaining(subject: str):
        return store.remaining(
            subject_type="tenant",
            subject_id=subject,
            token_caps=(1000, 1000),
            cost_caps_micros=(1_000_000, 1_000_000),
        )

    assert remaining(tenant_a)["day"] == (600, 990_000)
    assert remaining(tenant_b)["day"] == (900, 990_000)
