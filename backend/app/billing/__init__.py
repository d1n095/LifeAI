"""Revenue & Compute Engine — the single accounting pipeline for LifeAI.

Modules:
* accounting   — record_usage()/refund_usage(): the ONLY writers of usage_log + usage_rollups
* enforcement  — concurrency-safe reserve/settle/release budgets (Redis atomic, fail-closed)
* limits       — effective per-subject caps: tier policy ← tenant overrides ← Settings
* tiers        — free (ad-funded) / paid / enterprise usage policies
* local_cost   — infrastructure cost of LOCAL models (never billed to users; platform view)
* allocation   — compute capacity from SETTLED revenue only, per-tenant isolated

Reuse rules (why this is not a parallel accounting system):
* app/providers/pricing.py stays the ONLY price table.
* app/openai_consult/budgets.py keeps its consultation-specific counters and its
  reserve/settle semantics; this engine reuses its integer-micro-USD primitives and
  fail-closed storage pattern rather than inventing a second style.
* usage_log (pre-existing table) stays the per-call audit record; the rollups are a
  materialization of the SAME events written by the SAME function — never a second truth.
* ProviderSpendAuthorization (founder-granted envelope budgets) remains the authorization
  layer for workforce planning; this engine layers platform-wide accounting UNDER it.

Monetary values: integer micro-USD everywhere (no float rounding in enforcement).
Fail-closed: with billing_enforcement_enabled and no working counter store, spending is
REFUSED — never admitted uncounted (mirrors app/openai_consult/budgets.py).
"""

from app.billing.accounting import record_usage, refund_usage
from app.billing.enforcement import (
    BillingBudgetExceededError,
    BillingEnforcementUnavailableError,
    ComputeBudgetStore,
)

__all__ = [
    "record_usage",
    "refund_usage",
    "BillingBudgetExceededError",
    "BillingEnforcementUnavailableError",
    "ComputeBudgetStore",
]
