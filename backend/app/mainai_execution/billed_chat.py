"""Path A billed chat: reserve/settle through existing provider_spend, no new ledger.

P1 (Claude exam of #264 @ 21ff3cc): provider-spend authorization is required by default;
fallback answers outside the grant allowlist are refused; unknown pricing is refused
rather than settled at $0; reservation/settlement failures become ExecutionSpendError
so run_task_execution_job can leave the task recoverable instead of stuck `running`.

P1 SPEND-7 (Claude re-exam of #266 @ dba9390): reserve authorized spend BEFORE any paid
provider call; only the allowlisted provider/model may be invoked, including fallback;
every paid request is counted even when the answer is refused, oversized, unpriced, or
settlement fails; usage accounting commits on an independent session so a later job
rollback cannot un-count it; never release a hold after a paid request.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.provider_spend import (
    ProviderSpendError,
    get_current_provider_spend_authorization,
    reserve_provider_spend_call,
    settle_provider_spend_call,
)
from app.providers import registry as provider_registry
from app.providers.base import ChatResult, Message
from app.providers.pricing import estimate_cost
from app.providers.registry import chat_with_fallback, resolve_chat_chain
from app.request_context import current_user_id as current_user_id_var


class ExecutionSpendError(RuntimeError):
    """Path A refused an AI call because the per-goal spend cap/grant is not usable."""


def _usage_tokens(result: ChatResult) -> tuple[int, int]:
    usage = result.raw_usage or {}
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
    completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
    return int(prompt or 0), int(completion or 0)


def _cap_usd() -> Decimal:
    return Decimal(str(get_settings().mainai_execution_goal_spend_cap_usd))


def _as_decimal(value: Decimal | float | int | str | None) -> Decimal:
    if value is None:
        return Decimal("0")
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _grant_allows(grant, provider: str, model: str) -> bool:
    providers = list(grant.allowed_providers or [])
    models = list(grant.allowed_models or [])
    if providers and provider not in providers:
        return False
    if models and model not in models:
        return False
    return True


def _provider_name(provider) -> str:
    name = getattr(provider, "name", None)
    return str(name if name is not None else provider)


def _authorized_chain(chain, grant) -> list:
    return [(provider, model) for provider, model in chain if _grant_allows(grant, _provider_name(provider), model)]


@contextmanager
def _constrain_chat_chain(authorized: list):
    """Restrict registry fallback to the already-authorized pairs for this call only."""
    original = provider_registry.resolve_chat_chain
    provider_registry.resolve_chat_chain = lambda _db: list(authorized)
    try:
        yield
    finally:
        provider_registry.resolve_chat_chain = original


def _open_spend_session(owner_id: uuid.UUID) -> Session:
    """Independent session so reserve/settle commits survive the job session rollback."""
    if not current_user_id_var.get():
        current_user_id_var.set(str(owner_id))
    return SessionLocal()


def _reserved_hold(event) -> tuple[int, int, Decimal]:
    return (
        int(event.reserved_prompt_tokens or 0),
        int(event.reserved_completion_tokens or 0),
        _as_decimal(event.reserved_cost_usd),
    )


def _account_paid_call(
    spend_db: Session,
    *,
    owner_id: uuid.UUID,
    source_ref: str,
    event,
    result: ChatResult | None,
    attempted: list[str],
    refusal: str | None,
    over_hold: bool,
    extra_evidence: dict | None = None,
) -> bool:
    """Settle a hold after a paid request. Never release. Returns True if settled.

    A failed settle leaves the row `reserved`, which still counts against the grant.
    """
    hold_prompt, hold_completion, hold_cost = _reserved_hold(event)
    evidence: dict = {
        "path": "mainai_execution",
        "attempted": attempted,
        "over_hold": over_hold,
        "refused": refusal,
        **(extra_evidence or {}),
    }
    if result is not None:
        prompt_tokens, completion_tokens = _usage_tokens(result)
        cost = estimate_cost(result.provider, result.model, prompt_tokens, completion_tokens)
        evidence.update(
            {
                "provider": result.provider,
                "model": result.model,
                "actual_prompt_tokens": prompt_tokens,
                "actual_completion_tokens": completion_tokens,
                "actual_cost_usd": None if cost is None else str(cost),
            }
        )
        settle_prompt = min(max(0, prompt_tokens), hold_prompt)
        settle_completion = min(max(0, completion_tokens), hold_completion)
        if refusal or over_hold or cost is None:
            # Conservative: consume the hold so a refused paid answer cannot be retried free.
            settle_cost = hold_cost
        else:
            settle_cost = min(cost, hold_cost)
    else:
        settle_prompt, settle_completion, settle_cost = hold_prompt, hold_completion, hold_cost

    try:
        settle_provider_spend_call(
            spend_db,
            owner_id=owner_id,
            source_ref=source_ref,
            prompt_tokens=settle_prompt,
            completion_tokens=settle_completion,
            cost_usd=settle_cost,
            evidence=evidence,
        )
        spend_db.commit()
        return True
    except Exception as first_exc:
        spend_db.rollback()
        try:
            settle_provider_spend_call(
                spend_db,
                owner_id=owner_id,
                source_ref=source_ref,
                prompt_tokens=hold_prompt,
                completion_tokens=hold_completion,
                cost_usd=hold_cost,
                evidence={
                    **evidence,
                    "conservative_settle": True,
                    "first_settle_error": str(first_exc)[:300],
                },
            )
            spend_db.commit()
            return True
        except Exception:
            spend_db.rollback()
            return False


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
    spend_db = _open_spend_session(owner_id)
    try:
        grant = get_current_provider_spend_authorization(
            spend_db, owner_id=owner_id, goal_id=goal_id
        )
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
        authorized = _authorized_chain(chain, grant)
        if not authorized:
            raise ExecutionSpendError(
                "Path A AI call refused: no configured chat provider/model is on the "
                "Founder's provider-spend allowlist. Fallback must not bypass the authorized grant."
            )

        reserve_provider, reserve_model = _provider_name(authorized[0][0]), authorized[0][1]
        source_ref = (
            f"mainai-execution:{purpose}:{goal_id}:{task_id or 'plan'}:"
            f"{job_id or 'none'}:{uuid.uuid4()}"
        )
        try:
            event, created = reserve_provider_spend_call(
                spend_db,
                owner_id=owner_id,
                goal_id=goal_id,
                source_ref=source_ref,
                provider=reserve_provider,
                model=reserve_model,
                task_id=task_id,
                job_id=job_id,
                evidence={
                    "purpose": purpose,
                    "path": "mainai_execution",
                    "reserved_provider": reserve_provider,
                    "reserved_model": reserve_model,
                },
            )
        except ProviderSpendError as exc:
            spend_db.rollback()
            raise ExecutionSpendError(f"Path A AI call refused by provider_spend: {exc}") from exc
        if not created:
            spend_db.rollback()
            raise ExecutionSpendError("Path A AI call refused: spend reservation was not created.")
        # Durable before any paid invoke. A later job-session rollback must not un-count this.
        spend_db.commit()

        attempted: list[str] = []
        result: ChatResult | None = None
        provider_error: Exception | None = None
        try:
            with _constrain_chat_chain(authorized):
                result, attempted = await chat_with_fallback(
                    db,
                    messages,
                    owner_id=owner_id,
                    purpose=purpose,
                    requested_by=requested_by,
                    task_id=task_id,
                    job_id=job_id,
                )
        except Exception as exc:  # noqa: BLE001 - paid window is ambiguous; never release
            provider_error = exc
            attempted = list(attempted)

        refusal: str | None = None
        over_hold = False
        if result is not None:
            actual_provider = result.provider
            actual_model = result.model
            prompt_tokens, completion_tokens = _usage_tokens(result)
            cost = estimate_cost(actual_provider, actual_model, prompt_tokens, completion_tokens)
            hold_prompt, hold_completion, hold_cost = _reserved_hold(event)
            over_hold = (
                prompt_tokens > hold_prompt
                or completion_tokens > hold_completion
                or (cost is not None and cost > hold_cost)
            )
            if not _grant_allows(grant, actual_provider, actual_model):
                refusal = (
                    f"Path A AI call refused: answer came from {actual_provider}/{actual_model}, "
                    "which is outside the Founder's provider-spend allowlist. Fallback must not "
                    "bypass the authorized grant."
                )
            elif cost is None:
                refusal = (
                    f"Path A AI call refused: unknown pricing for {actual_provider}/{actual_model}. "
                    "Unpriced answers are not settled at $0."
                )
            elif over_hold:
                refusal = (
                    "Path A AI call refused: answer exceeded the reservation hold "
                    f"(prompt={prompt_tokens}/{hold_prompt}, "
                    f"completion={completion_tokens}/{hold_completion})."
                )

        settled = _account_paid_call(
            spend_db,
            owner_id=owner_id,
            source_ref=source_ref,
            event=event,
            result=result,
            attempted=attempted,
            refusal=refusal,
            over_hold=over_hold,
            extra_evidence={"provider_error": None if provider_error is None else type(provider_error).__name__},
        )

        if provider_error is not None:
            raise ExecutionSpendError(
                f"Path A provider call failed after reservation: {provider_error}"
            ) from provider_error
        if not settled:
            raise ExecutionSpendError(
                "Path A spend settlement failed after a paid provider request; "
                "the reservation remains counted against the grant."
            )
        if refusal:
            raise ExecutionSpendError(refusal)
        return result, attempted
    finally:
        spend_db.close()
