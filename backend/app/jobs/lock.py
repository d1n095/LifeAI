"""STEG 11: Redis-backed distributed coordination for Founder Knowledge Studio import jobs
— the interface a real multi-instance deployment would need, built and tested now, activated
today behind the SAME Redis instance already used for rate limiting (app/limiter.py). No new
service, no new production worker (per the work order's explicit constraint) — this is a
library import jobs already call synchronously, just made safe if more than one backend
replica ever calls it at once.
"""

import logging
import uuid

import redis

from app.config import get_settings

logger = logging.getLogger("mainai.jobs.lock")

_RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("DEL", KEYS[1])
else
    return 0
end
"""

_RENEW_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
    return redis.call("PEXPIRE", KEYS[1], ARGV[2])
else
    return 0
end
"""


class JobLockUnavailable(Exception):
    """Redis itself couldn't be reached — distinct from "another worker holds the lock"
    (LockHeld below, a normal and expected outcome). Callers must decide explicitly how to
    degrade (see app/rag/library_import.py's docstring on why it proceeds without a lock
    rather than blocking every import whenever Redis happens to be down) — this exception
    exists so that decision is made deliberately, never silently."""


class LockHeld(Exception):
    """Another worker currently holds this lock — a normal, expected outcome of real
    concurrency, not an error condition to log loudly."""


class JobLock:
    """A single Redis-backed lease on one coordination key, with a per-holder token so only
    the SAME acquirer can ever renew or release it — the standard single-Redis-instance
    distributed-lock pattern (this app has exactly one Redis, see app/config.py's
    redis_url; no multi-node quorum is needed or implemented).

    Never permanent: every lock is set with a lease TTL (`lease_seconds`). There is no code
    path that sets a lock without an expiry — a crashed or hung worker's lock always expires
    on its own, so a job can never be stuck behind a lock forever (see tests/backend/jobs/test_job_lock.py's
    "abandoned lock can be reacquired after its lease expires" test).
    """

    def __init__(self, key: str, lease_seconds: int = 60):
        self.key = f"joblock:{key}"
        self.token = str(uuid.uuid4())
        self.lease_seconds = lease_seconds
        self._client: redis.Redis | None = None

    def _get_client(self) -> redis.Redis:
        if self._client is None:
            settings = get_settings()
            if not settings.redis_url:
                raise JobLockUnavailable("REDIS_URL är inte konfigurerad — jobblås kan inte användas.")
            self._client = redis.from_url(settings.redis_url, socket_connect_timeout=3, socket_timeout=3)
        return self._client

    def acquire(self) -> bool:
        """True if this call acquired the lock, False if another holder already has it
        (LockHeld semantics without the exception — callers checking "is someone else
        already doing this" want a plain boolean, not a control-flow exception). Raises
        JobLockUnavailable if Redis itself can't be reached."""
        try:
            client = self._get_client()
            return bool(client.set(self.key, self.token, nx=True, ex=self.lease_seconds))
        except redis.RedisError as exc:
            raise JobLockUnavailable(str(exc)) from exc

    def renew(self) -> bool:
        """Extends the lease — the heartbeat mechanism for a long-running job. False means
        this holder's lease already expired and someone else may now hold it (or nobody
        does); the caller must treat its work as no longer exclusively guaranteed."""
        try:
            client = self._get_client()
            result = client.eval(_RENEW_SCRIPT, 1, self.key, self.token, self.lease_seconds * 1000)
            return bool(result)
        except redis.RedisError as exc:
            raise JobLockUnavailable(str(exc)) from exc

    def release(self) -> None:
        """Best-effort early release (before the lease would naturally expire). Never raises
        on a Redis error — releasing is a courtesy to let the NEXT holder proceed sooner, not
        a correctness requirement (the TTL is what actually guarantees the lock is never
        permanent), so a failure here is logged, not propagated."""
        try:
            client = self._get_client()
            client.eval(_RELEASE_SCRIPT, 1, self.key, self.token)
        except redis.RedisError:
            logger.warning("Kunde inte frigöra joblock %s explicit — TTL:en löser upp det ändå.", self.key)
