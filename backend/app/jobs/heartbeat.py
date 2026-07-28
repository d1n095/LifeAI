"""2026-07-28 incident: process-level worker heartbeat, independent of any specific
ImportJob row. Before this, `ImportJob.last_heartbeat_at` (app/jobs/lease.py) was the ONLY
liveness signal `app/routers/library.py`'s `ops_status()` and
`app/rag/context_status.py`'s `_worker_reachable()` had — and it is only ever written when
the worker actually CLAIMS a job. A worker that is alive and polling correctly, but has
nothing claimable (an empty queue, or every job already `blocked`/`partial`-waiting), never
touches that column — so after `3 * worker_lease_seconds` of genuinely idle-but-healthy
polling, both call sites started reporting "worker unavailable" even though nothing was
actually wrong. This module gives the worker a second, independent signal it can write on
EVERY poll cycle regardless of whether it claimed anything, using the same single Redis
instance already used for rate limiting (app/limiter.py) and job coordination
(app/jobs/lock.py) — no new service, no schema change.

Degrades to "unknown" (never raises) if Redis is unreachable: a heartbeat check must never
itself take down a status endpoint or a chat request. Callers keep the existing
ImportJob-based check as a fallback for exactly that case.
"""

import logging
from datetime import datetime, timezone

import redis

from app.config import get_settings

logger = logging.getLogger("mainai.jobs.heartbeat")

# A single, well-known key rather than one per worker_id: this app's worker_concurrency is
# deliberately kept at one process by design (see app/worker.py's module docstring) for the
# current VPS's resource budget, and the question callers actually ask is "is a worker alive
# at all right now", not "which one" — whichever worker process is currently running keeps
# this key's TTL refreshed. If this repo ever runs multiple worker replicas, this still works
# correctly (any of them refreshing the key is sufficient), it just stops being able to say
# which specific replica is alive — a distinction nothing here currently needs.
_HEARTBEAT_KEY = "worker:heartbeat"


def _client() -> redis.Redis | None:
    settings = get_settings()
    if not settings.redis_url:
        return None
    return redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)


def record_worker_heartbeat(worker_id: str, *, ttl_seconds: int) -> None:
    """Called on EVERY poll cycle (app/worker.py's run loop) — whether or not a job was
    claimed that cycle — so an idle-but-healthy worker is never mistaken for a dead one.
    TTL means a genuinely crashed worker's heartbeat simply expires on its own; no separate
    cleanup job needed. Never raises: a heartbeat write failing must not crash the poll loop
    (which already guards its own iteration with a try/except) — this is defense in depth
    specifically scoped to Redis being unavailable."""
    client = _client()
    if client is None:
        return
    try:
        client.set(_HEARTBEAT_KEY, f"{worker_id}:{datetime.now(timezone.utc).isoformat()}", ex=ttl_seconds)
    except redis.RedisError:
        logger.warning("Kunde inte skriva worker-heartbeat för %s.", worker_id)


def worker_process_alive() -> bool | None:
    """True if a live worker process has recorded a heartbeat within its TTL — otherwise
    None, NEVER False. This signal can only ever assert "definitely alive" or "don't know" —
    the absence of a heartbeat doesn't distinguish a genuinely dead worker from one that
    simply hasn't recorded one yet (a fresh deploy before the first poll cycle, Redis having
    been unreachable/not configured, or a test environment that never runs a real worker
    process at all). Callers must treat None as "fall back to the other signal" (typically
    ImportJob.last_heartbeat_at) — that fallback is what's actually able to say "unreachable"
    with any confidence; this module only ever narrows false negatives (an idle-but-healthy
    worker wrongly reported as down), it never invents a false positive on its own."""
    client = _client()
    if client is None:
        return None
    try:
        return True if client.exists(_HEARTBEAT_KEY) == 1 else None
    except redis.RedisError:
        return None
