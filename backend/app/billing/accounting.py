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
    # usage_rollups.period_start is a DATE column (app/models/billing.py) on BOTH dialects.
    # A bare "%Y-%m" month key is not a valid date — PostgreSQL rejects it outright
    # (InvalidDatetimeFormat ... '2026-11'), killing the request's transaction mid-chat.
    # SQLite's TEXT storage string-compared the bare month fine, which is exactly why the
    # in-memory-SQLite billing suite never caught this and every real-Postgres record_usage
    # path failed (found 2026-11-18 via the real-PG regression sweep). A month period is
    # anchored at its first day, which is a valid DATE on both dialects.
    return now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-01")


def _effective_cost_usd(cost: Decimal | None, settings) -> Decimal:
    """Cost that enforcement counts. Known price → the real price; unknown price → the
    configured fallback per-1k rate (conservative default, never silently free)."""
    if cost is not None:
        return cost
    fallback = Decimal(str(settings.billing_unknown_cost_fallback_usd_per_1k))
    return fallback  # caller multiplies by (total_tokens / 1000)


def _usage_row_by_pk(db: Session, pk) -> UsageLog:
    """Fetch a usage row by primary key through the typed column. Raw sqlite rows come
    back as strings and the postgresql UUID bind processor wants real UUIDs — coerce
    explicitly so both dialects work."""
    from sqlalchemy import select as _select

    typed_pk = uuid.UUID(pk) if isinstance(pk, str) else pk
    return db.execute(_select(UsageLog).where(UsageLog.id == typed_pk)).scalar_one()



def _apply_rollup_delta(db: Session, *, subject_type: str, subject_id, period_type: str,
                        period_start: str, d_tokens_in: int, d_tokens_out: int,
                        d_cost_micros: int, updated_at: datetime) -> None:
    """Atomic rollup increment. UPDATE-first, INSERT-on-miss — deliberately NOT an
    upsert: SQLite validates CHECK constraints on the PROPOSED insert row, so a refund
    delta (negative) would fail the non-negative CHECKs even when the resolved row
    stays non-negative; PostgreSQL only checks the final row. UPDATE-first is correct
    and atomic on BOTH dialects, and the unique constraint backs the rare INSERT race
    (loser retries as an UPDATE)."""
    bind = getattr(db, "bind", None)
    is_sqlite = bind is not None and bind.dialect.name == "sqlite"
    params = {"st": subject_type, "sid": subject_id, "pt": period_type, "ps": period_start,
              "tin": d_tokens_in, "tout": d_tokens_out, "cost": d_cost_micros, "upd": updated_at}
    updated = db.execute(
        text("""
            UPDATE usage_rollups
               SET tokens_in = tokens_in + :tin,
                   tokens_out = tokens_out + :tout,
                   cost_micros = cost_micros + :cost,
                   updated_at = :upd
             WHERE subject_type = :st AND subject_id = :sid
               AND period_type = :pt AND period_start = :ps
        """),
        params,
    ).rowcount
    if updated:
        return
    try:
        with db.begin_nested():
            db.execute(
                text("""
                    INSERT INTO usage_rollups
                        (id, subject_type, subject_id, period_type, period_start,
                         tokens_in, tokens_out, cost_micros, updated_at)
                    VALUES (:rid, :st, :sid, :pt, :ps, :tin, :tout, :cost, :upd)
                """),
                {**params,
                 "rid": str(uuid.uuid4()) if is_sqlite else uuid.uuid4()},
            )
    except Exception:
        # Lost an INSERT race: the row now exists — reapply as a plain UPDATE.
        db.execute(
            text("""
                UPDATE usage_rollups
                   SET tokens_in = tokens_in + :tin,
                       tokens_out = tokens_out + :tout,
                       cost_micros = cost_micros + :cost,
                       updated_at = :upd
                 WHERE subject_type = :st AND subject_id = :sid
                   AND period_type = :pt AND period_start = :ps
            """),
            params,
        )


