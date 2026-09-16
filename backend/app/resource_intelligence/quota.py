"""Read-only bridge to `app.provider_spend`'s ceiling vocabulary -- provider quota-remaining
awareness. See docs/mainai_v2/MAINAI_RESOURCE_INTELLIGENCE_ROUND2_ADDENDUM.md for the
architecture decision this module implements.

Round 1's own handoff named "no cross-provider quota-remaining tracking" as a known,
undisguised gap. `app.provider_spend.ProviderSpendAuthorization` already carries every ceiling
(`max_cost_usd`/`max_requests`/`max_prompt_tokens`/`max_completion_tokens`) and running
committed total (`spent_* + reserved_*`) this module needs -- no new ledger, no new table, no
modification to `app.provider_spend` itself.

READ-ONLY, UNLOCKED: this module never calls `reserve_provider_spend_call()`/
`settle_provider_spend_call()`/`release_provider_spend_call()`/`revoke_provider_spend()`, and
deliberately does NOT reuse `app.provider_spend.service.get_current_provider_spend_authorization()`
even though it looks like the obvious helper -- that function takes a `SELECT ... FOR UPDATE`
row lock and can itself flip an expired authorization to a terminal status (a real, if minor,
mutation). A resource-intelligence read has no business taking a write lock on the spend
ledger's own row; this module does its own plain, unlocked SELECT instead, exactly matching
`cost_bridge.py`'s own `_settled_usage_events_for_scope()` precedent.

UNKNOWN != ZERO, UNKNOWN != EXHAUSTED: no active authorization, or a ceiling dimension that is
not configured at all (the nullable `max_prompt_tokens`/`max_completion_tokens`), returns
`missing_data=True` for that dimension -- never a fabricated `0.0` (misreadable as "exhausted")
or `1.0` (misreadable as "unlimited")."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.provider_spend import ProviderSpendAuthorization, ProviderSpendAuthorizationStatus
from app.resource_intelligence.types import MetricEnvelope, unknown_metric

_SOURCE = "provider_spend_authorizations"

QUOTA_DIMENSIONS = ("cost_usd", "requests", "prompt_tokens", "completion_tokens")

# A quota this depleted is close enough to exhaustion that a caller should treat it the same way
# `decision.py`'s own acute context thresholds treat context exhaustion: not yet zero, but no
# longer safe to plan more work against without a real risk of a mid-task hard stop.
QUOTA_CRITICAL_REMAINING_FRACTION = 0.1


def _active_authorization(db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID) -> ProviderSpendAuthorization | None:
    return db.execute(
        select(ProviderSpendAuthorization).where(
            ProviderSpendAuthorization.owner_id == owner_id,
            ProviderSpendAuthorization.goal_id == goal_id,
            ProviderSpendAuthorization.status == ProviderSpendAuthorizationStatus.active.value,
        )
    ).scalar_one_or_none()


def provider_quota_remaining(db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID) -> dict[str, MetricEnvelope]:
    """Remaining fraction (0.0..1.0, where 0.0 means fully committed) of each of
    `QUOTA_DIMENSIONS`'s ceiling for the CURRENT active `ProviderSpendAuthorization` on this
    `goal_id`. `missing_data=True` (for ALL FOUR keys) when no active authorization exists for
    this goal_id; missing for a SINGLE dimension only when that dimension's own ceiling is not
    configured (nullable token ceilings) or is `<= 0` (no meaningful fraction).

    `reserved_*` amounts are two-phase holds that may still be released without ever being
    spent -- disclosed via `uncertainty` on every returned envelope, since this fraction may
    under-report the true remaining headroom while a reservation is outstanding."""

    definitions = {
        "cost_usd": "1 - (spent_cost_usd + reserved_cost_usd) / max_cost_usd for the current active provider_spend_authorization on this goal_id",
        "requests": "1 - (spent_requests + reserved_requests) / max_requests for the current active provider_spend_authorization on this goal_id",
        "prompt_tokens": "1 - (spent_prompt_tokens + reserved_prompt_tokens) / max_prompt_tokens for the current active provider_spend_authorization on this goal_id",
        "completion_tokens": "1 - (spent_completion_tokens + reserved_completion_tokens) / max_completion_tokens for the current active provider_spend_authorization on this goal_id",
    }

    auth = _active_authorization(db, owner_id=owner_id, goal_id=goal_id)
    if auth is None:
        return {
            key: unknown_metric(unit="fraction", definition=defn, source=_SOURCE, method=f"no active provider_spend_authorization found for goal_id={goal_id}")
            for key, defn in definitions.items()
        }

    population = f"provider_spend_authorization_id={auth.id}"

    def _envelope(key: str, ceiling: Decimal | int | None, committed: Decimal | int | None) -> MetricEnvelope:
        if ceiling is None:
            return unknown_metric(unit="fraction", definition=definitions[key], source=_SOURCE, method=f"{key} has no ceiling configured on this authorization (nullable field is None)")
        ceiling_f = float(ceiling)
        if ceiling_f <= 0:
            return unknown_metric(unit="fraction", definition=definitions[key], source=_SOURCE, method=f"{key} ceiling is <= 0 ({ceiling}); no meaningful fraction-remaining")
        committed_f = float(committed or 0)
        value = max(0.0, ceiling_f - committed_f) / ceiling_f
        return MetricEnvelope(
            value=value, unit="fraction", definition=definitions[key], denominator=key,
            time_window=None, population=population, sample_size=None, source=_SOURCE,
            method=f"1 - ({committed_f} committed / {ceiling_f} ceiling)",
            missing_data=False,
            uncertainty="includes outstanding two-phase reservations that may still be released; this fraction may under-report true remaining headroom",
            last_updated=auth.authorized_at, trend=None,
        )

    return {
        "cost_usd": _envelope("cost_usd", auth.max_cost_usd, auth.spent_cost_usd + auth.reserved_cost_usd),
        "requests": _envelope("requests", auth.max_requests, auth.spent_requests + auth.reserved_requests),
        "prompt_tokens": _envelope(
            "prompt_tokens", auth.max_prompt_tokens,
            (auth.spent_prompt_tokens + auth.reserved_prompt_tokens) if auth.max_prompt_tokens is not None else None,
        ),
        "completion_tokens": _envelope(
            "completion_tokens", auth.max_completion_tokens,
            (auth.spent_completion_tokens + auth.reserved_completion_tokens) if auth.max_completion_tokens is not None else None,
        ),
    }


def quota_critical(quota: dict[str, MetricEnvelope]) -> bool:
    """True only when at least one KNOWN dimension has fallen at/below
    `QUOTA_CRITICAL_REMAINING_FRACTION`. A dimension with `missing_data=True` never
    contributes `True` here -- UNKNOWN QUOTA != EXHAUSTED QUOTA, exactly like every other
    missing-data path in this package."""

    return any(
        not envelope.missing_data and envelope.value is not None and envelope.value <= QUOTA_CRITICAL_REMAINING_FRACTION
        for envelope in quota.values()
    )
