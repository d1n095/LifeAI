"""Reservation helpers binding the enforcement store to request flows.

Kept separate from enforcement.py so the store stays request-agnostic. estimate_reservation_cost()
prices a worst-case estimate through the SINGLE price table (app/providers/pricing.py)
and falls back to the configured per-1k rate when a provider/model is unpriced — a
reservation may never be priced at zero merely because pricing is incomplete.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.billing.enforcement import BillingEnforcementUnavailableError
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


@dataclass(frozen=True)
class SubjectReservations:
    """Reservations for BOTH the user subject and (when applicable) the tenant's shared
    envelope. All-or-nothing: if the second reserve fails, the first is released, so no
    subject is ever left holding a phantom reservation."""

    user_reservation: object
    tenant_reservation: object | None = None

    def _each(self):
        yield self.user_reservation
        if self.tenant_reservation is not None:
            yield self.tenant_reservation


def reserve_compute(
    db: Session, *, user, limits, estimated_tokens: int, estimated_cost_usd: Decimal,
    settings: Settings | None = None,
    idempotency_key: str | None = None,
):
    """Reserve against the user subject AND (when the user belongs to a tenant) the
    tenant's shared envelope — both must admit, or nothing is charged. Fail-closed and
    reject-with-zero-writes semantics come from the store; see enforcement.reserve.

    Tenant caps ARE enforced here (P1 review fix): the tenant's own overrides + tier
    policy are computed and passed to the tenant-side reserve. Previously the tenant
    reservation ran with unlimited caps, so a tenant's configured daily/monthly ceiling
    never bound during admission — only the user's did."""
    from app.billing.enforcement import to_micros
    from app.billing.tiers import policy_for_tier
    from app.models.billing import BillingTenant

    settings = settings or get_settings()
    store = _store(settings)
    tenant_reservation = None
    if user is not None and getattr(user, "tenant_id", None):
        tenant = db.query(BillingTenant).filter_by(id=user.tenant_id).one_or_none()
        if tenant is not None and tenant.is_active:
            # Tenant-side caps: the tenant row's overrides REPLACE the tier policy
            # defaults when set (same composition rule as app/billing/limits.py).
            tier_value = tenant.tier.value if hasattr(tenant.tier, "value") else tenant.tier
            policy = policy_for_tier(tier_value, settings)
            daily_cost = (tenant.daily_cost_cap_micros / 1_000_000
                          if tenant.daily_cost_cap_micros is not None else policy.daily_cost_usd)
            monthly_cost = (tenant.monthly_cost_cap_micros / 1_000_000
                            if tenant.monthly_cost_cap_micros is not None else policy.monthly_cost_usd)
            tenant_reservation = store.reserve(
                subject_type="tenant", subject_id=str(tenant.id), idempotency_key=idempotency_key,
                estimated_tokens=estimated_tokens, estimated_cost_usd=estimated_cost_usd,
                token_caps=(policy.daily_token_cap, policy.monthly_token_cap),
                cost_caps_micros=(
                    to_micros(daily_cost) if daily_cost is not None else None,
                    to_micros(monthly_cost) if monthly_cost is not None else None,
                ),
            )
    try:
        user_reservation = store.reserve(
            subject_type="user", subject_id=str(user.id), idempotency_key=idempotency_key,
            estimated_tokens=estimated_tokens, estimated_cost_usd=estimated_cost_usd,
            token_caps=limits.token_caps(), cost_caps_micros=limits.cost_caps_micros(),
        )
    except Exception:
        if tenant_reservation is not None:
            store.release(tenant_reservation)
        raise
    return SubjectReservations(user_reservation=user_reservation, tenant_reservation=tenant_reservation)


def settle_reservation(reservations, *, actual_tokens: int, actual_cost_usd: Decimal) -> None:
    settings = get_settings()
    store = _store(settings)
    for res in reservations._each():
        store.settle(res, actual_tokens=actual_tokens, actual_cost_usd=actual_cost_usd)


def release_reservation(reservations) -> None:
    settings = get_settings()
    store = _store(settings)
    for res in reservations._each():
        store.release(res)




class BillingPriceUnverifiedError(BillingEnforcementUnavailableError):
    reservation_outcome_unknown = False


class ChatAttemptBudget:
    """Existing reserve/settle primitives, bound to one actual provider attempt.

    Ambiguous effects retain a HOLD (not fabricated measured spend). The durable chat
    claim prevents reinvocation; reconciliation requires real external evidence.
    """
    def __init__(self, db, *, user, limits, estimated_tokens, source_ref, reply_slot, enforce):
        self.db, self.user, self.limits = db, user, limits
        self.estimated_tokens = estimated_tokens
        self.reservation = None
        self.source_ref = source_ref
        self.reply_slot = reply_slot
        self.requires_usage = enforce

    def before(self, provider, model):
        import json
        from app.audit import record_audit
        from app.billing.enforcement import BillingEnforcementUnavailableError
        # Persist intent identity before Redis too: an unknown counter-store outcome
        # remains attributable even if no provider request is ultimately invoked.
        self.reply_slot.provider, self.reply_slot.model = provider, model
        record_audit(self.db, user_id=self.user.id, action="chat_provider_intent", entity_type="message",
                     entity_id=str(self.reply_slot.id), commit=False,
                     detail=json.dumps({"source_ref":self.source_ref,"provider":provider,"model":model,
                                        "tenant_id":str(self.user.tenant_id) if self.user.tenant_id else None}))
        self.db.commit()
        if not self.requires_usage:
            return
        from app.providers.catalog import get_model_spec, evaluate_model, ModelSelectionPolicy, ModelSelectionError
        if self.reservation is not None:
            raise BillingEnforcementUnavailableError("Previous attempt budget is unresolved.")
        # Unknown/stale external pricing cannot become an invented admission guarantee.
        try:
            spec = get_model_spec(provider, model)
            if spec.external:
                ceiling = spec.pricing.input_per_million_usd
                if ceiling is None or not evaluate_model(spec, ModelSelectionPolicy(
                    allow_external=True, max_input_price_per_million_usd=ceiling))[0]:
                    raise BillingPriceUnverifiedError("External price bound is not verified.")
        except ModelSelectionError:
            raise BillingPriceUnverifiedError("Provider/model has no verified price bound.") from None
        self.reservation = reserve_compute(
            self.db, user=self.user, limits=self.limits, estimated_tokens=self.estimated_tokens,
            estimated_cost_usd=estimate_reservation_cost(None, self.estimated_tokens, provider=provider, model=model),
            idempotency_key=f"{self.source_ref}:{provider}:{model}",
        )

    def succeeded(self, result):
        if not self.requires_usage:
            return
        usage = result.raw_usage or {}
        settle_reservation(self.reservation, actual_tokens=usage.get("prompt_tokens", 0)+usage.get("completion_tokens", 0),
                           actual_cost_usd=estimate_reservation_cost(None, usage.get("prompt_tokens", 0)+usage.get("completion_tokens", 0),
                                                                    provider=result.provider, model=result.model))
        self.reservation = None

    def failed(self, error):
        if self.reservation is None:
            return
        if getattr(error, "provider_request_may_have_left", None) is False:
            release_reservation(self.reservation)
            self.reservation = None
        # Otherwise deliberately retain the hold. Never label unknown external effects free.
