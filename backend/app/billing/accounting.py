"""The ONLY accounting writer: usage_log rows + enforcement rollups, atomically.

Every provider call goes through record_usage() — never a direct db.add(UsageLog(...)).
This guarantees the per-call audit record and the enforcement rollups are the SAME
events (one pipeline, zero drift). Cost comes exclusively from app/providers/pricing.py;
a price-unknown call records cost_usd=NULL and still counts tokens, but contributes a
CONFIGURED fallback rate to the cost rollup so enforcement stays conservative rather
than blind (see billing_unknown_cost_fallback_usd_per_1k).

Rollup writes are SQL-side atomic increments (INSERT ... ON CONFLICT DO UPDATE with
arithmetic on the stored row), so N concurrent workers cannot lose an increment — no
read-modify-write race anywhere. SQLite (tests) supports the same upsert syntax.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.billing import BillingTier
from app.models.usage import UsageLog
from app.providers.pricing import estimate_cost


def _period_stamps(now: datetime) -> tuple[str, str]:
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m")


def _effective_cost_usd(cost: Decimal | None, settings) -> Decimal:
    """Cost that enforcement counts. Known price → the real price; unknown price → the
    configured fallback per-1k rate (conservative default, never silently free)."""
    if cost is not None:
        return cost
    fallback = Decimal(str(settings.billing_unknown_cost_fallback_usd_per_1k))
    return fallback  # caller multiplies by (total_tokens / 1000)


def record_usage(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    role: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    agent: str | None = None,
    task: str | None = None,
    tier: str = BillingTier.free.value,
    conversation_id: uuid.UUID | None = None,
    cost_usd: Decimal | None = None,
    kind: str = "consumption",
    created_at: datetime | None = None,
) -> UsageLog:
    """Write one usage event: per-call audit row + atomic rollup increments.

    For kind="refund", tokens/cost must be the POSITIVE magnitude being returned; the
    caller is expected to have validated the refund ceiling (see refund_usage for the
    validated wrapper). Rollup increments flip sign for refunds; DB CHECK constraints
    keep rollups non-negative, and refund_usage refuses over-refunds before writing.
    """
    settings = get_settings()
    now = created_at or datetime.now(timezone.utc)
    prompt_tokens = max(int(prompt_tokens), 0)
    completion_tokens = max(int(completion_tokens), 0)
    total_tokens = prompt_tokens + completion_tokens
    if cost_usd is None:
        cost_usd = estimate_cost(provider, model, prompt_tokens, completion_tokens)

    row = UsageLog(
        user_id=user_id,
        conversation_id=conversation_id,
        role=role,
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        tenant_id=tenant_id,
        agent=agent,
        task=task,
        tier=tier,
        kind=kind,
        created_at=now,
    )
    db.add(row)

    # Rollup increments — atomic, sign-flipped for refunds. Micros are integer (ceiling
    # rounding for the fractional part, mirroring to_micros' conservative bias).
    cost_micros = int((Decimal(str(cost_usd)) * 1_000_000).to_integral_value(rounding="ROUND_CEILING")) \
        if cost_usd is not None else int(
        (_effective_cost_usd(None, settings) * Decimal(total_tokens) / 1000 * 1_000_000)
        .to_integral_value(rounding="ROUND_CEILING"))
    if kind == "refund":
        cost_micros = -cost_micros
    sign = -1 if kind == "refund" else 1
    day_start, month_start = _period_stamps(now)

    for subject_type, subject_id in (("user", user_id), ("tenant", tenant_id)):
        if subject_id is None:
            continue
        # Raw-SQL binds are dialect-dependent: PG wants real UUIDs, sqlite wants strings.
        bind_uuid = str(subject_id) if db.bind.dialect.name == "sqlite" else subject_id
        for period_type, period_start in (("day", day_start), ("month", month_start)):
            db.execute(
                text("""
                    INSERT INTO usage_rollups
                        (id, subject_type, subject_id, period_type, period_start,
                         tokens_in, tokens_out, cost_micros, updated_at)
                    VALUES (:rid, :st, :sid, :pt, :ps,
                            :tokens_in, :tokens_out, :cost_micros, :updated_at)
                    ON CONFLICT (subject_type, subject_id, period_type, period_start)
                    DO UPDATE SET
                        tokens_in  = usage_rollups.tokens_in  + :tokens_in,
                        tokens_out = usage_rollups.tokens_out + :tokens_out,
                        cost_micros = usage_rollups.cost_micros + :cost_micros,
                        updated_at = :updated_at
                """),
                {
                    "rid": uuid.uuid4() if db.bind.dialect.name != "sqlite" else str(uuid.uuid4()),
                    "st": subject_type,
                    "sid": bind_uuid,
                    "pt": period_type,
                    "ps": period_start,
                    "tokens_in": sign * prompt_tokens,
                    "tokens_out": sign * completion_tokens,
                    "cost_micros": cost_micros,
                    "updated_at": now,
                },
            )
    return row


def refund_usage(
    db: Session,
    *,
    user_id: uuid.UUID | None,
    tenant_id: uuid.UUID | None,
    role: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: Decimal,
    agent: str | None = None,
    task: str | None = None,
    tier: str = BillingTier.free.value,
    created_at: datetime | None = None,
) -> UsageLog:
    """Validated refund: refuses to refund more than the subject's recorded consumption
    for the current day (tokens and cost checked independently), then writes a negative
    usage event through the same pipeline. Over-refund attempts raise ValueError and
    leave NO rows behind."""
    settings = get_settings()
    now = created_at or datetime.now(timezone.utc)
    day_start, _ = _period_stamps(now)
    p_tokens = max(int(prompt_tokens), 0)
    c_tokens = max(int(completion_tokens), 0)

    row = db.execute(
        text("""
            SELECT tokens_in, tokens_out, cost_micros FROM usage_rollups
             WHERE subject_type = 'user' AND subject_id = :sid
               AND period_type = 'day' AND period_start = :ps
        """),
        {"sid": str(user_id) if db.bind.dialect.name == "sqlite" else user_id, "ps": day_start},
    ).first()
    recorded_in = int(row[0]) if row else 0
    recorded_out = int(row[1]) if row else 0
    recorded_cost = int(row[2]) if row else 0
    refund_cost_micros = int((Decimal(str(cost_usd)) * 1_000_000).to_integral_value(rounding="ROUND_CEILING"))

    if p_tokens > recorded_in or c_tokens > recorded_out or refund_cost_micros > recorded_cost:
        raise ValueError(
            "refund exceeds recorded consumption for the current UTC day — refused "
            f"(refund in={p_tokens} out={c_tokens} cost_micros={refund_cost_micros}; "
            f"recorded in={recorded_in} out={recorded_out} cost_micros={recorded_cost})"
        )

    return record_usage(
        db,
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        provider=provider,
        model=model,
        prompt_tokens=p_tokens,
        completion_tokens=c_tokens,
        agent=agent,
        task=task,
        tier=tier,
        cost_usd=cost_usd,
        kind="refund",
        created_at=now,
    )
