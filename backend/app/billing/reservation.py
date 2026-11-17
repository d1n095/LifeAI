"""Reservation helpers binding the enforcement store to request flows.

Kept separate from enforcement.py so the store stays request-agnostic. estimate_reservation_cost()
prices a worst-case estimate through the SINGLE price table (app/providers/pricing.py)
and falls back to the configured per-1k rate when a provider/model is unpriced — a
reservation may never be priced at zero merely because pricing is incomplete.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.providers.pricing import estimate_cost


def estimate_reservation_cost(
    settings: Settings | None, total_tokens: int, *, provider: str | None = None, model: str | None = None
) -> Decimal:
    settings = settings or get_settings()
    if provider is not None and model is not None:
        priced = estimate_cost(provider, model, 0, total_tokens)
        if priced is not None:
            return priced
    fallback = Decimal(str(settings.billing_unknown_cost_fallback_usd_per_1k))
    return (fallback * Decimal(max(total_tokens, 0)) / 1000).quantize(Decimal("0.000001"))


def _store(settings: Settings):
    from app.billing.enforcement import ComputeBudgetStore

    # One store per process; re-created only when configuration changes (tests).
    global _store_cache, _store_cache_key
    key = settings.redis_url
    if _store_cache is None or _store_cache_key != key:
        _store_cache = ComputeBudgetStore(redis_url=key)
        _store_cache_key = key
    return _store_cache


_store_cache = None
_store_cache_key = None


def reserve_compute(
    db: Session, *, user, limits, estimated_tokens: int, estimated_cost_usd: Decimal,
    settings: Settings | None = None,
):
    """Reserve against the user subject AND (when the user belongs to a tenant) the
    tenant's shared envelope — both must admit, or nothing is charged. Fail-closed and
    reject-with-zero-writes semantics come from the store; see enforcement.reserve."""
    from app.billing.enforcement import BillingEnforcementUnavailableError
    from app.models.billing import BillingTenant

    settings = settings or get_settings()
    store = _store(settings)
    tenant_reservation = None
    if user is not None and getattr(user, "tenant_id", None):
        tenant = db.query(BillingTenant).filter_by(id=user.tenant_id).one_or_none()
        if tenant is not None and tenant.is_active:
            tenant_reservation = store.reserve(
                subject_type="tenant", subject_id=str(tenant.id),
                estimated_tokens=estimated_tokens, estimated_cost_usd=estimated_cost_usd,
            )
    try:
        reservation = store.reserve(
            subject_type="user", subject_id=str(user.id),
            estimated_tokens=estimated_tokens, estimated_cost_usd=estimated_cost_usd,
            token_caps=limits.token_caps(), cost_caps_micros=limits.cost_caps_micros(),
        )
    except Exception:
        if tenant_reservation is not None:
            store.release(tenant_reservation)
        raise
    if tenant_reservation is not None:
        # Both subjects reserved; return the user's reservation — settle/release mirror
        # the same double-subject structure.
        reservation.tenant_reservation = tenant_reservation  # type: ignore[attr-defined]
    return reservation


def settle_reservation(reservation, *, actual_tokens: int, actual_cost_usd: Decimal) -> None:
    from app.billing.enforcement import ComputeBudgetStore

    settings = get_settings()
    store = _store(settings)
    store.settle(reservation, actual_tokens=actual_tokens, actual_cost_usd=actual_cost_usd)
    tenant_reservation = getattr(reservation, "tenant_reservation", None)
    if tenant_reservation is not None:
        store.settle(tenant_reservation, actual_tokens=actual_tokens, actual_cost_usd=actual_cost_usd)


def release_reservation(reservation) -> None:
    settings = get_settings()
    store = _store(settings)
    store.release(reservation)
    tenant_reservation = getattr(reservation, "tenant_reservation", None)
    if tenant_reservation is not None:
        store.release(tenant_reservation)
