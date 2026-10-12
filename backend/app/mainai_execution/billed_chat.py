"""Path A billed chat: reserve/settle through existing provider_spend, no new ledger.

P1 (Claude exam of #264 @ 21ff3cc): provider-spend authorization is required by default;
fallback answers outside the grant allowlist are refused; unknown pricing is refused
rather than settled at $0; reservation/settlement failures become ExecutionSpendError
so run_task_execution_job can leave the task recoverable instead of stuck `running`."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import get_settings
from app.provider_spend import (
    ProviderSpendError,
    get_current_provider_spend_authorization,
    release_provider_spend_call,
    reserve_provider_spend_call,
    settle_provider_spend_call,
)
from app.providers.base import ChatResult, Message
from app.providers.pricing import estimate_cost
from app.providers.registry import chat_with_fallback, resolve_chat_chain


class ExecutionSpendError(RuntimeError):
    """Path A refused an AI call because the per-goal spend cap/grant is not usable."""


def _usage_tokens(result: ChatResult) -> tuple[int, int]:
    usage = result.raw_usage or {}
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    return int(prompt or 0), int(completion or 0)


def _cap_usd() -> Decimal:
    return Decimal(str(get_settings().mainai_execution_goal_spend_cap_usd))


def _grant_allows(grant, provider: str, model: str) -> bool:
    providers = list(grant.allowed_providers or [])
    models = list(grant.allowed_models or [])
    if providers and provider not in providers:
        return False
    if models and model not in models:
        return False
    return True


def _release_quietly(db: Session, *, owner_id: uuid.UUID, source_ref: str, reason: str) -> None:
    try:
        release_provider_spend_call(
            db,
            owner_id=owner_id,
            source_ref=source_ref,
            evidence={"released": reason},
        )
    except Exception:
        return


async def billed_execution_chat(
    db: Session,
    messages: list[Message],
    *,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    purpose: str,
    requested_by: str,
    task_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> tuple[ChatResult, list[str]]:
    settings = get_settings()
    grant = get_current_provider_spend_authorization(db, owner_id=owner_id, goal_id=goal_id)
    if grant is None:
        if settings.mainai_execution_require_provider_spend:
            raise ExecutionSpendError(
                "Path A AI call refused: no live provider-spend authorization for this goal. "
                "Founder must grant spend through the existing /api/provider-spend API."
            )
        return await chat_with_fallback(
            db,
            messages,
            owner_id=owner_id,
            purpose=purpose,
            requested_by=requested_by,
            task_id=task_id,
            goal_id=goal_id,
            job_id=job_id,
        )

    if grant.max_cost_usd > _cap_usd():
        raise ExecutionSpendError(
            f"Path A AI call refused: grant max_cost_usd={grant.max_cost_usd} exceeds "
            f"MAINAI_EXECUTION_GOAL_SPEND_CAP_USD={_cap_usd()}."
        )

    chain = resolve_chat_chain(db)
    if not chain:
        raise ExecutionSpendError("Path A AI call refused: no configured chat provider.")

    # Inspect the actual answer before creating a usage row. A fallback provider/model
    # outside the grant, or an unpriced model, must not leave a $0 settled event attributed
    # to the reserved primary (Claude SPEND-2/SPEND-3). The call still goes through
    # chat_with_fallback so existing provider adapters stay in place; the grant is the
    # accept/settle authority, not the first chain entry. Provider-call failures stay
    # their original type — only reservation/settlement/allowlist/pricing failures are
    # ExecutionSpendError.
    result, attempted = await chat_with_fallback(
        db,
        messages,
        owner_id=owner_id,
        purpose=purpose,
        requested_by=requested_by,
        task_id=task_id,
        goal_id=goal_id,
        job_id=job_id,
    )

    actual_provider = result.provider
    actual_model = result.model
    if not _grant_allows(grant, actual_provider, actual_model):
        raise ExecutionSpendError(
            f"Path A AI call refused: answer came from {actual_provider}/{actual_model}, "
            "which is outside the Founder's provider-spend allowlist. Fallback must not "
            "bypass the authorized grant."
        )

    prompt_tokens, completion_tokens = _usage_tokens(result)
    cost = estimate_cost(actual_provider, actual_model, prompt_tokens, completion_tokens)
    if cost is None:
        raise ExecutionSpendError(
            f"Path A AI call refused: unknown pricing for {actual_provider}/{actual_model}. "
            "Unpriced answers are not settled at $0."
        )

    source_ref = (
        f"mainai-execution:{purpose}:{goal_id}:{task_id or 'plan'}:{job_id or 'none'}:{uuid.uuid4()}"
    )
    try:
        _event, created = reserve_provider_spend_call(
            db,
            owner_id=owner_id,
            goal_id=goal_id,
            source_ref=source_ref,
            provider=actual_provider,
            model=actual_model,
            task_id=task_id,
            job_id=job_id,
            evidence={
                "purpose": purpose,
                "path": "mainai_execution",
                "attempted": attempted,
                "actual_provider": actual_provider,
                "actual_model": actual_model,
            },
        )
    except ProviderSpendError as exc:
        raise ExecutionSpendError(f"Path A AI call refused by provider_spend: {exc}") from exc
    if not created:
        raise ExecutionSpendError("Path A AI call refused: spend reservation was not created.")

    try:
        settle_provider_spend_call(
            db,
            owner_id=owner_id,
            source_ref=source_ref,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost,
            evidence={
                "provider": actual_provider,
                "model": actual_model,
                "attempted": attempted,
            },
        )
    except Exception as exc:
        _release_quietly(db, owner_id=owner_id, source_ref=source_ref, reason="settle_failed")
        raise ExecutionSpendError(f"Path A spend settlement failed: {exc}") from exc
    return result, attempted
