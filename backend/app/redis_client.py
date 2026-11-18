"""One shared Redis client per process — fixes the per-tick connection churn that made
long-running soak tests unstable and left unclosed pools at teardown.

Incident (2026-11-18 probe, scripts/probe_redis_leak.py): app/jobs/heartbeat.py called
`redis.from_url(...)` on EVERY worker poll cycle. 300 heartbeat calls churned 304
server-side TCP connections (one SYN/ESTABLISHED/close per tick). A worker polling every
few seconds for days, and pytest sessions running thousands of ticks, accumulated socket
churn (TIME_WAIT/CLOSE_WAIT) and nondeterministic FD pressure — the mechanism behind the
reproducible soak-test instability and the Redis teardown failures pytest kept reporting.
The same pattern existed in app/jobs/lock.py (one client per JobLock instance).

This module hands out ONE client per `decode_responses` variant for the whole process.
redis-py's client is thread-safe and pools connections internally — sharing one instance
is the documented pattern; per-call construction was never needed and was never correct.

Test-integration rules (checked against the existing suites):
  * tests/backend/jobs/ runs against a REAL Redis (no monkeypatching) — safe to share.
  * The fakeredis-patched suites (tests/backend/test_openai_consult_budgets.py etc.) patch
    `redis.Redis.from_url` on the classes they construct directly (ads/billing/consult
    stores) — those call sites deliberately keep their own construction and are NOT
    converted here, so their patching contract stays intact.
  * tests that construct their own clients (test_worker_heartbeat.py, conftest.py) are
    unaffected — reading back through a second client is still valid against one server.
"""

import logging
import threading

import redis

from app.config import get_settings

logger = logging.getLogger("mainai.redis_client")

# Module-level (not lru_cache) so tests can reset explicitly and so the lock-guarded
# lazy build below stays readable.
_clients: dict[bool, redis.Redis] = {}
_lock = threading.Lock()


def get_redis_client(*, decode_responses: bool = False) -> redis.Redis:
    """The process-wide shared client for the configured REDIS_URL.

    Raises the same way the previous call sites did when Redis is not configured at all —
    callers already have explicit degrade/raise paths for that mode (JobLockUnavailable,
    heartbeat's None, limiter's in-memory fallback) and keep them.
    """
    client = _clients.get(decode_responses)
    if client is not None:
        return client
    settings = get_settings()
    if not settings.redis_url:
        raise RuntimeError("REDIS_URL is not configured — no shared Redis client to hand out.")
    with _lock:
        # Re-check inside the lock: two threads can pass the dict-miss together.
        client = _clients.get(decode_responses)
        if client is None:
            client = redis.Redis.from_url(
                settings.redis_url,
                socket_connect_timeout=3,
                socket_timeout=3,
                decode_responses=decode_responses,
            )
            _clients[decode_responses] = client
    return client


def close_shared_redis_clients() -> None:
    """Close the shared clients' pools (app/main.py's shutdown hook). Safe to call when
    nothing was ever built (Redis not configured / process never touched Redis)."""
    with _lock:
        for decode_responses, client in _clients.items():
            try:
                client.close()
            except redis.RedisError:  # pragma: no cover - pool already dead
                logger.warning("Kunde inte stänga delad Redis-klient (decode_responses=%s).", decode_responses)
        _clients.clear()


def reset_shared_redis_clients_for_tests() -> None:
    """Test-only: drop cached clients WITHOUT closing them (fakeredis-injected clients or
    per-test isolation must never be closed out from under another fixture)."""
    with _lock:
        _clients.clear()
