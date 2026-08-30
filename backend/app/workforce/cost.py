"""Workforce cost governance (T16). Reuses conceptual ceilings; real spend via provider_spend.

Cost scoring must NEVER override security or authority requirements.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.workforce_ops import WorkforceCostBudget


class CostGovernanceError(Exception):
    pass


SCOPE_KINDS = frozenset({"assignment", "agent", "team", "goal", "period", "provider"})


def set_cost_budget(
    db: Session,
    *,
    owner_id: uuid.UUID,
    scope_kind: str,
    scope_ref: str,
    cap_usd: float,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
) -> WorkforceCostBudget:
    if scope_kind not in SCOPE_KINDS:
        raise CostGovernanceError(f"invalid scope_kind: {scope_kind}")
    if cap_usd < 0:
        raise CostGovernanceError("cap_usd must be >= 0")
    existing = db.execute(
        select(WorkforceCostBudget).where(
            WorkforceCostBudget.owner_id == owner_id,
            WorkforceCostBudget.scope_kind == scope_kind,
            WorkforceCostBudget.scope_ref == scope_ref,
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing.cap_usd = float(cap_usd)
        existing.period_start = period_start
        existing.period_end = period_end
        existing.status = "active"
        existing.updated_at = datetime.utcnow()
        db.flush()
        return existing
    row = WorkforceCostBudget(
        owner_id=owner_id,
        scope_kind=scope_kind,
        scope_ref=scope_ref,
        cap_usd=float(cap_usd),
        period_start=period_start,
        period_end=period_end,
        status="active",
    )
    db.add(row)
    db.flush()
    return row


def budget_remaining(budget: WorkforceCostBudget) -> float:
    return float(budget.cap_usd) - float(budget.spent_usd) - float(budget.reserved_usd)


def budget_is_live(budget: WorkforceCostBudget, *, now: datetime | None = None) -> bool:
    now = now or datetime.utcnow()
    if budget.status != "active":
        return False
    if budget.period_end is not None and budget.period_end <= now:
        return False
    if budget_remaining(budget) <= 0:
        return False
    return True


def reserve_against_budget(
    db: Session,
    *,
    owner_id: uuid.UUID,
    scope_kind: str,
    scope_ref: str,
    amount_usd: float,
) -> WorkforceCostBudget:
    if amount_usd < 0:
        raise CostGovernanceError("amount must be >= 0")
    budget = db.execute(
        select(WorkforceCostBudget).where(
            WorkforceCostBudget.owner_id == owner_id,
            WorkforceCostBudget.scope_kind == scope_kind,
            WorkforceCostBudget.scope_ref == scope_ref,
        )
    ).scalar_one_or_none()
    if budget is None:
        raise CostGovernanceError(f"no budget for {scope_kind}:{scope_ref}")
    if not budget_is_live(budget):
        budget.status = "exhausted" if budget_remaining(budget) <= 0 else budget.status
        db.flush()
        raise CostGovernanceError("budget not live / exhausted")
    if budget_remaining(budget) < amount_usd:
        raise CostGovernanceError("insufficient budget remaining")
    budget.reserved_usd = float(budget.reserved_usd) + float(amount_usd)
    budget.updated_at = datetime.utcnow()
    db.flush()
    return budget


def settle_budget_reservation(
    db: Session,
    *,
    owner_id: uuid.UUID,
    scope_kind: str,
    scope_ref: str,
    reserved_usd: float,
    actual_usd: float,
) -> WorkforceCostBudget:
    budget = db.execute(
        select(WorkforceCostBudget).where(
            WorkforceCostBudget.owner_id == owner_id,
            WorkforceCostBudget.scope_kind == scope_kind,
            WorkforceCostBudget.scope_ref == scope_ref,
        )
    ).scalar_one_or_none()
    if budget is None:
        raise CostGovernanceError("budget missing at settle")
    budget.reserved_usd = max(0.0, float(budget.reserved_usd) - float(reserved_usd))
    budget.spent_usd = float(budget.spent_usd) + max(0.0, float(actual_usd))
    if budget_remaining(budget) <= 0:
        budget.status = "exhausted"
    budget.updated_at = datetime.utcnow()
    db.flush()
    return budget


def release_budget_reservation(
    db: Session,
    *,
    owner_id: uuid.UUID,
    scope_kind: str,
    scope_ref: str,
    amount_usd: float,
) -> WorkforceCostBudget:
    """Release only when non-effect is proven — caller responsibility (Window B)."""
    budget = db.execute(
        select(WorkforceCostBudget).where(
            WorkforceCostBudget.owner_id == owner_id,
            WorkforceCostBudget.scope_kind == scope_kind,
            WorkforceCostBudget.scope_ref == scope_ref,
        )
    ).scalar_one_or_none()
    if budget is None:
        raise CostGovernanceError("budget missing at release")
    budget.reserved_usd = max(0.0, float(budget.reserved_usd) - float(amount_usd))
    budget.updated_at = datetime.utcnow()
    db.flush()
    return budget


def assert_scopes_allow_spend(
    db: Session,
    *,
    owner_id: uuid.UUID,
    scopes: list[tuple[str, str]],
    amount_usd: float,
) -> None:
    """All listed scopes must have remaining room. Security/authority checks happen elsewhere."""
    for kind, ref in scopes:
        budget = db.execute(
            select(WorkforceCostBudget).where(
                WorkforceCostBudget.owner_id == owner_id,
                WorkforceCostBudget.scope_kind == kind,
                WorkforceCostBudget.scope_ref == ref,
            )
        ).scalar_one_or_none()
        if budget is None:
            continue  # unset scope = no org ceiling (provider_spend may still bind)
        if not budget_is_live(budget) or budget_remaining(budget) < amount_usd:
            raise CostGovernanceError(f"cost gate blocked: {kind}:{ref}")
