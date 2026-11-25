"""Bounded provider circuit breaker.

The breaker is deliberately small and Redis-backed: it reuses the process-wide Redis client
already used by heartbeats, rate limits and job coordination. It never stores prompts,
responses, credentials or provider error bodies — only a hashed provider/model identity and
failure counters.

Fail-open on Redis problems: a broken breaker store must never take chat down. In that mode
the provider call is allowed and the existing fallback/effect-guard rules still apply.
"""

from __future__ import annotations

import hashlib
import logging

import redis

from app.config import get_settings
from app.redis_client import get_redis_client

logger = logging.getLogger(__name__)

FAILURE_THRESHOLD = 5
FAILURE_WINDOW_SECONDS = 60
OPEN_COOLDOWN_SECONDS = 120


def _client() -> redis.Redis | None:
    try:
        if not get_settings().redis_url:
            return None
        return get_redis_client()
    except Exception:  # noqa: BLE001 - breaker availability must never break chat
        return None


def _keys(provider: str, model: str) -> tuple[str, str]:
    digest = hashlib.sha256(f"{provider}:{model}".encode("utf-8")).hexdigest()[:32]
    return f"provider:circuit:{digest}:fail", f"provider:circuit:{digest}:open"


def is_open(provider: str, model: str) -> bool:
    try:
        client = _client()
    except Exception:  # noqa: BLE001 - breaker availability must never break chat
        return False
    if client is None:
        return False
    _, open_key = _keys(provider, model)
    try:
        return client.exists(open_key) == 1
    except redis.RedisError:
        return False


def state(provider: str, model: str) -> str:
    """Tri-state breaker view for reporting: ``open``, ``closed`` or ``unknown``.

    ``is_open()`` above must stay fail-open for the chat hot path, which means its ``False``
    cannot distinguish "the breaker store answered and the circuit is closed" from "Redis is
    down or timed out". A read-only truth view must not round that up to healthy, so this
    variant reports ``unknown`` whenever the store did not actually answer. It changes no
    call decision and is never used on the request path.
    """

    try:
        client = _client()
    except Exception:  # noqa: BLE001 - a reporting helper must never raise
        return "unknown"
    if client is None:
        return "unknown"
    _, open_key = _keys(provider, model)
    try:
        return "open" if client.exists(open_key) == 1 else "closed"
    except redis.RedisError:
        return "unknown"


def record_failure(provider: str, model: str) -> bool:
    """Count one failed provider attempt. Returns True when this failure opened the circuit."""

    try:
        client = _client()
    except Exception:  # noqa: BLE001 - breaker availability must never break chat
        return False
    if client is None:
        return False
    fail_key, open_key = _keys(provider, model)
    try:
        pipe = client.pipeline()
        pipe.incr(fail_key)
        pipe.expire(fail_key, FAILURE_WINDOW_SECONDS)
        count, _ = pipe.execute()
        if int(count) >= FAILURE_THRESHOLD:
            client.set(open_key, "1", ex=OPEN_COOLDOWN_SECONDS)
            client.delete(fail_key)
            logger.warning("Provider circuit opened for %s/%s after %s failures", provider, model, count)
            return True
    except redis.RedisError:
        return False
    return False


def record_success(provider: str, model: str) -> None:
    try:
        client = _client()
    except Exception:  # noqa: BLE001 - breaker availability must never break chat
        return
    if client is None:
        return
    fail_key, open_key = _keys(provider, model)
    try:
        client.delete(fail_key, open_key)
    except redis.RedisError:
        pass
