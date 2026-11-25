"""Infrastructure cost of LOCAL models (ollama).

Two distinct cost concepts, deliberately kept apart:

* USER-FACING cost — app/providers/pricing.py's estimate_cost(): local inference is FREE
  to users (Decimal("0")). This is product policy; changing it changes user behavior.
* PLATFORM cost — this module: the real infrastructure cost of running local compute
  (hardware amortization + energy + margin overhead). Used by compute allocation
  (app/billing/allocation.py), the admin cost dashboard and the revenue simulation —
  never by user billing.

Formula (all knobs in Settings):
    cost_per_token_usd = gpu_hour_usd / (tokens_per_second * 3600) * (1 + overhead_pct/100)

A GPU that can no longer serve requests has cost but no tokens — the per-token figure is
a MODEL of marginal cost, not a claim about sunk hardware cost; the simulation in
docs/REVENUE_COMPUTE_ENGINE.md states its assumptions explicitly.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import get_settings


def local_cost_per_token(settings=None) -> Decimal:
    settings = settings or get_settings()
    if settings.billing_local_gpu_hour_usd <= 0 or settings.billing_local_gpu_tokens_per_second <= 0:
        return Decimal("0")
    per_second = Decimal(str(settings.billing_local_gpu_hour_usd)) / (
        Decimal(str(settings.billing_local_gpu_tokens_per_second)) * 3600
    )
    return (per_second * (1 + Decimal(settings.billing_local_energy_overhead_pct) / 100)).quantize(
        Decimal("0.000000001")
    )


def estimate_local_compute_cost(provider: str, total_tokens: int, settings=None) -> Decimal | None:
    """Platform-side infrastructure cost in USD, or None for non-local providers (callers
    distinguish 'not applicable' from 'free')."""
    if provider != "ollama":
        return None
    return (local_cost_per_token(settings) * Decimal(max(total_tokens, 0))).quantize(Decimal("0.000001"))
