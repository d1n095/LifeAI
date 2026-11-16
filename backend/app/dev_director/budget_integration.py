"""Real budget integration contract (spend/cost authority reconciliation follow-up).

See docs/mainai_v2/MAINAI_V2_SPEND_AUTHORITY_RECONCILIATION.md. TWO LEDGERS != TWO SOURCES
OF TRUTH: this module never becomes a THIRD parallel spend ledger. It composes two REAL
systems -- app.provider_spend (canonical actual spend, goal+envelope scoped) and
app.workforce.cost (optional organizational ceilings, now wired as a real check inside
app.provider_spend.service.reserve_provider_spend_call() itself) -- with `Program`'s own
`BudgetEnvelope` (Program-local bookkeeping/visibility, see program.py), never duplicating
either real system's own durable state.

DELIBERATE EXCEPTION to this V2 lane's package-independence discipline. Every other module
in app.dev_director (Part 1/2) deliberately referenced real production types by NAME/STRING
VALUE only, never by import -- this is the first module in this package to actually import
real production code. That is a deliberate, in-scope extension of an exception already
reserved for this package (see its own __init__.py docstring: "a new coordination tier
explicitly designed to compose with real infrastructure, not a sixth sibling requiring
mutual independence from production code"), and mirrors the SAME real-import precedent
`app.operating_shell.canonical_projection` already established in the Intent/Goal
reconciliation round for an analogous reason -- composing with, never duplicating, real
canonical state. This module DOES import real, pre-existing production code
(`app.provider_spend`, `app.workforce.cost`, `app.models.workforce_ops.WorkforceCostBudget`).
The eight-sibling independence rule never applied to real production code, and this module
is still not imported by app.main/any router/the executive loop -- read/write composition
from new, unreachable code, never new production wiring on its own.

COST RECORD != SPEND AUTHORITY: recording a settled cost here never grants future spend
authority -- `remaining_budget()` always re-derives headroom from the REAL current state of
all three ledgers, never from a cached/assumed value.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.workforce_ops import WorkforceCostBudget
from app.provider_spend import ProviderSpendError
from app.provider_spend.service import (
    get_current_provider_spend_authorization,
    provider_spend_is_live,
    release_provider_spend_call,
    reserve_provider_spend_call,
    settle_provider_spend_call,
)
from app.workforce.cost import budget_is_live, budget_remaining

from app.dev_director.program import (
    release_budget_reservation,
    reserve_from_budget,
)
from app.dev_director.types import BudgetEnvelope, BudgetExceededError


class BudgetIntegrationError(ValueError):
    """Raised when the composed real+Program-local budget check refuses a reservation."""


def _workforce_cost_budget(db: Session, *, owner_id: uuid.UUID, scope_kind: str, scope_ref: str) -> WorkforceCostBudget | None:
    """Read-only lookup -- no public getter is exported from app.workforce.cost, so this
    mirrors the exact same query assert_scopes_allow_spend() itself uses internally, never
    writing, never bypassing that module's own real gating logic (which
    reserve_provider_spend_call() already calls for real, see provider_spend/service.py)."""
    return db.execute(
        select(WorkforceCostBudget).where(
            WorkforceCostBudget.owner_id == owner_id,
            WorkforceCostBudget.scope_kind == scope_kind,
            WorkforceCostBudget.scope_ref == scope_ref,
        )
    ).scalar_one_or_none()


def can_reserve_budget(
    db: Session, *, envelope: BudgetEnvelope, owner_id: uuid.UUID, goal_id: uuid.UUID, provider: str, estimated_amount_usd: float
) -> bool:
    """A non-mutating dry-run answering "can I afford this job?" against all three real
    ledgers this reconciliation composes -- never reserves anything itself. BUDGET !=
    EXECUTION AUTHORITY: a True result here is advisory only; the real gate is always
    reserve_budget()'s own call into the REAL reserve_provider_spend_call()."""
    if envelope.consumed_usd + envelope.reserved_usd + estimated_amount_usd > envelope.total_ceiling_usd:
        return False
    if not provider_spend_is_live(db, owner_id=owner_id, goal_id=goal_id):
        return False
    for scope_kind, scope_ref in (("goal", str(goal_id)), ("provider", provider)):
        budget = _workforce_cost_budget(db, owner_id=owner_id, scope_kind=scope_kind, scope_ref=scope_ref)
        if budget is not None and (not budget_is_live(budget) or budget_remaining(budget) < estimated_amount_usd):
            return False
    return True


