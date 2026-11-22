"""Usage tiers: free (ad-funded), paid, enterprise.

Defaults come from Settings so the founder can reprice without code changes; tenant rows
(migration 0070) can override caps per tenant. The free tier exists to be funded by the
advertising subsystem (app/ads/) — it is intentionally SMALLER than paid tiers, and no
tier ever changes authorization or security behavior (tiers touch ONLY spending limits).
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings


@dataclass(frozen=True)
class UsagePolicy:
    """Spending caps for one tier. None = unlimited (enterprise override; must be set
    deliberately — there is no accidental unlimited tier)."""

    daily_cost_usd: float | None
    monthly_cost_usd: float | None
    daily_token_cap: int | None
    monthly_token_cap: int | None
    ad_funded: bool


def _policy_from_settings(tier: str, settings=None) -> UsagePolicy:
    settings = settings or get_settings()
    if tier == "free":
        return UsagePolicy(
            daily_cost_usd=settings.billing_free_daily_cost_usd,
            monthly_cost_usd=settings.billing_free_monthly_cost_usd,
            daily_token_cap=settings.billing_free_daily_token_cap,
            monthly_token_cap=settings.billing_free_monthly_token_cap,
            ad_funded=True,
        )
    if tier == "enterprise":
        return UsagePolicy(
            daily_cost_usd=settings.billing_enterprise_daily_cost_usd,
            monthly_cost_usd=settings.billing_enterprise_monthly_cost_usd,
            daily_token_cap=settings.billing_enterprise_daily_token_cap,
            monthly_token_cap=settings.billing_enterprise_monthly_token_cap,
            ad_funded=False,
        )
    return UsagePolicy(
        daily_cost_usd=settings.billing_paid_daily_cost_usd,
        monthly_cost_usd=settings.billing_paid_monthly_cost_usd,
        daily_token_cap=settings.billing_paid_daily_token_cap,
        monthly_token_cap=settings.billing_paid_monthly_token_cap,
        ad_funded=False,
    )


def policy_for_tier(tier: str, settings=None) -> UsagePolicy:
    if tier not in ("free", "paid", "enterprise"):
        raise ValueError(f"unknown billing tier: {tier!r}")
    return _policy_from_settings(tier, settings)
