"""Path A billed chat: reserve/settle through existing provider_spend, no new ledger."""

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
    provider, model = chain[0]
    source_ref = (
        f"mainai-execution:{purpose}:{goal_id}:{task_id or 'plan'}:{job_id or 'none'}:{uuid.uuid4()}"
    )
    try:
        _event, created = reserve_provider_spend_call(
            db,
            owner_id=owner_id,
            goal_id=goal_id,
            source_ref=source_ref,
            provider=provider.name,
            model=model,
            task_id=task_id,
            job_id=job_id,
            evidence={"purpose": purpose, "path": "mainai_execution"},
        )
    except ProviderSpendError as exc:
        raise ExecutionSpendError(f"Path A AI call refused by provider_spend: {exc}") from exc
    if not created:
        raise ExecutionSpendError("Path A AI call refused: spend reservation was not created.")

    try:
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
    except Exception:
        release_provider_spend_call(db, owner_id=owner_id, source_ref=source_ref, evidence={"released": "call_failed"})
        raise

    prompt_tokens, completion_tokens = _usage_tokens(result)
    cost = estimate_cost(result.provider, result.model, prompt_tokens, completion_tokens)
    if cost is None:
        cost = Decimal("0")
    settle_provider_spend_call(
        db,
        owner_id=owner_id,
        source_ref=source_ref,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        evidence={"provider": result.provider, "model": result.model, "attempted": attempted},
    )
    return result, attempted