def reserve_budget(
    db: Session,
    *,
    envelope: BudgetEnvelope,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    source_ref: str,
    provider: str,
    model: str,
    task_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> tuple[Any, BudgetEnvelope]:
    """The REAL reservation -- calls the REAL reserve_provider_spend_call(), which (after
    this reconciliation's fix) already checks app.workforce.cost's own organizational
    ceilings internally. On success, mirrors the SAME committed amount into the Program's
    own BudgetEnvelope for local visibility/reporting -- the envelope is never itself the
    gate for the real provider call, only a derived view of it."""
    try:
        event, created = reserve_provider_spend_call(
            db, owner_id=owner_id, goal_id=goal_id, source_ref=source_ref, provider=provider, model=model, task_id=task_id, job_id=job_id,
        )
    except ProviderSpendError as exc:
        raise BudgetIntegrationError(f"real provider-spend reservation refused: {exc}") from exc

    reserved_amount = float(event.reserved_cost_usd)
    if created:
        try:
            reserve_from_budget(envelope, amount_usd=reserved_amount, provider_identity=provider)
        except BudgetExceededError as exc:
            # The Program-local envelope disagrees with the real system it mirrors -- this
            # is a real, reportable inconsistency (the Program's own ceiling is tighter than
            # what the real systems just allowed), never silently ignored. Release the REAL
            # reservation we just took, so the two ledgers cannot diverge.
            release_provider_spend_call(db, owner_id=owner_id, source_ref=source_ref)
            raise BudgetIntegrationError(
                f"real reservation succeeded but exceeds this Program's own local budget envelope: {exc}"
            ) from exc
    return event, envelope


def settle_job_cost(
    db: Session,
    *,
    envelope: BudgetEnvelope,
    owner_id: uuid.UUID,
    source_ref: str,
    provider: str,
    reserved_amount_usd: float,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    actual_cost_usd: Decimal | float | int | str = 0,
) -> tuple[Any, BudgetEnvelope]:
    """MODEL-REPORTED COST != CANONICAL COST, PROVIDER BILLING CLAIM != VERIFIED SPEND: this
    function trusts `actual_cost_usd` exactly as much as the REAL settle_provider_spend_call()
    already does (no independent verification exists anywhere in this codebase yet -- a real,
    flagged, pre-existing limitation, see the reconciliation doc's own honest note; this
    module does not pretend to close that gap).

    `reserved_amount_usd` is the ORIGINAL reservation amount from this call's own earlier
    reserve_budget() (event.reserved_cost_usd at reservation time) -- required explicitly
    because the real provider_spend system's own settle_provider_spend_usage() fully clears
    a reservation on settle regardless of whether the actual settled cost is less than what
    was held (confirmed by direct testing: reserving $1.00 then settling $0.25 leaves
    reserved_cost_usd at $0.00, not $0.75) -- settling for less than the full reservation is
    a completely normal, expected case (an estimate is rarely exact), not an edge case. This
    function mirrors that exact real behavior in the Program-local envelope: the FULL
    original reservation is released, and ONLY the real actual amount is recorded as
    consumed -- never conflating "what we set aside" with "what we actually spent"."""
    event = settle_provider_spend_call(
        db, owner_id=owner_id, source_ref=source_ref, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, cost_usd=actual_cost_usd,
    )
    actual = float(event.cost_usd)
    # Release the full original hold, then record only the real actual amount as consumed --
    # composes the two existing, already-tested Part-1 primitives rather than modifying
    # either of their own established single-amount contracts.
    release_budget_reservation(envelope, amount_usd=reserved_amount_usd)
    envelope.consumed_usd += actual
    if provider is not None:
        envelope.per_provider_consumed_usd[provider] = envelope.per_provider_consumed_usd.get(provider, 0.0) + actual
    return event, envelope


def release_unused_reservation(db: Session, *, envelope: BudgetEnvelope, owner_id: uuid.UUID, source_ref: str, reserved_amount_usd: float) -> tuple[Any, BudgetEnvelope]:
    """PROVIDER FAILURE != AUTHORITY WIDENING: releases the REAL reservation and the matching
    Program-local hold -- never converts a failed attempt into recorded spend on either
    ledger (see the reconciliation doc's own flagged "failed_attempt_cost" limitation: a
    provider that genuinely incurred partial cost before failing still records zero here,
    matching release_provider_spend_call()'s own existing, unchanged behavior)."""
    event = release_provider_spend_call(db, owner_id=owner_id, source_ref=source_ref)
    release_budget_reservation(envelope, amount_usd=reserved_amount_usd)
    return event, envelope


def remaining_budget(db: Session, *, envelope: BudgetEnvelope, owner_id: uuid.UUID, goal_id: uuid.UUID, provider: str | None = None) -> float:
    """ONE deterministic answer to "how much budget remains?" -- the minimum of all three
    real/local ceilings this reconciliation composes, never a single ledger's own view in
    isolation. COST RECORD != SPEND AUTHORITY: always re-derived from current state, never
    cached."""
    candidates = [envelope.total_ceiling_usd - envelope.consumed_usd - envelope.reserved_usd]
    auth = get_current_provider_spend_authorization(db, owner_id=owner_id, goal_id=goal_id)
    if auth is not None:
        candidates.append(float(auth.max_cost_usd) - float(auth.spent_cost_usd) - float(auth.reserved_cost_usd))
    for scope_kind, scope_ref in (("goal", str(goal_id)), ("provider", provider) if provider else (None, None)):
        if scope_kind is None:
            continue
        budget = _workforce_cost_budget(db, owner_id=owner_id, scope_kind=scope_kind, scope_ref=scope_ref)
        if budget is not None:
            candidates.append(budget_remaining(budget))
    return max(0.0, min(candidates))
