"""Founder-granted provider-spend budgets for billed planning — never inferred from repo-write authority.

See migration 0060. This module is the only writer of `ProviderSpendAuthorization` rows.
`provider_spend_is_live()` is the boolean `production_entry` will eventually call; it is
exported and tested here WITHOUT wiring that call site yet (Claude owns adjacent surfaces).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.execution_envelopes.service import get_current_execution_envelope
from app.models.execution_envelope import ExecutionAuthorizationEnvelope
from app.models.mainai_execution import MainAIGoal
from app.models.provider_spend import (
    ProviderSpendAuthorization,
    ProviderSpendAuthorizationStatus,
    ProviderSpendUsageEvent,
)


class ProviderSpendError(ValueError):
    pass


def _as_decimal(value: Decimal | float | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def authorize_provider_spend(
    db: Session,
    *,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    execution_envelope_id: uuid.UUID,
    authorized_by: str,
    max_cost_usd: Decimal | float | int | str,
    max_requests: int,
    idempotency_key: str,
    max_prompt_tokens: int | None = None,
    max_completion_tokens: int | None = None,
    allowed_providers: list[str] | None = None,
    allowed_models: list[str] | None = None,
    expires_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
) -> ProviderSpendAuthorization:
    """The ONLY path that creates an active provider-spend grant.

    Requires an explicit founder `authorized_by` and ceilings. The cited envelope must be the
    goal's CURRENT active execution envelope — granting spend against a stale/superseded
    envelope is rejected. A prior active spend grant for the same goal is superseded (never
    mutated). Idempotent on (owner_id, idempotency_key).
    """
    max_cost = _as_decimal(max_cost_usd)
    if max_cost < 0 or max_requests < 0:
        raise ProviderSpendError("ceilings must be non-negative")
    if max_prompt_tokens is not None and max_prompt_tokens < 0:
        raise ProviderSpendError("max_prompt_tokens must be non-negative")
    if max_completion_tokens is not None and max_completion_tokens < 0:
        raise ProviderSpendError("max_completion_tokens must be non-negative")

    goal = db.execute(
        select(MainAIGoal).where(MainAIGoal.id == goal_id, MainAIGoal.owner_id == owner_id)
    ).scalar_one_or_none()
    if goal is None:
        raise ProviderSpendError(f"goal_id={goal_id} does not belong to owner_id={owner_id}")

    current_envelope = get_current_execution_envelope(db, owner_id=owner_id, goal_id=goal_id)
    if current_envelope is None or current_envelope.id != execution_envelope_id:
        raise ProviderSpendError(
            "execution_envelope_id must be the goal's current active execution authorization envelope"
        )

    existing = db.execute(
        select(ProviderSpendAuthorization).where(
            ProviderSpendAuthorization.owner_id == owner_id,
            ProviderSpendAuthorization.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        same = (
            existing.goal_id == goal_id
            and existing.execution_envelope_id == execution_envelope_id
            and existing.max_cost_usd == max_cost
            and existing.max_requests == max_requests
            and existing.authorized_by == authorized_by
        )
        if not same:
            raise ProviderSpendError("idempotency key reused with different fields")
        return existing

    prior = db.execute(
        select(ProviderSpendAuthorization)
        .where(
            ProviderSpendAuthorization.owner_id == owner_id,
            ProviderSpendAuthorization.goal_id == goal_id,
            ProviderSpendAuthorization.status == ProviderSpendAuthorizationStatus.active.value,
        )
        .with_for_update()
    ).scalar_one_or_none()

    row = ProviderSpendAuthorization(
        owner_id=owner_id,
        goal_id=goal_id,
        execution_envelope_id=execution_envelope_id,
        authorized_by=authorized_by,
        max_cost_usd=max_cost,
        max_requests=max_requests,
        max_prompt_tokens=max_prompt_tokens,
        max_completion_tokens=max_completion_tokens,
        allowed_providers=list(allowed_providers or []),
        allowed_models=list(allowed_models or []),
        expires_at=expires_at,
        provenance=provenance or {},
        idempotency_key=idempotency_key,
        supersedes_authorization_id=prior.id if prior is not None else None,
    )
    db.add(row)
    db.flush()

    if prior is not None:
        prior.status = ProviderSpendAuthorizationStatus.superseded.value
        db.flush()
    return row


def revoke_provider_spend(
    db: Session, *, owner_id: uuid.UUID, authorization_id: uuid.UUID, reason: str
) -> ProviderSpendAuthorization:
    """Founder revoke — status becomes revoked; never deletes the audit row."""
    row = db.execute(
        select(ProviderSpendAuthorization)
        .where(
            ProviderSpendAuthorization.id == authorization_id,
            ProviderSpendAuthorization.owner_id == owner_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ProviderSpendError("provider spend authorization missing or belongs to another owner")
    if row.status != ProviderSpendAuthorizationStatus.active.value:
        raise ProviderSpendError(f"provider spend authorization is already {row.status}")
    row.status = ProviderSpendAuthorizationStatus.revoked.value
    row.provenance = {**(row.provenance or {}), "revoked_reason": reason, "revoked_at": datetime.utcnow().isoformat()}
    db.flush()
    return row


def _mark_terminal_if_needed(row: ProviderSpendAuthorization, *, now: datetime) -> None:
    if row.status != ProviderSpendAuthorizationStatus.active.value:
        return
    if row.expires_at is not None and row.expires_at <= now:
        row.status = ProviderSpendAuthorizationStatus.expired.value
        return
    if row.spent_requests >= row.max_requests or row.spent_cost_usd >= row.max_cost_usd:
        row.status = ProviderSpendAuthorizationStatus.exhausted.value
        return
    if row.max_prompt_tokens is not None and row.spent_prompt_tokens >= row.max_prompt_tokens:
        row.status = ProviderSpendAuthorizationStatus.exhausted.value
        return
    if row.max_completion_tokens is not None and row.spent_completion_tokens >= row.max_completion_tokens:
        row.status = ProviderSpendAuthorizationStatus.exhausted.value


def get_current_provider_spend_authorization(
    db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID
) -> ProviderSpendAuthorization | None:
    """Active spend grant for this goal under the CURRENT active execution envelope, or None.

    Fail closed when: no active row; expired/exhausted (marked); envelope missing or not the
    one cited on the grant (envelope re-auth without re-granting spend).
    """
    now = datetime.utcnow()
    row = db.execute(
        select(ProviderSpendAuthorization)
        .where(
            ProviderSpendAuthorization.owner_id == owner_id,
            ProviderSpendAuthorization.goal_id == goal_id,
            ProviderSpendAuthorization.status == ProviderSpendAuthorizationStatus.active.value,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        return None
    _mark_terminal_if_needed(row, now=now)
    if row.status != ProviderSpendAuthorizationStatus.active.value:
        db.flush()
        return None
    envelope = get_current_execution_envelope(db, owner_id=owner_id, goal_id=goal_id)
    if envelope is None or envelope.id != row.execution_envelope_id:
        return None
    return row


def provider_spend_is_live(
    db: Session,
    *,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    execution_envelope_id: uuid.UUID | None = None,
) -> bool:
    """The boolean `production_entry` should eventually pass into SupervisorScope.

    Optional `execution_envelope_id` must match the grant's cited envelope when provided
    (the production tick's freshly re-verified envelope).
    """
    row = get_current_provider_spend_authorization(db, owner_id=owner_id, goal_id=goal_id)
    if row is None:
        return False
    if execution_envelope_id is not None and row.execution_envelope_id != execution_envelope_id:
        return False
    return True


def _provider_allowed(row: ProviderSpendAuthorization, provider: str, model: str) -> bool:
    providers = list(row.allowed_providers or [])
    models = list(row.allowed_models or [])
    if providers and provider not in providers:
        return False
    if models and model not in models:
        return False
    return True


def record_provider_spend_usage(
    db: Session,
    *,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    source_ref: str,
    provider: str,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: Decimal | float | int | str = 0,
    task_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    evidence: dict[str, Any] | None = None,
) -> ProviderSpendUsageEvent | None:
    """Atomically check remaining ceilings under row lock, append usage, bump spent counters.

    Returns None when source_ref was already recorded (retry — never increases spend).
    Raises ProviderSpendError when no live grant, allowlist miss, or ceilings would be exceeded.
    """
    if prompt_tokens < 0 or completion_tokens < 0:
        raise ProviderSpendError("token counts must be non-negative")
    cost = _as_decimal(cost_usd)
    if cost < 0:
        raise ProviderSpendError("cost_usd must be non-negative")

    # Idempotent retry must short-circuit BEFORE live-grant / ceiling checks — otherwise a
    # replay after exhaustion would falsely fail closed instead of returning the recorded event.
    prior_event = db.execute(
        select(ProviderSpendUsageEvent).where(
            ProviderSpendUsageEvent.source_ref == source_ref,
            ProviderSpendUsageEvent.owner_id == owner_id,
        )
    ).scalar_one_or_none()
    if prior_event is not None:
        return prior_event

    row = get_current_provider_spend_authorization(db, owner_id=owner_id, goal_id=goal_id)
    if row is None:
        raise ProviderSpendError("no live provider spend authorization for this goal")

    if not _provider_allowed(row, provider, model):
        raise ProviderSpendError(f"provider/model not allowlisted on this spend grant: {provider}/{model}")

    # Ceiling check BEFORE insert — concurrent callers serialize on the FOR UPDATE above.
    if row.spent_requests + 1 > row.max_requests:
        row.status = ProviderSpendAuthorizationStatus.exhausted.value
        db.flush()
        raise ProviderSpendError("provider spend request ceiling exhausted")
    if row.spent_cost_usd + cost > row.max_cost_usd:
        row.status = ProviderSpendAuthorizationStatus.exhausted.value
        db.flush()
        raise ProviderSpendError("provider spend cost ceiling exhausted")
    if row.max_prompt_tokens is not None and row.spent_prompt_tokens + prompt_tokens > row.max_prompt_tokens:
        row.status = ProviderSpendAuthorizationStatus.exhausted.value
        db.flush()
        raise ProviderSpendError("provider spend prompt-token ceiling exhausted")
    if row.max_completion_tokens is not None and row.spent_completion_tokens + completion_tokens > row.max_completion_tokens:
        row.status = ProviderSpendAuthorizationStatus.exhausted.value
        db.flush()
        raise ProviderSpendError("provider spend completion-token ceiling exhausted")

    result = db.execute(
        pg_insert(ProviderSpendUsageEvent)
        .values(
            id=uuid.uuid4(),
            owner_id=owner_id,
            authorization_id=row.id,
            goal_id=goal_id,
            task_id=task_id,
            job_id=job_id,
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            evidence=evidence or {},
            source_ref=source_ref,
        )
        .on_conflict_do_nothing(constraint="uq_provider_spend_usage_events_source_ref")
        .returning(ProviderSpendUsageEvent.id)
    )
    inserted_id = result.scalar_one_or_none()
    if inserted_id is None:
        # Retry of the same source_ref — spend must not increase.
        return db.execute(
            select(ProviderSpendUsageEvent).where(ProviderSpendUsageEvent.source_ref == source_ref)
        ).scalar_one_or_none()

    row.spent_requests += 1
    row.spent_cost_usd = _as_decimal(row.spent_cost_usd) + cost
    row.spent_prompt_tokens += prompt_tokens
    row.spent_completion_tokens += completion_tokens
    _mark_terminal_if_needed(row, now=datetime.utcnow())
    db.flush()
    return db.get(ProviderSpendUsageEvent, inserted_id)
