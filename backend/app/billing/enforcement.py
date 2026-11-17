"""Concurrency-safe compute budget enforcement (reserve/settle/release).

Generalizes the proven reserve/settle pattern of app/openai_consult/budgets.py to the
platform-wide Revenue & Compute Engine, without replacing it:

* SAME storage stance: with REDIS_URL, counters live in Redis (transactional MULTI/EXEC
  pipeline) so enforcement is shared across replicas; without Redis, an in-process
  fallback is correct for exactly one process (local dev / E2E) — and in production,
  when billing_enforcement_enabled, a missing or failing counter store FAILS CLOSED
  (spending refused, never admitted uncounted).
* SAME integer micro-USD accounting (imported from budgets.py — one implementation).
* WINDOWS: rolling UTC day AND UTC month, per subject (user or tenant). Two windows mean
  a monthly cap holds even if usage is spread thinly across days.
* reserve() charges a worst-case estimate BEFORE any outbound call; settle() reconciles
  with the real usage (the unused part of the reservation is released automatically);
  release() returns a reservation after a failed call (a failed provider must not leave
  phantom spend behind).

Store keys: billing:budget:{subject_type}:{subject_id}:{window}:{period_stamp}
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from app.openai_consult.budgets import _seconds_until_utc_midnight, to_micros  # noqa: F401


class BillingEnforcementUnavailableError(RuntimeError):
    """Budget enforcement could not be performed and the reservation state is UNKNOWN.
    When enforcement is enabled this is fail-closed: the call is refused. Never a raw
    driver traceback — operators get a clean, actionable message."""


class BillingBudgetExceededError(RuntimeError):
    """Raised BEFORE any outbound call: admitting the request would push the subject past
    a cap for the window. `window` is "day" or "month"; `which` is "tokens" or "cost"."""

    def __init__(self, window: str, which: str, *, subject_type: str, subject_id: str, budgeted: int, requested: int):
        self.window = window
        self.which = which
        self.subject_type = subject_type
        self.subject_id = subject_id
        super().__init__(
            f"{window} {which} budget exhausted for {subject_type}:{subject_id}: "
            f"budgeted={budgeted} requested_total={requested}"
        )


@dataclass(frozen=True)
class Reservation:
    subject_type: str
    subject_id: str
    tokens: int
    cost_micros: int
    period_keys: tuple[str, str]  # (day_key, month_key) the reservation lives under


def _month_key(now: datetime) -> str:
    return now.strftime("%Y%m")


def _seconds_until_utc_month_end(now: datetime) -> int:
    import calendar

    year, month = now.year, now.month
    # First instant of the next month: December wraps to the next year. monthrange is
    # kept as a cheap validity assertion for the (already valid) month component.
    calendar.monthrange(year, month)
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12 else \
        datetime(year, month + 1, 1, tzinfo=timezone.utc)
    return max(int((end - now).total_seconds()) + 60, 60)


class ComputeBudgetStore:
    """Day+month token/cost counters per subject. One primitive `_increment` underlies
    reserve/settle/release so the Redis and in-memory paths stay behaviorally identical
    (same contract as openai_consult's DailyBudgetStore)."""

    def __init__(self, *, redis_url: str | None):
        self._redis = None
        self._local: dict[str, tuple[int, int]] = {}
        self._lock = threading.Lock()
        if redis_url:
            import redis as redis_module

            self._redis = redis_module.Redis.from_url(redis_url, decode_responses=True)

    # -- keys ---------------------------------------------------------------------
    @staticmethod
    def _keys(subject_type: str, subject_id: str, now: datetime) -> tuple[str, str, int, int]:
        day = now.strftime("%Y%m%d")
        month = _month_key(now)
        base = f"billing:budget:{subject_type}:{subject_id}"
        return (f"{base}:day:{day}", f"{base}:month:{month}",
                _seconds_until_utc_midnight(now), _seconds_until_utc_month_end(now))

    # -- raw primitive ------------------------------------------------------------
    def _increment(self, key: str, delta_tokens: int, delta_cost_micros: int,
                   ttl_seconds: int) -> tuple[int, int]:
        if self._redis is not None:
            try:
                pipe = self._redis.pipeline()
                pipe.hincrby(key, "tokens", delta_tokens)
                pipe.hincrby(key, "cost_micros", delta_cost_micros)
                pipe.expire(key, ttl_seconds)
                tokens, cost = pipe.execute()[:2]
                return int(tokens), int(cost)
            except Exception as exc:
                import redis as redis_module

                if isinstance(exc, (redis_module.ConnectionError, redis_module.TimeoutError)):
                    raise BillingEnforcementUnavailableError(
                        "Redis budget enforcement unavailable — spending refused (fail-closed)"
                    ) from exc
                raise
        with self._lock:
            tokens, cost = self._local.get(key, (0, 0))
            tokens += delta_tokens
            cost += delta_cost_micros
            self._local[key] = (tokens, cost)
            return tokens, cost

    def _read(self, key: str) -> tuple[int, int]:
        if self._redis is not None:
            try:
                values = self._redis.hgetall(key)
                return int(values.get("tokens", 0)), int(values.get("cost_micros", 0))
            except Exception as exc:
                import redis as redis_module

                if isinstance(exc, (redis_module.ConnectionError, redis_module.TimeoutError)):
                    raise BillingEnforcementUnavailableError(
                        "Redis budget enforcement unavailable — cannot prove budget headroom (fail-closed)"
                    ) from exc
                raise
        with self._lock:
            return self._local.get(key, (0, 0))

    # -- public contract ----------------------------------------------------------
    def reserve(
        self,
        *,
        subject_type: str,
        subject_id: str,
        estimated_tokens: int,
        estimated_cost_usd: Decimal | str | int,
        now: datetime | None = None,
        token_caps: tuple[int | None, int | None] = (None, None),
        cost_caps_micros: tuple[int | None, int | None] = (None, None),
    ) -> Reservation:
        """Charge a worst-case estimate against BOTH windows, checking caps first.
        `token_caps`/`cost_caps_micros` are (daily, monthly) effective caps; None = no cap.
        All increments commit or none does (per-window atomicity via pipeline; the
        two windows are checked-then-charged, with the check reading the same atomic
        counter state the charge mutates — a rejected reserve leaves NO partial state
        because it never increments)."""
        now = now or datetime.now(timezone.utc)
        day_key, month_key, day_ttl, month_ttl = self._keys(subject_type, subject_id, now)
        cost_micros = to_micros(estimated_cost_usd)
        tokens = max(int(estimated_tokens), 0)

        # Check both windows BEFORE charging either (reject => zero writes).
        for key, window, token_cap, cost_cap in (
            (day_key, "day", token_caps[0], cost_caps_micros[0]),
            (month_key, "month", token_caps[1], cost_caps_micros[1]),
        ):
            used_tokens, used_cost = self._read(key)
            if token_cap is not None and used_tokens + tokens > token_cap:
                raise BillingBudgetExceededError(window, "tokens", subject_type=subject_type,
                                                 subject_id=subject_id, budgeted=token_cap,
                                                 requested=used_tokens + tokens)
            if cost_cap is not None and used_cost + cost_micros > cost_cap:
                raise BillingBudgetExceededError(window, "cost", subject_type=subject_type,
                                                 subject_id=subject_id, budgeted=cost_cap,
                                                 requested=used_cost + cost_micros)

        self._increment(day_key, tokens, cost_micros, day_ttl)
        self._increment(month_key, tokens, cost_micros, month_ttl)
        return Reservation(subject_type=subject_type, subject_id=subject_id,
                           tokens=tokens, cost_micros=cost_micros,
                           period_keys=(day_key, month_key))

    def settle(self, reservation: Reservation, *, actual_tokens: int,
               actual_cost_usd: Decimal | str | int) -> None:
        """Reconcile a reservation with real usage: the unused part of BOTH windows is
        released automatically (worst-case reservation, actual charge). A negative
        adjustment can never push a counter below zero."""
        now = datetime.now(timezone.utc)
        day_key, month_key = reservation.period_keys
        day_ttl = _seconds_until_utc_midnight(now)
        month_ttl = _seconds_until_utc_month_end(now)
        # NEGATIVE adjustments release the unused part of the worst-case reservation
        # (actual spend replaces the estimate). The sign was deliberately verified by
        # the smoke/test suite: settle must LOWER the counters, never raise them.
        adj_tokens = max(int(actual_tokens), 0) - reservation.tokens
        adj_cost = to_micros(actual_cost_usd) - reservation.cost_micros
        self._increment(day_key, adj_tokens, adj_cost, day_ttl)
        self._increment(month_key, adj_tokens, adj_cost, month_ttl)

    def release(self, reservation: Reservation) -> None:
        """Give the ENTIRE reservation back (provider call failed before any spend)."""
        now = datetime.now(timezone.utc)
        day_key, month_key = reservation.period_keys
        self._increment(day_key, -reservation.tokens, -reservation.cost_micros,
                        _seconds_until_utc_midnight(now))
        self._increment(month_key, -reservation.tokens, -reservation.cost_micros,
                        _seconds_until_utc_month_end(now))

    def remaining(self, *, subject_type: str, subject_id: str,
                  token_caps: tuple[int | None, int | None], cost_caps_micros: tuple[int | None, int | None],
                  now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        day_key, month_key, _, _ = self._keys(subject_type, subject_id, now)
        out: dict[str, tuple[int, int]] = {}
        for key, window in ((day_key, "day"), (month_key, "month")):
            used_tokens, used_cost = self._read(key)
            token_cap, cost_cap = token_caps[0] if window == "day" else token_caps[1], \
                cost_caps_micros[0] if window == "day" else cost_caps_micros[1]
            out[window] = (
                (token_cap - used_tokens) if token_cap is not None else -1,
                (cost_cap - used_cost) if cost_cap is not None else -1,
            )
        return out
