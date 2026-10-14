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

P1 SPEND-8 (Claude exam of #268 @ effafbe, remaining facets from #270 @ d5153eb):
refuse before any provider call when the prompt upper bound exceeds the reserved
prompt budget; pass a completion-token limit that matches the reservation; shrink
that cap (or refuse) so the priced cost of prompt_ub + max_tokens cannot exceed the
USD hold; estimate tokens from UTF-8 bytes plus per-message overhead (not chars/3).

P1 SPEND-9: one reservation authorizes exactly one provider/model attempt; Path A does
not fall back to a second paid request under the same hold.

P2 SPEND-10: pass the reserved chain explicitly into chat_with_fallback; do not mutate
the process-wide provider registry.
"""

from __future__ import annotations

import uuid
from decimal import ROUND_DOWN, Decimal

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.provider_spend import (
    ProviderSpendError,
    get_current_provider_spend_authorization,
    release_provider_spend_call,
    reserve_provider_spend_call,
    settle_provider_spend_call,
)
from app.provider_spend.service import _bound_reservation
from app.providers.base import ChatResult, Message
from app.providers.pricing import estimate_cost, token_rates_per_1k
from app.providers.registry import chat_with_fallback, resolve_chat_chain
from app.request_context import current_user_id as current_user_id_var

# Byte-level BPE never covers less than one UTF-8 byte. Add a small per-message
# framing allowance so the pre-call size is an upper bound, not chars/3.
_PROMPT_MESSAGE_OVERHEAD_TOKENS = 8


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


def _estimate_prompt_tokens(messages: list[Message]) -> int:
    """Upper bound on prompt tokens: UTF-8 bytes plus per-message overhead (SPEND-8b)."""
    total_bytes = 0
    count = 0
    for message in messages or []:
        count += 1
        content = getattr(message, "content", None) or ""
        if not isinstance(content, str):
            content = str(content)
        total_bytes += len(content.encode("utf-8"))
    if count <= 0:
        return 0
    return total_bytes + _PROMPT_MESSAGE_OVERHEAD_TOKENS * count


def _affordable_completion_tokens(
    provider: str, model: str, prompt_tokens: int, hold_cost: Decimal, hold_completion: int
) -> int:
    """Largest completion cap whose priced cost with prompt_tokens stays within hold_cost.

    Unpriced / zero-output-rate models keep the token hold (SPEND-3 still refuses unpriced
    answers after the call). Returns 0 when even one completion token would exceed the USD hold.
    """
    if hold_completion <= 0:
        return 0
    one_token_cost = estimate_cost(provider, model, prompt_tokens, 1)
    if one_token_cost is None:
        return hold_completion
    if one_token_cost > hold_cost:
        return 0
    rates = token_rates_per_1k(provider, model)
    if rates is None:
        return hold_completion
    _input_rate, output_rate = rates
    if output_rate <= 0:
        return hold_completion
    input_cost = estimate_cost(provider, model, prompt_tokens, 0) or Decimal("0")
    headroom = hold_cost - input_cost
    if headroom <= 0:
        return 0
    per_token = output_rate / Decimal(1000)
    affordable = int((headroom / per_token).to_integral_value(rounding=ROUND_DOWN))
    affordable = min(hold_completion, max(0, affordable))
    while affordable > 0:
        priced = estimate_cost(provider, model, prompt_tokens, affordable)
        if priced is not None and priced <= hold_cost:
            break
        affordable -= 1
    return affordable


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

        # SPEND-9: one reservation = one provider/model attempt. Do not fall back inside
        # the reserved Path A call; a later retry (new reservation) may pick the next pair.
        reserved_chain = authorized[:1]
        reserve_provider, reserve_model = _provider_name(reserved_chain[0][0]), reserved_chain[0][1]

        try:
            expected_prompt, expected_completion, expected_cost = _bound_reservation(
                grant, provider=reserve_provider, model=reserve_model
            )
        except ProviderSpendError as exc:
            raise ExecutionSpendError(f"Path A AI call refused by provider_spend: {exc}") from exc
        estimated_prompt = _estimate_prompt_tokens(messages)
        if estimated_prompt > expected_prompt:
            raise ExecutionSpendError(
                "Path A AI call refused: estimated prompt exceeds the reserved prompt budget "
                f"(estimated={estimated_prompt}/{expected_prompt}). No provider request was sent."
            )
        completion_cap = _affordable_completion_tokens(
            reserve_provider, reserve_model, estimated_prompt, expected_cost, expected_completion
        )
        if completion_cap <= 0:
            raise ExecutionSpendError(
                "Path A AI call refused: priced prompt/completion cannot fit the USD hold "
                f"(hold=${expected_cost}). No provider request was sent."
            )

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
                    "estimated_prompt_tokens": estimated_prompt,
                    "max_tokens": completion_cap,
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

        hold_prompt, hold_completion, hold_cost = _reserved_hold(event)
        completion_cap = _affordable_completion_tokens(
            reserve_provider, reserve_model, estimated_prompt, hold_cost, hold_completion
        )
        if estimated_prompt > hold_prompt or completion_cap <= 0:
            # Concurrent consumption shrunk the hold after our pre-check. Do not pay.
            # SPEND-7 forbids releasing after a paid request; this path has not paid yet.
            refused = (
                "estimated prompt exceeds reserved prompt budget after reserve"
                if estimated_prompt > hold_prompt
                else "priced prompt/completion cannot fit the USD hold after reserve"
            )
            try:
                release_provider_spend_call(
                    spend_db,
                    owner_id=owner_id,
                    source_ref=source_ref,
                    evidence={
                        "path": "mainai_execution",
                        "refused": refused,
                        "estimated_prompt_tokens": estimated_prompt,
                        "reserved_prompt_tokens": hold_prompt,
                        "max_tokens": completion_cap,
                    },
                )
                spend_db.commit()
            except Exception:
                spend_db.rollback()
            if estimated_prompt > hold_prompt:
                raise ExecutionSpendError(
                    "Path A AI call refused: estimated prompt exceeds the reserved prompt budget "
                    f"(estimated={estimated_prompt}/{hold_prompt}). No provider request was sent."
                )
            raise ExecutionSpendError(
                "Path A AI call refused: priced prompt/completion cannot fit the USD hold "
                f"(hold=${hold_cost}). No provider request was sent."
            )

        attempted: list[str] = []
        result: ChatResult | None = None
        provider_error: Exception | None = None
        try:
            result, attempted = await chat_with_fallback(
                db,
                messages,
                owner_id=owner_id,
                purpose=purpose,
                requested_by=requested_by,
                task_id=task_id,
                job_id=job_id,
                chain=reserved_chain,
                max_tokens=completion_cap,
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
            extra_evidence={
                "provider_error": None if provider_error is None else type(provider_error).__name__,
                "answering_provider": None if result is None else result.provider,
                "answering_model": None if result is None else result.model,
                "max_tokens": completion_cap,
            },
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
