"""Revenue-backed compute allocation (P3): how much compute may be spent, by whom.

Core rules, each structurally enforced:

1. ESTIMATED REVENUE NEVER FUNDS SPENDING. Only revenue_ledger rows with
   status='settled' (which the DB CHECK ties to a verified_ref) are summed. An
   advertiser's or ad network's self-reported numbers enter the ledger as 'estimated'
   and are invisible to allocation until a settlement reference lands.
2. CONSERVATIVE MARGINS. Allocatable = settled × (1 − safety_margin_pct). The margin
   defaults to 25% (Settings) and per-tenant margins live on the tenant row. Capacity is
   reserved for volatility (refunds, provider price drift, collection risk).
3. TENANT ISOLATION. A tenant's envelope is computed ONLY from that tenant's settled
   revenue plus the PLATFORM envelope (operator grants, subject_id NULL — explicitly
   platform-scoped funding). No query ever joins across subject ids; tenant A's revenue
   can never raise tenant B's ceiling. Leakage is structurally impossible, not policed.
4. ADVERTISEMENTS CANNOT OVERRIDE AUTHORIZATION OR SECURITY. This module has no import
   path into app.ads and never reads ad tables; ad revenue enters the SAME ledger as any
   other revenue and is subject to the SAME estimated/settled rule. Authorization
   (execution envelopes, provider spend, kill switch) is untouched by allocation —
   allocation only ever LOWERS spending ceilings.
5. BOOTSTRAP RULE. A subject with NO revenue rows for the period is governed by its tier
   caps alone (the founder-funded bootstrap state); allocation BINDS (lowering ceilings)
   only when revenue rows actually exist for that period. This keeps the pre-revenue
   platform usable without pretending revenue exists.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.billing import BillingTenant, RevenueLedger, RevenueStatus


@dataclass(frozen=True)
class AllocationEnvelope:
    subject_type: str          # "user" | "tenant" | "platform"
    subject_id: uuid.UUID | None
    period_start: date
    settled_revenue_micros: int
    estimated_revenue_micros: int  # reported for transparency ONLY — never added to the budget
    safety_margin_pct: int
    allocatable_micros: int    # the ONLY number any spending ceiling may use
    bound: bool                # False = bootstrap (tier caps govern), True = revenue binds


def _month_start(now: datetime) -> date:
    return date(now.year, now.month, 1)


def _settled_and_estimated(
    db: Session, *, subject_type: str, subject_id: uuid.UUID | None, period: date
) -> tuple[int, int]:
    settled_q = (
        select(RevenueLedger.amount_micros)
        .where(
            RevenueLedger.subject_type == subject_type,
            RevenueLedger.period_start == period,
            RevenueLedger.status == RevenueStatus.settled.value,
        )
    )
    estimated_q = (
        select(RevenueLedger.amount_micros)
        .where(
            RevenueLedger.subject_type == subject_type,
            RevenueLedger.period_start == period,
            RevenueLedger.status == RevenueStatus.estimated.value,
        )
    )
    if subject_id is not None:
        settled_q = settled_q.where(RevenueLedger.subject_id == subject_id)
        estimated_q = estimated_q.where(RevenueLedger.subject_id == subject_id)
    else:
        settled_q = settled_q.where(RevenueLedger.subject_id.is_(None))
        estimated_q = estimated_q.where(RevenueLedger.subject_id.is_(None))
    settled = sum(int(v) for v in db.execute(settled_q).scalars())
    estimated = sum(int(v) for v in db.execute(estimated_q).scalars())
    return settled, estimated


def _apply_margin(micros: int, margin_pct: int) -> int:
    margin_pct = max(0, min(int(margin_pct), 90))
    return int(micros * (100 - margin_pct) // 100)


def platform_envelope(db: Session, *, now: datetime | None = None, settings=None) -> AllocationEnvelope:
    """The shared, operator-granted envelope (revenue_ledger rows with subject_type
    'platform'). Funds bootstrap capacity for every subject, conservatively margined."""
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    period = _month_start(now)
    settled, estimated = _settled_and_estimated(db, subject_type="platform", subject_id=None, period=period)
    margin = settings.billing_safety_margin_pct
    return AllocationEnvelope(
        subject_type="platform", subject_id=None, period_start=period,
        settled_revenue_micros=settled, estimated_revenue_micros=estimated,
        safety_margin_pct=margin, allocatable_micros=_apply_margin(settled, margin),
        bound=settled > 0,
    )


def tenant_envelope(db: Session, tenant: BillingTenant, *, now: datetime | None = None) -> AllocationEnvelope:
    """One tenant's OWN envelope: that tenant's settled revenue, margined by the TENANT's
    configured safety margin. Platform grants are added by caller composition (see
    subject_envelope) — never silently — and never from another tenant's revenue."""
    now = now or datetime.now(timezone.utc)
    period = _month_start(now)
    settled, estimated = _settled_and_estimated(
        db, subject_type="tenant", subject_id=tenant.id, period=period
    )
    return AllocationEnvelope(
        subject_type="tenant", subject_id=tenant.id, period_start=period,
        settled_revenue_micros=settled, estimated_revenue_micros=estimated,
        safety_margin_pct=tenant.safety_margin_pct,
        allocatable_micros=_apply_margin(settled, tenant.safety_margin_pct),
        bound=settled > 0,
    )


def subject_envelope(db: Session, *, tenant_id: uuid.UUID | None,
                     now: datetime | None = None, settings=None) -> AllocationEnvelope:
    """The envelope that governs a subject's monthly compute ceiling: the tenant's own
    settled revenue when the subject belongs to a tenant, else the platform envelope.
    Composition rule: tenant envelope binds when it has revenue rows; otherwise the
    platform envelope binds when IT has rows; otherwise bootstrap (tier caps govern)."""
    settings = settings or get_settings()
    now = now or datetime.now(timezone.utc)
    if tenant_id is not None:
        tenant = db.execute(select(BillingTenant).where(BillingTenant.id == tenant_id)).scalar_one_or_none()
        if tenant is not None:
            own = tenant_envelope(db, tenant, now=now)
            if own.bound:
                return own
    return platform_envelope(db, now=now, settings=settings)
