"""Bounded consultation-selection policy — how MANY MainAI consultations are allowed.

The budget store (app/openai_consult/budgets.py) bounds HOW MUCH (tokens/cost) any owner
may spend per UTC day. This module bounds HOW OFTEN the MainAI orchestration may consult
OpenAI at all (a per-owner daily call cap), so consultation stays an explicitly bounded
exception to the local-AI-primary rule rather than an unbounded default reflex.

Storage stance mirrors budgets.py exactly: with REDIS_URL the counter lives in Redis
(shared by all replicas, transactional pipeline increments); without it, process-local —
and production without Redis fails closed in build_budget_store_from_settings' sibling
check here, for the same N-replicas × cap reason.

Counting is admission-based: the counter increments when a consultation is ADMITTED (all
gates passed, before egress). Refused consultations do not consume the cap for the caller
(the refusal happens before the increment for authorization refusals); admitted calls that
later fail at the provider DO count (conservative, and they consumed real egress review).
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from app.config import Settings, get_settings
from app.openai_consult.service import ConsultationConfigError


class PolicyEnforcementUnavailableError(RuntimeError):
    """Distributed policy enforcement cannot be performed (production without Redis, or a
    Redis failure at counter time). Fail closed — a consultation admitted without proven
    policy compliance is not admissible."""


def _seconds_until_utc_midnight(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((tomorrow - now).total_seconds()) + 60, 60)


class DailyConsultCounter:
    """Per-owner, per-UTC-day consultation admission counter with a hard cap."""

    def __init__(self, *, daily_call_cap: int, redis_url: str | None):
        self.daily_call_cap = int(daily_call_cap)
        self._redis = None
        self._local: dict[str, int] = {}
        self._lock = threading.Lock()
        if redis_url:
            import redis as redis_module

            self._redis = redis_module.Redis.from_url(redis_url, decode_responses=True)

    def _guard_redis_failure(self, exc: Exception) -> None:
        import redis as redis_module

        if isinstance(exc, (redis_module.ConnectionError, redis_module.TimeoutError)):
            raise PolicyEnforcementUnavailableError(
                "Consultation-policy enforcement unavailable — consultation refused (fail-closed)"
            ) from exc

    def _key(self, user_id: str, *, now: datetime | None = None) -> str:
        day = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
        return f"openai_consult:policy:{user_id}:{day}"

    def check_and_increment(self, user_id: str, *, now: datetime | None = None) -> int:
        """Admit one consultation: increment the counter and enforce the cap. Returns the
        admission count after this increment. Raises ConsultationConfigError when the cap
        is exhausted (policy refusal — fail closed) and PolicyEnforcementUnavailableError
        when the counter itself is unavailable."""
        if self.daily_call_cap <= 0:
            raise ConsultationConfigError(
                "MainAI-konsultation vägras av policy: dagens samtalskapacitet är 0 "
                "(OPENAI_CONSULT_MAINAI_DAILY_CALL_CAP=0) — explicit policy krävs."
            )
        key = self._key(user_id, now=now)
        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                pipe.hincrby(key, "calls", 1)
                pipe.expire(key, _seconds_until_utc_midnight(now))
                calls = pipe.execute()[0]
            except PolicyEnforcementUnavailableError:
                raise
            except Exception as exc:
                self._guard_redis_failure(exc)
                raise
            calls = int(calls)
        else:
            with self._lock:
                calls = self._local.get(key, 0) + 1
                self._local[key] = calls
        if calls > self.daily_call_cap:
            raise ConsultationConfigError(
                f"MainAI-konsultation vägras av policy: dagens kap ({self.daily_call_cap} "
                "samtal/ägare/UTC-dag) är slut — lokal worker fortsätter som primär väg."
            )
        return calls

    def remaining(self, user_id: str, *, now: datetime | None = None) -> int:
        key = self._key(user_id, now=now)
        if self._redis is not None:
            try:
                raw = self._redis.hgetall(key) or {}
            except Exception as exc:
                self._guard_redis_failure(exc)
                raise
            calls = int(raw.get("calls", 0))
        else:
            with self._lock:
                calls = self._local.get(key, 0)
        return max(self.daily_call_cap - calls, 0)


def build_policy_from_settings(settings: Settings | None = None) -> DailyConsultCounter:
    """One counter PER PROCESS, cached by (cap, redis_url) — mirrors
    build_budget_store_from_settings: per-request construction would reset the in-memory
    counter on every call and silently void the cap.

    FAIL CLOSED in production without Redis — same rule as the budget store: a
    per-process counter would multiply the cap by the replica count."""
    settings = settings or get_settings()
    if settings.environment == "production" and not settings.redis_url:
        raise PolicyEnforcementUnavailableError(
            "OPENAI_CONSULT MainAI policy requires REDIS_URL in production — distributed "
            "enforcement is mandatory and its absence is fail-closed"
        )
    cache_key = (int(settings.openai_consult_mainai_daily_call_cap), settings.redis_url)
    with _COUNTER_CACHE_LOCK:
        counter = _COUNTER_CACHE.get(cache_key)
        if counter is None:
            counter = DailyConsultCounter(
                daily_call_cap=settings.openai_consult_mainai_daily_call_cap,
                redis_url=settings.redis_url,
            )
            _COUNTER_CACHE[cache_key] = counter
    return counter


# Process-lifetime counter cache — see build_policy_from_settings.
_COUNTER_CACHE: dict[tuple[int, str | None], "DailyConsultCounter"] = {}
_COUNTER_CACHE_LOCK = threading.Lock()