def _derive_tenant_id(db: Session, user_id: uuid.UUID | None) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Tenant membership comes from the USER ROW, never from the caller's word.

    Financial-invariant reason: if callers could pass an arbitrary tenant_id, a buggy or
    hostile path could attribute spend to (or steal budget from) another tenant. The
    user row is the single source of truth for membership."""
    if user_id is None:
        return None, None
    from sqlalchemy import select as _select

    from app.models.user import User as _User

    row = db.execute(_select(_User.tenant_id).where(_User.id == user_id)).first()
    db_tenant = row[0] if row else None
    return user_id, db_tenant


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
    idempotency_key: str | None = None,
) -> UsageLog:
    """Write one usage event: per-call audit row + atomic rollup increments.

    Financial invariants enforced here:
    * TENANT DERIVATION — tenant_id is read from the user row; a caller-supplied value
      that DISAGREES with membership raises ValueError (unauthorized budget attribution
      is refused, never recorded). Pass tenant_id=None and let it derive.
    * IDEMPOTENCY — with idempotency_key set, a duplicate event (same key, any number of
      concurrent writers) yields exactly ONE audit row and ONE rollup increment. Ordinary
      calls (key=None) never dedupe: each retry of a FAILED call is a distinct billable
      event and must count.
    * REFUNDS — for kind="refund", tokens/cost are the POSITIVE magnitude returned; the
      validated wrapper refund_usage enforces the ceiling. DB CHECKs keep rollups
      non-negative.
    """
    settings = get_settings()
    now = created_at or datetime.now(timezone.utc)
    prompt_tokens = max(int(prompt_tokens), 0)
    completion_tokens = max(int(completion_tokens), 0)
    total_tokens = prompt_tokens + completion_tokens
    if cost_usd is None:
        cost_usd = estimate_cost(provider, model, prompt_tokens, completion_tokens)

    # Tenant membership from the user row (see _derive_tenant_id).
    user_id, membership = _derive_tenant_id(db, user_id)
    if tenant_id is not None and tenant_id != membership:
        # Fires in BOTH directions: a user WITH membership claiming another tenant, and a
        # membership-less user claiming ANY tenant. No forged attribution is ever recorded.
        raise ValueError(
            "tenant_id disagrees with the user's membership — refusing to record "
            "(unauthorized budget attribution)"
        )
    tenant_id = membership

    if idempotency_key is not None:
        existing = db.execute(
            text("SELECT id FROM usage_log WHERE idempotency_key = :k"),
            {"k": idempotency_key},
        ).first()
        if existing is not None:
            return _usage_row_by_pk(db, existing[0])

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
        idempotency_key=idempotency_key,
        created_at=now,
    )
    db.add(row)
    if idempotency_key is not None:
        # Race-safe dedup: a SAVEPOINT flush surfaces the unique-index violation without
        # touching the caller's outer transaction; the loser reads the winner's row and
        # contributes NOTHING (no audit row, no rollup increment).
        try:
            with db.begin_nested():
                db.flush()
        except Exception:
            existing = db.execute(
                text("SELECT id FROM usage_log WHERE idempotency_key = :k"),
                {"k": idempotency_key},
            ).first()
            if existing is None:
                raise
            return _usage_row_by_pk(db, existing[0])
    else:
        db.flush()

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
        # getattr keeps session-like doubles (which never run the raw SQL) working.
        bind = getattr(db, "bind", None)
        is_sqlite = bind is not None and bind.dialect.name == "sqlite"
        bind_uuid = str(subject_id) if is_sqlite else subject_id
        for period_type, period_start in (("day", day_start), ("month", month_start)):
            _apply_rollup_delta(db, subject_type=subject_type, subject_id=bind_uuid,
                                period_type=period_type, period_start=period_start,
                                d_tokens_in=sign * prompt_tokens, d_tokens_out=sign * completion_tokens,
                                d_cost_micros=cost_micros, updated_at=now)
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
    bind = getattr(db, "bind", None)
    is_sqlite = bind is not None and bind.dialect.name == "sqlite"
    day_start, _ = _period_stamps(now)
    p_tokens = max(int(prompt_tokens), 0)
    c_tokens = max(int(completion_tokens), 0)

    row = db.execute(
        text("""
            SELECT tokens_in, tokens_out, cost_micros FROM usage_rollups
             WHERE subject_type = 'user' AND subject_id = :sid
               AND period_type = 'day' AND period_start = :ps
        """),
        {"sid": str(user_id) if is_sqlite else user_id, "ps": day_start},
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
