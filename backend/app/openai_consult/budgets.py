"""Per-user, per-UTC-day token/cost budgets for the OpenAI consultation surface.

Design notes (mirroring app/limiter.py's storage stance):
* With REDIS_URL set, counters live in Redis so the budget is enforced across all backend
  replicas and survives restarts/deployments. Without it, counters are process-local —
  correct for exactly one backend process (local dev, the E2E harness), the same
  documented limitation slowapi's in-memory backend already has for rate limiting.
* RESERVE before the outbound call (estimated tokens/cost), SETTLE after it with the real
  usage numbers from the Responses API `usage` object. Reserve-then-settle means a request
  can never race past the ceiling by being admitted before its own cost is known: the
  reservation charges a worst-case estimate up front, and settle reconciles the difference.
  A crashed call leaks at most one reservation until the UTC-day TTL clears the key —
  deliberately conservative (under-admission, never over-admission).
* Counters are plain integers: tokens as tokens, cost as integer micro-USD, so both Redis
  and the in-memory fallback never deal with float rounding. Cost ceilings come from
  openai_consult_daily_cost_budget_usd (Decimal-parsed string) and per-call costs from
  app/providers/pricing.py's Decimal table.
* Day rollover is structural: the key embeds the UTC date, and every write (re)sets the
  key's TTL to just past UTC midnight, so stale days cannot accumulate.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.config import get_settings

_MICROS = 1_000_000


class BudgetEnforcementUnavailableError(RuntimeError):
    """Raised when distributed budget enforcement cannot be performed. Two cases:

    * PRODUCTION without REDIS_URL — process-local counters would silently let N workers
      each spend the full daily ceiling (N× overspend). Production must FAIL CLOSED: the
      consultation surface refuses until Redis-backed enforcement is available.
    * A Redis connection failure/timeout at reserve/settle time — the counter state is
      unknown, so admission cannot be proven safe. Fail closed with a clean, safe error
      (never a raw driver traceback) instead of admitting an uncounted call.
    """


class BudgetExceededError(RuntimeError):
    """Raised BEFORE any outbound call when the estimated reservation would push the
    caller past their daily token or cost ceiling. `which` is "tokens" or "cost"."""

    def __init__(self, which: str, *, budgeted: int, requested: int):
        self.which = which
        self.budgeted = budgeted
        self.requested = requested
        super().__init__(
            f"daily {which} budget exhausted: budgeted={budgeted} requested_total={requested}"
        )


def _seconds_until_utc_midnight(now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(int((tomorrow - now).total_seconds()) + 60, 60)


def to_micros(amount: Decimal | float | int | str) -> int:
    return int((Decimal(str(amount)) * _MICROS).to_integral_value(rounding="ROUND_CEILING"))


def from_micros(micros: int) -> Decimal:
    return (Decimal(micros) / Decimal(_MICROS)).quantize(Decimal("0.000001"))


class DailyBudgetStore:
    """Reserve/settle/remaining for one user's rolling-UTC-day token and cost ceilings."""

    def __init__(self, *, token_budget: int, cost_budget_usd: Decimal | str, redis_url: str | None):
        self.token_budget = int(token_budget)
        self.cost_budget_micros = to_micros(cost_budget_usd)
        self._redis = None
        self._local: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()
        if redis_url:
            import redis as redis_module

            self._redis = redis_module.Redis.from_url(redis_url, decode_responses=True)

    # -- key handling -------------------------------------------------------------
    def _key(self, user_id: str, *, now: datetime | None = None) -> str:
        day = (now or datetime.now(timezone.utc)).strftime("%Y%m%d")
        return f"openai_consult:budget:{user_id}:{day}"

    def _ttl(self, now: datetime | None = None) -> int:
        return _seconds_until_utc_midnight(now)

    # -- raw counter primitives ---------------------------------------------------
    def _guard_redis_failure(self, exc: Exception) -> None:
        """Map Redis connectivity failures to a fail-closed safe error. Admission without
        a working counter is never allowed — the reservation state is unknown."""
        import redis as redis_module

        if isinstance(exc, (redis_module.ConnectionError, redis_module.TimeoutError)):
            raise BudgetEnforcementUnavailableError(
                "Redis budget enforcement unavailable — consultation refused (fail-closed)"
            ) from exc

    def _increment(self, key: str, delta_tokens: int, delta_cost_micros: int, now: datetime | None) -> tuple[int, int]:
        """Returns the (tokens, cost_micros) AFTER this increment. One primitive for both
        reserve and settle so redis and in-memory stay behaviorally identical.

        Redis path: a MULTI/EXEC pipeline (redis-py pipelines are transactional by
        default) so both counters and the TTL bump commit atomically — two workers racing
        on the same key serialize at Redis, never double-count or lose an increment."""
        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                pipe.hincrby(key, "tokens", delta_tokens)
                pipe.hincrby(key, "cost_micros", delta_cost_micros)
                pipe.expire(key, self._ttl(now))
                tokens, cost = pipe.execute()[:2]
            except BudgetEnforcementUnavailableError:
                raise
            except Exception as exc:
                self._guard_redis_failure(exc)
                raise
            return int(tokens), int(cost)
        with self._lock:
            tokens, cost = self._local.get(key, (0, 0))
            tokens += delta_tokens
            cost += delta_cost_micros
            self._local[key] = (tokens, cost)
            return tokens, cost

    def _current(self, key: str) -> tuple[int, int]:
        if self._redis is not None:
            try:
                raw = self._redis.hgetall(key) or {}
            except Exception as exc:
                self._guard_redis_failure(exc)
                raise
            return int(raw.get("tokens", 0)), int(raw.get("cost_micros", 0))
        with self._lock:
            return self._local.get(key, (0, 0))

    # -- public API ---------------------------------------------------------------
    def reserve(self, user_id: str, *, estimated_tokens: int, estimated_cost_usd: Decimal | str, now: datetime | None = None) -> None:
        """Charge the worst-case estimate BEFORE the outbound call. Raises BudgetExceededError
        (leaving the reservation in place — it counts toward the day either way) when the
        estimate alone would cross a ceiling."""
        est_tokens = int(estimated_tokens)
        est_cost_micros = to_micros(estimated_cost_usd)
        key = self._key(user_id, now=now)
        tokens_after, cost_after = self._increment(key, est_tokens, est_cost_micros, now)
        if tokens_after > self.token_budget:
            raise BudgetExceededError("tokens", budgeted=self.token_budget, requested=tokens_after)
        if cost_after > self.cost_budget_micros:
            raise BudgetExceededError("cost", budgeted=self.cost_budget_micros, requested=cost_after)

    def settle(self, user_id: str, *, reserved_tokens: int, actual_tokens: int, reserved_cost_usd: Decimal | str, actual_cost_usd: Decimal | str, now: datetime | None = None) -> None:
        """Reconcile a reservation with real usage: release the estimate, charge the actual.
        Only ever called after reserve() for the same call."""
        key = self._key(user_id, now=now)
        delta_tokens = int(actual_tokens) - int(reserved_tokens)
        delta_cost = to_micros(actual_cost_usd) - to_micros(reserved_cost_usd)
        self._increment(key, delta_tokens, delta_cost, now)

    def remaining(self, user_id: str, *, now: datetime | None = None) -> tuple[int, Decimal]:
        """(tokens_left, cost_left_usd) for the caller's own visibility. Never negative."""
        tokens, cost = self._current(self._key(user_id, now=now))
        return (
            max(self.token_budget - tokens, 0),
            from_micros(max(self.cost_budget_micros - cost, 0)),
        )


def build_budget_store_from_settings(settings=None) -> DailyBudgetStore:
    """One store per construction; routers build it per request from get_settings() — cheap
    (a Redis connection pool is shared inside redis-py via from_url's connection pooling).

    FAIL CLOSED in production: without REDIS_URL the counters would be process-local, so
    every backend replica would enforce the ceiling independently (N workers → N× the
    daily budget). Production deployments therefore refuse the consultation surface until
    distributed enforcement is configured. Non-production keeps the in-memory fallback for
    local dev / the E2E harness, as documented in the module docstring."""
    settings = settings or get_settings()
    if settings.environment == "production" and not settings.redis_url:
        raise BudgetEnforcementUnavailableError(
            "OPENAI_CONSULT requires REDIS_URL in production — distributed budget "
            "enforcement is mandatory and its absence is fail-closed, not per-process"
        )
    return DailyBudgetStore(
        token_budget=settings.openai_consult_daily_token_budget,
        cost_budget_usd=settings.openai_consult_daily_cost_budget_usd,
        redis_url=settings.redis_url,
    )
