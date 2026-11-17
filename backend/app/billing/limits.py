"""Effective spending limits per subject: tier policy defaults ← tenant overrides ←
(no Settings override of individual caps beyond the tier defaults — keep one mechanism).

Both daily and monthly caps are ALWAYS resolved together so enforcement never checks
one window against stale limits. Caps are None = unlimited; only enterprise policy can
produce None, and only when the operator explicitly configured unlimited enterprise caps.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing.enforcement import to_micros
from app.billing.tiers import UsagePolicy, policy_for_tier
from app.models.billing import BillingTenant, BillingTier
from app.models.user import User


@dataclass(frozen=True)
class EffectiveLimits:
    tier: str
    tenant_id: uuid.UUID | None
    daily_cost_usd: float | None
    monthly_cost_usd: float | None
    daily_token_cap: int | None
    monthly_token_cap: int | None

    def cost_caps_micros(self) -> tuple[int | None, int | None]:
        return (
            to_micros(self.daily_cost_usd) if self.daily_cost_usd is not None else None,
            to_micros(self.monthly_cost_usd) if self.monthly_cost_usd is not None else None,
        )

    def token_caps(self) -> tuple[int | None, int | None]:
        return (self.daily_token_cap, self.monthly_token_cap)


def effective_limits(db: Session, user: User | None, settings=None) -> EffectiveLimits:
    """Resolve the limits that apply to ONE user (and, via tenant membership, their
    tenant's shared envelope — enforced against the tenant subject by the caller when
    the user has a tenant; personal accounts enforce against themselves only)."""
    settings = settings or __import__("app.config", fromlist=["get_settings"]).get_settings()
    tier_value = settings.billing_default_tier
    tenant_id: uuid.UUID | None = None
    daily_override = monthly_override = None

    if user is not None and getattr(user, "tenant_id", None):
        tenant = db.execute(
            select(BillingTenant).where(BillingTenant.id == user.tenant_id)
        ).scalar_one_or_none()
        if tenant is not None and tenant.is_active:
            tenant_id = tenant.id
            tier_value = tenant.tier.value if isinstance(tenant.tier, BillingTier) else tenant.tier
            daily_override = tenant.daily_cost_cap_micros
            monthly_override = tenant.monthly_cost_cap_micros

    policy: UsagePolicy = policy_for_tier(tier_value, settings)
    daily_cost = policy.daily_cost_usd
    monthly_cost = policy.monthly_cost_usd
    # Tenant cap overrides REPLACE the tier defaults entirely when set (no silent
    # combining of two mechanisms).
    if daily_override is not None:
        daily_cost = daily_override / 1_000_000
    if monthly_override is not None:
        monthly_cost = monthly_override / 1_000_000

    # Revenue-backed allocation (P3): when SETTLED revenue actually exists for the
    # current period, the allocatable envelope BINDS — the monthly ceiling becomes
    # min(policy cap, allocatable). Estimated revenue can never raise or set a ceiling;
    # with no revenue rows at all this is the bootstrap state and tier caps govern.
    from app.billing.allocation import subject_envelope

    envelope = subject_envelope(db, tenant_id=tenant_id, settings=settings)
    if envelope.bound and monthly_cost is not None:
        monthly_cost = min(monthly_cost, envelope.allocatable_micros / 1_000_000)
    elif envelope.bound and monthly_cost is None:
        monthly_cost = envelope.allocatable_micros / 1_000_000

    return EffectiveLimits(
        tier=tier_value,
        tenant_id=tenant_id,
        daily_cost_usd=daily_cost,
        monthly_cost_usd=monthly_cost,
        daily_token_cap=policy.daily_token_cap,
        monthly_token_cap=policy.monthly_token_cap,
    )
