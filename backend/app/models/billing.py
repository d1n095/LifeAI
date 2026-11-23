"""Revenue & Compute Engine models (migration 0070).

The accounting pipeline is deliberately SINGLE-TRACKED:

    provider call → app/billing/accounting.record_usage()  (the ONLY writer)
                        ├─ usage_log        per-call audit record (pre-existing table)
                        └─ usage_rollups    per-subject, per-period totals (enforcement input)

app/providers/pricing.py remains the ONLY price table. No other module may compute or
persist costs — see docs/REVENUE_COMPUTE_ENGINE.md.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class BillingTier(str, enum.Enum):
    free = "free"
    paid = "paid"
    enterprise = "enterprise"


class BillingTenant(Base):
    """A billing subject that groups users (enterprise / account-family). LifeAI today is
    effectively a single-owner deployment; this table exists so per-tenant accounting,
    caps and compute allocation are structurally isolated per subject — never shared."""

    __tablename__ = "billing_tenants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(128))
    tier: Mapped[BillingTier] = mapped_column(String(16), default=BillingTier.paid)
    # NULL caps = use the tier policy defaults from app/billing/tiers.py.
    daily_cost_cap_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    monthly_cost_cap_micros: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    safety_margin_pct: Mapped[int] = mapped_column(Integer, default=25)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UsageRollup(Base):
    """Per-subject, per-period materialized usage totals. Integer micro-USD so neither the
    DB layer nor enforcement ever sees float rounding. Written ONLY by
    accounting.record_usage() as a SQL-side atomic increment (no read-modify-write race)."""

    __tablename__ = "usage_rollups"
    __table_args__ = (
        UniqueConstraint("subject_type", "subject_id", "period_type", "period_start",
                         name="uq_usage_rollups_subject_period"),
        # Parity with migration 0070: balances can NEVER go negative, even if a future
        # code path bypasses the application-level refund ceilings.
        CheckConstraint("tokens_in >= 0", name="ck_usage_rollups_tokens_in_nonneg"),
        CheckConstraint("tokens_out >= 0", name="ck_usage_rollups_tokens_out_nonneg"),
        CheckConstraint("cost_micros >= 0", name="ck_usage_rollups_cost_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_type: Mapped[str] = mapped_column(String(8))  # "user" | "tenant"
    subject_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    period_type: Mapped[str] = mapped_column(String(8))  # "day" | "month"
    period_start: Mapped[date] = mapped_column(Date)
    tokens_in: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, default=0)
    # Non-negative by DB constraint: refunds decrement via the SAME atomic increment with
    # a checked ceiling (accounting.refund_usage refuses over-refunds before writing).
    cost_micros: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RevenueStatus(str, enum.Enum):
    estimated = "estimated"  # unverified (e.g. ad-network-reported) — NEVER spendable
    settled = "settled"      # verified settlement reference present — allocatable


class RevenueSource(str, enum.Enum):
    subscription = "subscription"
    ad_revenue = "ad_revenue"
    grant = "grant"
    manual = "manual"


class RevenueLedger(Base):
    """Append-only revenue entries. The estimated/settled distinction is the core
    anti-risk control: an advertiser's self-reported numbers are 'estimated' and count
    for NOTHING until a settlement reference lands. DB CHECK: settled rows MUST carry a
    verified_ref. Platform rows (subject_id NULL) fund the shared envelope; tenant rows
    fund ONLY that tenant (see allocation.py)."""

    __tablename__ = "revenue_ledger"
    __table_args__ = (
        # Parity with migration 0070: settled revenue REQUIRES a verified reference —
        # settlement cannot happen by fiat (financial invariant, P3 adversarial-tested).
        CheckConstraint("status = 'estimated' OR verified_ref IS NOT NULL",
                        name="ck_revenue_settled_requires_ref"),
        CheckConstraint("subject_type = 'platform' OR subject_id IS NOT NULL",
                        name="ck_revenue_platform_rows_have_no_subject"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    subject_type: Mapped[str] = mapped_column(String(8))  # "tenant" | "platform"
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source: Mapped[RevenueSource] = mapped_column(String(32))
    status: Mapped[RevenueStatus] = mapped_column(String(16))
    amount_micros: Mapped[int] = mapped_column(BigInteger)
    period_start: Mapped[date] = mapped_column(Date)
    verified_ref: Mapped[str | None] = mapped_column(String(256), nullable=True)
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
