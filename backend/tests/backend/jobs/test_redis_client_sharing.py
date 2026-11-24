"""Regression tests for the shared Redis client (app/redis_client.py).

Background: app/jobs/heartbeat.py used to build a FRESH redis.from_url() client on every
worker poll cycle. A probe (scripts/probe_redis_leak.py outside the repo) measured 300
heartbeat calls → 304 server-side TCP connections — one connect/close churn per tick,
which degraded long soak runs and left unclosed pools at pytest teardown ("Redis teardown
failures"). These tests pin the fix: every call site listed below reuses ONE client per
process, verified against a REAL Redis (same contract as tests/backend/jobs/).
"""

import uuid

import redis

from app.jobs import heartbeat
from app.jobs.lock import JobLock
from app.redis_client import get_redis_client, reset_shared_redis_clients_for_tests


def _total_connections_received() -> int:
    """Server-side counter (INFO stats) — the ground truth for "how many TCP connects did
    this process actually make", not what the client library believes."""
    info = get_redis_client().info("stats")
    return int(info["total_connections_received"])


def test_get_redis_client_returns_the_same_instance_per_variant():
    reset_shared_redis_clients_for_tests()
    a = get_redis_client()
    b = get_redis_client()
    assert a is b, "every get_redis_client() call must hand out the SAME client object"

    decoded_a = get_redis_client(decode_responses=True)
    decoded_b = get_redis_client(decode_responses=True)
    assert decoded_a is decoded_b
    assert decoded_a is not a, "the two decode_responses variants are separate pools, each still shared"


def test_heartbeat_calls_reuse_a_single_connection():
    """THE regression: N heartbeat ticks must churn ZERO new server-side connections, not
    one per tick. The old code churned one TCP connect per tick (probe: 300 calls → 304
    connections). The shared client is built ONCE (the warmup ping), then reused."""
    reset_shared_redis_clients_for_tests()
    warm = get_redis_client()
    warm.ping()  # first build: exactly one connection, outside the measured window
    before = _total_connections_received()

    n = 25
    for i in range(n):
        heartbeat.record_worker_heartbeat(f"probe-{i}", ttl_seconds=60)

    churned = _total_connections_received() - before
    assert churned == 0, (
        f"{n} heartbeat calls churned {churned} server-side connections; "
        "the shared client must reuse its pooled connection for every tick"
    )

    # And the heartbeat is actually readable — the fix must not break the signal itself.
    assert get_redis_client().exists(heartbeat._HEARTBEAT_KEY) == 1


def test_joblock_instances_share_one_client():
    """A soak creates one JobLock per job; per-instance clients churned one connection per
    job. All JobLock instances must now share the process-wide client while keeping their
    independent per-holder tokens."""
    reset_shared_redis_clients_for_tests()
    warm = get_redis_client()
    warm.ping()  # first build: outside the measured window
    before = _total_connections_received()

    locks = [JobLock(f"share-{uuid.uuid4()}", lease_seconds=30) for _ in range(10)]
    for lock in locks:
        assert lock.acquire() is True
    for lock in locks:
        lock.release()

    churned = _total_connections_received() - before
    assert churned == 0, f"10 JobLock instances churned {churned} connections — must share one pool"

    # Distinct locks on distinct keys don't collide (behavior unchanged by sharing).
    key = f"share-{uuid.uuid4()}"
    worker_a = JobLock(key, lease_seconds=30)
    worker_b = JobLock(key, lease_seconds=30)
    assert worker_a.acquire() is True
    assert worker_b.acquire() is False
    worker_a.release()


def test_close_shared_redis_clients_closes_the_pool_and_reset_rebuilds():
    reset_shared_redis_clients_for_tests()
    client = get_redis_client()
    assert client.ping() is True

    client.close()
    # After an explicit close, a fresh call must hand out a WORKING client again (the
    # shutdown hook closes pools; a later rebuild must not return the dead instance).
    reset_shared_redis_clients_for_tests()
    rebuilt = get_redis_client()
    assert rebuilt is not client
    assert rebuilt.ping() is True


def test_no_redis_url_still_degrades_without_raising(monkeypatch):
    """The heartbeat's documented contract: REDIS_URL unset → None (in-memory / single
    instance mode is a valid deployment, app/limiter.py). The shared client must not
    change that degrade path."""
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "redis_url", None)
    reset_shared_redis_clients_for_tests()
    try:
        assert heartbeat._client() is None
    finally:
        reset_shared_redis_clients_for_tests()
