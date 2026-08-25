"""Founder-granted provider-spend budgets for billed planning — never inferred from repo-write authority.

See migration 0060. Call boundary:

    reserve_provider_spend_call  (BEFORE adapter/provider invocation)
    → provider invocation
    → settle_provider_spend_call  (actuals)  OR  release_provider_spend_call (failure)

`provider_spend_is_live()` remains the boolean production_entry will eventually call; it does
NOT authorize a call by itself — every billed call must reserve first.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.execution_envelopes.service import get_current_execution_envelope
from app.models.mainai_execution import MainAIGoal
from app.models.provider_spend import (
    ProviderSpendAuthorization,
    ProviderSpendAuthorizationStatus,
    ProviderSpendUsageEvent,
    ProviderSpendUsageStatus,
)
from app.providers.pricing import estimate_cost

# Conservative defaults when grant omits per-call token ceilings but still requires a
# pre-call hold. Callers with unknown pricing must set max_cost_per_request_usd.
_DEFAULT_RESERVE_PROMPT_TOKENS = 8_000
_DEFAULT_RESERVE_COMPLETION_TOKENS = 4_000


class ProviderSpendError(ValueError):
    pass


def _as_decimal(value: Decimal | float | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _norm_list(values: list[str] | None) -> list[str]:
    return sorted({str(v) for v in (values or [])})


def _authority_fingerprint(
    *,
    goal_id: uuid.UUID,
    execution_envelope_id: uuid.UUID,
    authorized_by: str,
    max_cost_usd: Decimal,
    max_requests: int,
    max_prompt_tokens: int | None,
    max_completion_tokens: int | None,
    max_cost_per_request_usd: Decimal | None,
    allowed_providers: list[str],
    allowed_models: list[str],
    expires_at: datetime | None,
) -> dict[str, Any]:
    def _money(value: Decimal | None) -> str | None:
        if value is None:
            return None
        return str(_as_decimal(value).quantize(Decimal("0.000001")))

    return {
        "goal_id": str(goal_id),
        "execution_envelope_id": str(execution_envelope_id),
        "authorized_by": authorized_by,
        "max_cost_usd": _money(max_cost_usd),
        "max_requests": max_requests,
        "max_prompt_tokens": max_prompt_tokens,
        "max_completion_tokens": max_completion_tokens,
        "max_cost_per_request_usd": _money(max_cost_per_request_usd),
        "allowed_providers": allowed_providers,
        "allowed_models": allowed_models,
        "expires_at": expires_at.replace(microsecond=0).isoformat() if expires_at is not None else None,
    }


def _row_authority_fingerprint(row: ProviderSpendAuthorization) -> dict[str, Any]:
    return _authority_fingerprint(
        goal_id=row.goal_id,
        execution_envelope_id=row.execution_envelope_id,
        authorized_by=row.authorized_by,
        max_cost_usd=_as_decimal(row.max_cost_usd),
        max_requests=row.max_requests,
        max_prompt_tokens=row.max_prompt_tokens,
        max_completion_tokens=row.max_completion_tokens,
        max_cost_per_request_usd=(
            _as_decimal(row.max_cost_per_request_usd) if row.max_cost_per_request_usd is not None else None
        ),
        allowed_providers=_norm_list(list(row.allowed_providers or [])),
        allowed_models=_norm_list(list(row.allowed_models or [])),
        expires_at=row.expires_at,
    )


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
    max_cost_per_request_usd: Decimal | float | int | str | None = None,
    allowed_providers: list[str] | None = None,
    allowed_models: list[str] | None = None,
    expires_at: datetime | None = None,
    provenance: dict[str, Any] | None = None,
) -> ProviderSpendAuthorization:
    """The ONLY path that creates an active provider-spend grant."""
    max_cost = _as_decimal(max_cost_usd)
    per_request = (
        _as_decimal(max_cost_per_request_usd) if max_cost_per_request_usd is not None else None
    )
    if max_cost < 0 or max_requests < 0:
        raise ProviderSpendError("ceilings must be non-negative")
    if max_prompt_tokens is not None and max_prompt_tokens < 0:
        raise ProviderSpendError("max_prompt_tokens must be non-negative")
    if max_completion_tokens is not None and max_completion_tokens < 0:
        raise ProviderSpendError("max_completion_tokens must be non-negative")
    if per_request is not None and per_request < 0:
        raise ProviderSpendError("max_cost_per_request_usd must be non-negative")

    providers = _norm_list(allowed_providers)
    models = _norm_list(allowed_models)
    requested = _authority_fingerprint(
        goal_id=goal_id,
        execution_envelope_id=execution_envelope_id,
        authorized_by=authorized_by,
        max_cost_usd=max_cost,
        max_requests=max_requests,
        max_prompt_tokens=max_prompt_tokens,
        max_completion_tokens=max_completion_tokens,
        max_cost_per_request_usd=per_request,
        allowed_providers=providers,
        allowed_models=models,
        expires_at=expires_at,
    )

    # Lock the goal row so concurrent first grants serialize before the partial unique index.
    goal = db.execute(
        select(MainAIGoal)
        .where(MainAIGoal.id == goal_id, MainAIGoal.owner_id == owner_id)
        .with_for_update()
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
        if _row_authority_fingerprint(existing) != requested:
            raise ProviderSpendError("idempotency key reused with different authority fields")
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

    # Supersede BEFORE inserting the new active row — partial unique index enforces one active.
    prior_id = prior.id if prior is not None else None
    if prior is not None:
        prior.status = ProviderSpendAuthorizationStatus.superseded.value
        db.flush()

    row = ProviderSpendAuthorization(
        owner_id=owner_id,
        goal_id=goal_id,
        execution_envelope_id=execution_envelope_id,
        authorized_by=authorized_by,
        max_cost_usd=max_cost,
        max_requests=max_requests,
        max_prompt_tokens=max_prompt_tokens,
        max_completion_tokens=max_completion_tokens,
        max_cost_per_request_usd=per_request,
        allowed_providers=providers,
        allowed_models=models,
        expires_at=expires_at,
        provenance={**(provenance or {}), "authority_fingerprint": requested},
        idempotency_key=idempotency_key,
        supersedes_authorization_id=prior_id,
    )
    db.add(row)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        raise ProviderSpendError(
            "concurrent active provider spend grant already exists for this owner+goal"
        ) from exc
    return row


def revoke_provider_spend(
    db: Session, *, owner_id: uuid.UUID, authorization_id: uuid.UUID, reason: str
) -> ProviderSpendAuthorization:
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
    row.provenance = {
        **(row.provenance or {}),
        "revoked_reason": reason,
        "revoked_at": datetime.utcnow().isoformat(),
    }
    db.flush()
    return row


def _committed_requests(row: ProviderSpendAuthorization) -> int:
    return int(row.spent_requests) + int(row.reserved_requests)


def _committed_cost(row: ProviderSpendAuthorization) -> Decimal:
    return _as_decimal(row.spent_cost_usd) + _as_decimal(row.reserved_cost_usd)


def _committed_prompt(row: ProviderSpendAuthorization) -> int:
    return int(row.spent_prompt_tokens) + int(row.reserved_prompt_tokens)


def _committed_completion(row: ProviderSpendAuthorization) -> int:
    return int(row.spent_completion_tokens) + int(row.reserved_completion_tokens)


def _mark_terminal_if_needed(row: ProviderSpendAuthorization, *, now: datetime) -> None:
    if row.status != ProviderSpendAuthorizationStatus.active.value:
        return
    if row.expires_at is not None and row.expires_at <= now:
        row.status = ProviderSpendAuthorizationStatus.expired.value
        return
    # Exhaustion is settled-spend only. Reservations hold headroom for live checks but must
    # not flip status to exhausted — a released reservation must reopen the grant.
    if row.spent_requests >= row.max_requests or _as_decimal(row.spent_cost_usd) >= row.max_cost_usd:
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
    row = get_current_provider_spend_authorization(db, owner_id=owner_id, goal_id=goal_id)
    if row is None:
        return False
    if execution_envelope_id is not None and row.execution_envelope_id != execution_envelope_id:
        return False
    # A live grant with no remaining request capacity is not callable.
    if _committed_requests(row) >= row.max_requests:
        return False
    if _committed_cost(row) >= row.max_cost_usd:
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


def _bound_reservation(
    row: ProviderSpendAuthorization, *, provider: str, model: str
) -> tuple[int, int, Decimal]:
    prompt = row.max_prompt_tokens if row.max_prompt_tokens is not None else _DEFAULT_RESERVE_PROMPT_TOKENS
    completion = (
        row.max_completion_tokens
        if row.max_completion_tokens is not None
        else _DEFAULT_RESERVE_COMPLETION_TOKENS
    )
    # Remaining headroom on the grant caps the hold.
    if row.max_prompt_tokens is not None:
        prompt = min(prompt, max(0, row.max_prompt_tokens - _committed_prompt(row)))
    if row.max_completion_tokens is not None:
        completion = min(completion, max(0, row.max_completion_tokens - _committed_completion(row)))

    remaining_budget = row.max_cost_usd - _committed_cost(row)
    if remaining_budget < 0:
        remaining_budget = Decimal("0")

    if row.max_cost_per_request_usd is not None:
        cost = min(_as_decimal(row.max_cost_per_request_usd), remaining_budget)
    else:
        estimated = estimate_cost(provider, model, prompt, completion)
        if estimated is None:
            raise ProviderSpendError(
                "cannot bound USD cost before provider call: set max_cost_per_request_usd "
                "or allowlist a provider/model with known pricing"
            )
        cost = min(_as_decimal(estimated), remaining_budget)
    return prompt, completion, cost


def reserve_provider_spend_call(
    db: Session,
    *,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    source_ref: str,
    provider: str,
    model: str,
    task_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
    evidence: dict[str, Any] | None = None,
) -> ProviderSpendUsageEvent:
    """Atomically hold request/token/cost budget BEFORE any provider invocation.

    Idempotent on (owner_id, source_ref): a reserved or settled prior event is returned
    without increasing holds. A released prior event is treated as a new call (new reserve).
    """
    # Do NOT FOR UPDATE the usage row here — mainai_app has UPDATE revoked on append-only
    # usage events (Postgres FOR UPDATE requires UPDATE privilege). Serialize via the
    # authorization row lock inside get_current_provider_spend_authorization / below.
    prior = db.execute(
        select(ProviderSpendUsageEvent).where(
            ProviderSpendUsageEvent.owner_id == owner_id,
            ProviderSpendUsageEvent.source_ref == source_ref,
        )
    ).scalar_one_or_none()
    if prior is not None and prior.status != ProviderSpendUsageStatus.released.value:
        return prior

    row = get_current_provider_spend_authorization(db, owner_id=owner_id, goal_id=goal_id)
    if row is None:
        raise ProviderSpendError("no live provider spend authorization for this goal")
    if not _provider_allowed(row, provider, model):
        raise ProviderSpendError(f"provider/model not allowlisted on this spend grant: {provider}/{model}")

    if _committed_requests(row) + 1 > row.max_requests:
        row.status = ProviderSpendAuthorizationStatus.exhausted.value
        db.flush()
        raise ProviderSpendError("provider spend request ceiling exhausted")

    prompt, completion, cost = _bound_reservation(row, provider=provider, model=model)
    if cost <= 0 and row.max_cost_usd > 0 and _committed_cost(row) >= row.max_cost_usd:
        row.status = ProviderSpendAuthorizationStatus.exhausted.value
        db.flush()
        raise ProviderSpendError("provider spend cost ceiling exhausted")
    if _committed_cost(row) + cost > row.max_cost_usd:
        row.status = ProviderSpendAuthorizationStatus.exhausted.value
        db.flush()
        raise ProviderSpendError("provider spend cost ceiling exhausted")
    if row.max_prompt_tokens is not None and _committed_prompt(row) + prompt > row.max_prompt_tokens:
        row.status = ProviderSpendAuthorizationStatus.exhausted.value
        db.flush()
        raise ProviderSpendError("provider spend prompt-token ceiling exhausted")
    if row.max_completion_tokens is not None and _committed_completion(row) + completion > row.max_completion_tokens:
        row.status = ProviderSpendAuthorizationStatus.exhausted.value
        db.flush()
        raise ProviderSpendError("provider spend completion-token ceiling exhausted")

    event_id = uuid.uuid4()
    result = db.execute(
        pg_insert(ProviderSpendUsageEvent)
        .values(
            id=event_id,
            owner_id=owner_id,
            authorization_id=row.id,
            goal_id=goal_id,
            task_id=task_id,
            job_id=job_id,
            provider=provider,
            model=model,
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=Decimal("0"),
            reserved_prompt_tokens=prompt,
            reserved_completion_tokens=completion,
            reserved_cost_usd=cost,
            status=ProviderSpendUsageStatus.reserved.value,
            evidence=evidence or {},
            source_ref=source_ref,
        )
        .on_conflict_do_nothing(constraint="uq_provider_spend_usage_events_owner_source_ref")
        .returning(ProviderSpendUsageEvent.id)
    )
    inserted_id = result.scalar_one_or_none()
    if inserted_id is None:
        # Concurrent twin reserved the same source_ref — return the winner's row.
        return db.execute(
            select(ProviderSpendUsageEvent).where(
                ProviderSpendUsageEvent.owner_id == owner_id,
                ProviderSpendUsageEvent.source_ref == source_ref,
            )
        ).scalar_one()

    row.reserved_requests += 1
    row.reserved_prompt_tokens += prompt
    row.reserved_completion_tokens += completion
    row.reserved_cost_usd = _as_decimal(row.reserved_cost_usd) + cost
    _mark_terminal_if_needed(row, now=datetime.utcnow())
    db.flush()
    return db.get(ProviderSpendUsageEvent, inserted_id)


def settle_provider_spend_call(
    db: Session,
    *,
    owner_id: uuid.UUID,
    source_ref: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: Decimal | float | int | str = 0,
    evidence: dict[str, Any] | None = None,
) -> ProviderSpendUsageEvent:
    """Convert a reservation into settled actuals via SECURITY DEFINER (idempotent)."""
    cost = _as_decimal(cost_usd)
    db.execute(
        text(
            "SELECT settle_provider_spend_usage("
            ":owner_id, :source_ref, :prompt_tokens, :completion_tokens, :cost_usd, CAST(:evidence AS jsonb)"
            ")"
        ),
        {
            "owner_id": str(owner_id),
            "source_ref": source_ref,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cost_usd": str(cost),
            "evidence": json.dumps(evidence or {}),
        },
    )
    db.flush()
    event = db.execute(
        select(ProviderSpendUsageEvent).where(
            ProviderSpendUsageEvent.owner_id == owner_id,
            ProviderSpendUsageEvent.source_ref == source_ref,
        )
    ).scalar_one()
    auth = db.get(ProviderSpendAuthorization, event.authorization_id)
    if auth is not None:
        _mark_terminal_if_needed(auth, now=datetime.utcnow())
        db.flush()
    return event


def release_provider_spend_call(
    db: Session,
    *,
    owner_id: uuid.UUID,
    source_ref: str,
    evidence: dict[str, Any] | None = None,
) -> ProviderSpendUsageEvent:
    """Free a reservation after a failed/crashed call that must not consume budget."""
    db.execute(
        text(
            "SELECT release_provider_spend_usage(:owner_id, :source_ref, CAST(:evidence AS jsonb))"
        ),
        {
            "owner_id": str(owner_id),
            "source_ref": source_ref,
            "evidence": json.dumps(evidence or {}),
        },
    )
    db.flush()
    return db.execute(
        select(ProviderSpendUsageEvent).where(
            ProviderSpendUsageEvent.owner_id == owner_id,
            ProviderSpendUsageEvent.source_ref == source_ref,
        )
    ).scalar_one()


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
) -> ProviderSpendUsageEvent:
    """Legacy one-shot settle path: reserve then immediately settle (same source_ref).

    Prefer explicit reserve → invoke → settle around real provider calls.
    """
    reserve_provider_spend_call(
        db,
        owner_id=owner_id,
        goal_id=goal_id,
        source_ref=source_ref,
        provider=provider,
        model=model,
        task_id=task_id,
        job_id=job_id,
        evidence=evidence,
    )
    return settle_provider_spend_call(
        db,
        owner_id=owner_id,
        source_ref=source_ref,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        evidence=evidence,
    )
