"""Program + AutonomyLevel + BudgetEnvelope service functions (Milestone 1)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.dev_director.types import (
    LEVELS_REQUIRING_EXPLICIT_FOUNDER_AUTHORIZATION,
    AutonomyLevel,
    AutonomyLevelRequiresFounderAuthorizationError,
    BudgetEnvelope,
    BudgetExceededError,
    DEFAULT_AUTONOMY_LEVEL,
    Program,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_program(
    *, owner_id: uuid.UUID, repo_identity: str, goal: str, priority: str = "medium",
    risk_class: str = "low", total_budget_ceiling_usd: float = 0.0,
) -> Program:
    now = _utcnow()
    program_id = uuid.uuid4()
    return Program(
        program_id=program_id, owner_id=owner_id, repo_identity=repo_identity, goal=goal,
        priority=priority, risk_class=risk_class, autonomy_level=DEFAULT_AUTONOMY_LEVEL,
        budget_envelope=BudgetEnvelope(program_id=program_id, total_ceiling_usd=total_budget_ceiling_usd),
        created_at=now, updated_at=now,
    )


def set_autonomy_level(program: Program, *, level: AutonomyLevel, founder_authorization_ref: str | None = None) -> Program:
    """AUTONOMY LEVEL != AUTHORITY TOKEN: setting this field never itself grants anything.
    Levels 3/4 are structurally impossible to set without a real, non-empty
    founder_authorization_ref -- this is the ONLY function that may set `autonomy_level`,
    and it enforces this every time, not just at Program construction."""
    if level in LEVELS_REQUIRING_EXPLICIT_FOUNDER_AUTHORIZATION:
        if not founder_authorization_ref or not founder_authorization_ref.strip():
            raise AutonomyLevelRequiresFounderAuthorizationError(
                f"{level.value} requires a real, non-empty founder_authorization_ref -- AUTONOMY LEVEL != AUTHORITY TOKEN"
            )
        program.founder_authorization_ref = founder_authorization_ref
    program.autonomy_level = level
    program.updated_at = _utcnow()
    return program


# --- Budget envelope: Program-local bookkeeping only. See types.py's BudgetEnvelope
# docstring -- explicitly NOT unified with app.provider_spend/app.workforce.cost. ----------


def reserve_from_budget(envelope: BudgetEnvelope, *, amount_usd: float, provider_identity: str | None = None) -> BudgetEnvelope:
    if amount_usd < 0:
        raise BudgetExceededError("cannot reserve a negative amount")
    projected = envelope.consumed_usd + envelope.reserved_usd + amount_usd
    if projected > envelope.total_ceiling_usd:
        raise BudgetExceededError(
            f"reserving {amount_usd} would bring committed spend to {projected}, exceeding ceiling {envelope.total_ceiling_usd}"
        )
    if provider_identity is not None:
        per_provider_ceiling = envelope.per_provider_ceiling_usd.get(provider_identity)
        if per_provider_ceiling is not None:
            provider_projected = envelope.per_provider_consumed_usd.get(provider_identity, 0.0) + amount_usd
            if provider_projected > per_provider_ceiling:
                raise BudgetExceededError(
                    f"reserving {amount_usd} for {provider_identity!r} would exceed its own ceiling {per_provider_ceiling}"
                )
    envelope.reserved_usd += amount_usd
    return envelope


def release_budget_reservation(envelope: BudgetEnvelope, *, amount_usd: float) -> BudgetEnvelope:
    envelope.reserved_usd = max(0.0, envelope.reserved_usd - amount_usd)
    return envelope


def record_budget_consumption(envelope: BudgetEnvelope, *, amount_usd: float, provider_identity: str | None = None) -> BudgetEnvelope:
    """Moves a reservation into real consumption -- releases the matching reservation amount
    and records actual spend. Fail-closed: cannot consume more than was reserved for this call."""
    if amount_usd > envelope.reserved_usd:
        raise BudgetExceededError(f"cannot consume {amount_usd}, only {envelope.reserved_usd} is currently reserved")
    envelope.reserved_usd -= amount_usd
    envelope.consumed_usd += amount_usd
    if provider_identity is not None:
        envelope.per_provider_consumed_usd[provider_identity] = envelope.per_provider_consumed_usd.get(provider_identity, 0.0) + amount_usd
    return envelope
