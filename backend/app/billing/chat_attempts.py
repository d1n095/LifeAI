"""Durable, evidence-gated reconciliation for billable chat attempts (R15-02).

The problem this closes: a provider timeout/crash can leave an unknown external effect and a
held ComputeBudgetStore reservation. The chat row then blocks retries (correctly), but nothing
can ever close the hold — the in-process Reservation object is gone after a restart, and
releasing it blind would refund a call that may have been billed.

The invariant is the same one provider_planning/service.py already uses:

    UNKNOWN EXTERNAL EFFECT != NO EXTERNAL EFFECT.

This module therefore never invokes a provider and never refunds on a guess:

* ``reserved`` — the durable invocation marker was never written, so no request can have
  left. A stale row in this state may be released, but only with a reviewable evidence ref.
* ``invoking`` / ``held`` — the provider may have been reached. Only a conservative
  settlement (charge the already-reserved worst case) or a settlement from recorded usage
  is allowed. A release is refused.
* ``settlement_pending`` / ``release_pending`` — a previous finalization could not be proven;
  retry the same finalization (the store's finalize operation is idempotent).

Every transition is owner-scoped (FORCE RLS + explicit owner filter), audited, and committed
with the attempt row so a restart resumes from durable state rather than memory.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from app.audit import record_audit
from app.billing.enforcement import Reservation
from app.config import get_settings
from app.models.conversation import (
    CHAT_ATTEMPT_UNRESOLVED_STATES,
    ChatProviderAttempt,
    Message,
    MessageStatus,
)

logger = logging.getLogger(__name__)

# A live provider call is bounded by the provider/httpx timeouts (the local verification
# budget is 120s). Fifteen minutes is comfortably beyond any real invocation, so a row older
# than this cannot still be owned by a live request. The threshold is deliberately not a
# config knob: lowering it is a security-relevant change, not an operational tweak.
DEFAULT_STALE_AFTER_SECONDS = 900

RELEASE_PRE_EGRESS = "release_pre_egress"
SETTLE_CONSERVATIVE = "settle_conservative"
SETTLE_FROM_USAGE = "settle_from_usage"
RECONCILIATION_RESOLUTIONS = frozenset(
    {RELEASE_PRE_EGRESS, SETTLE_CONSERVATIVE, SETTLE_FROM_USAGE}
)


class ReconciliationDenied(RuntimeError):
    """The requested resolution is not provable from durable state. Nothing is changed."""


class ReconciliationUnavailable(RuntimeError):
    """The reservation store could not be finalized; the attempt remains unresolved."""


def _utcnow() -> datetime:
    # Chat timestamps are naive UTC throughout (see Message.created_at), so keep the
    # comparison in the same clock domain instead of mixing aware/naive datetimes.
    return datetime.utcnow()


def _iter_reservations(reservations) -> list:
    each = getattr(reservations, "_each", None)
    return list(each()) if each is not None else list(reservations)


def serialize_reservations(reservations) -> list[dict]:
    """Store the exact identities needed to finalize the hold after a process restart."""

    return [
        {
            "subject_type": r.subject_type,
            "subject_id": r.subject_id,
            "tokens": int(r.tokens),
            "cost_micros": int(r.cost_micros),
            "period_keys": list(r.period_keys),
            "reservation_id": r.reservation_id,
        }
        for r in _iter_reservations(reservations)
    ]


def reservations_from_record(record: list[dict] | None) -> list[Reservation]:
    if not record:
        return []
    return [
        Reservation(
            subject_type=row["subject_type"],
            subject_id=row["subject_id"],
            tokens=int(row["tokens"]),
            cost_micros=int(row["cost_micros"]),
            period_keys=tuple(row["period_keys"]),
            reservation_id=row["reservation_id"],
        )
        for row in record
    ]


def create_attempt(
    db: Session,
    *,
    owner_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_message_id: uuid.UUID,
    assistant_message_id: uuid.UUID,
    source_ref: str,
    provider: str,
    model: str,
    reservations,
) -> ChatProviderAttempt:
    """Persist the reservation identity BEFORE the provider is invoked.

    The caller commits this row together with the durable reply claim. If the insert fails,
    the caller releases the freshly-created hold — an attempt that cannot be recorded must
    never become an untraceable reservation.
    """

    serialized = serialize_reservations(reservations)
    attempt = ChatProviderAttempt(
        owner_id=owner_id,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        source_ref=source_ref,
        provider=provider,
        model=model,
        state="reserved",
        effect_boundary=None,
        reservations=serialized,
        evidence={},
        reserved_tokens=sum(row["tokens"] for row in serialized),
        reserved_cost_micros=sum(row["cost_micros"] for row in serialized),
    )
    db.add(attempt)
    db.flush()
    return attempt


def _merge_evidence(attempt: ChatProviderAttempt, **values) -> None:
    attempt.evidence = {**(attempt.evidence or {}), **values}


def mark_invoking(db: Session, attempt: ChatProviderAttempt) -> None:
    """Write the durable boundary immediately before the outbound provider call.

    Once this commits, the attempt is ambiguous by construction: a crash after this point
    can no longer prove that no request left the process.
    """

    attempt.state = "invoking"
    attempt.effect_boundary = "ambiguous"
    _merge_evidence(attempt, invocation_started_at=_utcnow().isoformat())
    db.flush()


def mark_held(db: Session, attempt: ChatProviderAttempt, *, evidence: dict) -> None:
    attempt.state = "held"
    attempt.effect_boundary = "ambiguous"
    _merge_evidence(attempt, **evidence)
    db.flush()


def mark_released(db: Session, attempt: ChatProviderAttempt, *, evidence: dict) -> None:
    attempt.state = "released"
    attempt.effect_boundary = "pre_egress"
    _merge_evidence(attempt, **evidence)
    db.flush()


def mark_release_pending(
    db: Session, attempt: ChatProviderAttempt, *, evidence: dict
) -> None:
    attempt.state = "release_pending"
    attempt.effect_boundary = "pre_egress"
    _merge_evidence(attempt, **evidence)
    db.flush()


def mark_settled(
    db: Session, attempt: ChatProviderAttempt, *, boundary: str, evidence: dict
) -> None:
    attempt.state = "settled"
    attempt.effect_boundary = boundary
    _merge_evidence(attempt, **evidence)
    db.flush()


def mark_settlement_pending(
    db: Session, attempt: ChatProviderAttempt, *, evidence: dict
) -> None:
    attempt.state = "settlement_pending"
    _merge_evidence(attempt, **evidence)
    db.flush()


def release_is_provably_safe(attempt: ChatProviderAttempt) -> bool:
    return (
        attempt.state == "reserved" and attempt.effect_boundary in (None, "pre_egress")
    ) or (
        attempt.state == "release_pending"
        and attempt.effect_boundary == "pre_egress"
    )


def decide_reconciliation(
    *,
    state: str,
    effect_boundary: str | None,
    resolution: str,
    evidence_ref: str | None,
    age_seconds: float,
    has_usage_evidence: bool,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> str | None:
    """Return a denial reason, or None when the resolution is provable.

    Pure policy: no DB, no store. The endpoint and tests both call this, so the decision
    cannot drift from the actual write path.
    """

    if resolution not in RECONCILIATION_RESOLUTIONS:
        return "Okänd avstämningsåtgärd."
    if not evidence_ref or len(evidence_ref.strip()) < 8:
        return "En verifierbar bevisreferens krävs innan något ändras."
    if age_seconds < stale_after_seconds:
        return (
            "Försöket är inte tillräckligt gammalt; en pågående begäran kan fortfarande äga "
            "det. Vänta tills det är stale."
        )

    if resolution == RELEASE_PRE_EGRESS:
        if state == "reserved" and effect_boundary in (None, "pre_egress"):
            return None
        if state == "release_pending" and effect_boundary == "pre_egress":
            return None
        return (
            "Släpp nekas: försöket kan ha nått leverantören. Använd settle_conservative "
            "eller settle_from_usage."
        )

    if resolution == SETTLE_CONSERVATIVE:
        if state in {"invoking", "held", "release_pending", "settlement_pending"}:
            return None
        return (
            "Konservativ avräkning kräver ett försök som kan ha nått leverantören; "
            "ett oanropat försök ska släppas, inte debiteras."
        )

    # SETTLE_FROM_USAGE
    if state in {"invoking", "held", "settlement_pending"} and has_usage_evidence:
        return None
    return "Avräkning från användning kräver dokumenterad leverantörsanvändning."


def _store():
    # Reuse the single ComputeBudgetStore factory from billing.reservation — no parallel
    # budget path, no second Redis client.
    from app.billing.reservation import _store as reservation_store

    return reservation_store(get_settings())


def _settle_usage(
    attempt: ChatProviderAttempt, *, tokens: int, cost_usd: Decimal
) -> None:
    store = _store()
    for reservation in reservations_from_record(attempt.reservations):
        store.settle(reservation, actual_tokens=tokens, actual_cost_usd=cost_usd)


def _release(attempt: ChatProviderAttempt) -> None:
    store = _store()
    for reservation in reservations_from_record(attempt.reservations):
        store.release(reservation)


def _settle_conservative(attempt: ChatProviderAttempt) -> None:
    store = _store()
    for reservation in reservations_from_record(attempt.reservations):
        store.settle(
            reservation,
            actual_tokens=reservation.tokens,
            actual_cost_usd=Decimal(reservation.cost_micros) / Decimal(1_000_000),
        )


def _message(db: Session, attempt: ChatProviderAttempt) -> Message | None:
    return (
        db.query(Message)
        .filter(
            Message.id == attempt.assistant_message_id,
            Message.conversation_id == attempt.conversation_id,
        )
        .with_for_update()
        .one_or_none()
    )


def _mark_message_failed(db: Session, attempt: ChatProviderAttempt, category: str) -> None:
    message = _message(db, attempt)
    if message is None or message.status == MessageStatus.succeeded:
        # Never rewrite a reply that actually succeeded; recovery only closes unresolved
        # attempts.
        return
    message.status = MessageStatus.failed
    message.error_category = category
    message.content = ""
    db.flush()


def list_unresolved_attempts(
    db: Session, *, owner_id: uuid.UUID
) -> list[ChatProviderAttempt]:
    return (
        db.query(ChatProviderAttempt)
        .filter(
            ChatProviderAttempt.owner_id == owner_id,
            ChatProviderAttempt.state.in_(CHAT_ATTEMPT_UNRESOLVED_STATES),
        )
        .order_by(ChatProviderAttempt.updated_at.asc())
        .all()
    )


def attempt_age_seconds(attempt: ChatProviderAttempt, *, now: datetime | None = None) -> float:
    updated = attempt.updated_at or attempt.created_at or _utcnow()
    return max((now or _utcnow()) - updated, timedelta(0)).total_seconds()


def reconcile_attempt(
    db: Session,
    *,
    owner_id: uuid.UUID,
    attempt_id: uuid.UUID,
    resolution: str,
    evidence_ref: str,
    actor_id: uuid.UUID,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
) -> ChatProviderAttempt:
    """Finalize one unresolved attempt without invoking any provider.

    Owner-scoped and row-locked. The reservation is finalized FIRST; only then is the
    attempt state and message committed. If the store refuses, the transaction is rolled
    back and the attempt stays unresolved — never a fabricated release/settlement.
    """

    attempt = (
        db.query(ChatProviderAttempt)
        .filter(
            ChatProviderAttempt.id == attempt_id,
            ChatProviderAttempt.owner_id == owner_id,
        )
        .with_for_update()
        .one_or_none()
    )
    if attempt is None:
        raise LookupError("attempt not found")

    previous_state = attempt.state
    usage = (attempt.evidence or {}).get("usage") or {}
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    has_usage = (prompt + completion) > 0

    denial = decide_reconciliation(
        state=attempt.state,
        effect_boundary=attempt.effect_boundary,
        resolution=resolution,
        evidence_ref=evidence_ref,
        age_seconds=attempt_age_seconds(attempt),
        has_usage_evidence=has_usage,
        stale_after_seconds=stale_after_seconds,
    )
    if denial is not None:
        raise ReconciliationDenied(denial)

    try:
        if resolution == RELEASE_PRE_EGRESS:
            _release(attempt)
            mark_released(
                db,
                attempt,
                evidence={
                    "reason": "reviewed_pre_egress",
                    "evidence_ref": evidence_ref.strip(),
                    "reconciled_by": str(actor_id),
                },
            )
            _mark_message_failed(db, attempt, "provider_pre_egress_released")
        elif resolution == SETTLE_CONSERVATIVE:
            _settle_conservative(attempt)
            mark_settled(
                db,
                attempt,
                boundary="ambiguous",
                evidence={
                    "reason": "conservative_reserved_amount",
                    "evidence_ref": evidence_ref.strip(),
                    "reconciled_by": str(actor_id),
                },
            )
            _mark_message_failed(db, attempt, "external_effect_unresolved")
        else:  # SETTLE_FROM_USAGE
            from app.billing.reservation import estimate_reservation_cost

            tokens = prompt + completion
            cost = estimate_reservation_cost(
                None, tokens, provider=attempt.provider, model=attempt.model
            )
            _settle_usage(attempt, tokens=tokens, cost_usd=cost)
            mark_settled(
                db,
                attempt,
                boundary="observed",
                evidence={
                    "reason": "recorded_usage",
                    "evidence_ref": evidence_ref.strip(),
                    "reconciled_by": str(actor_id),
                },
            )
            _mark_message_failed(db, attempt, "external_effect_unresolved")
    except Exception as exc:
        # A store/identity failure must leave the attempt unresolved, never half-applied.
        # The store's finalize operation is idempotent, so a retry is safe.
        db.rollback()
        raise ReconciliationUnavailable(
            "Budgetreservationen kunde inte slutföras; försöket är fortfarande oavstämt."
        ) from exc

    record_audit(
        db,
        user_id=actor_id,
        action="chat_attempt_reconciled",
        entity_type="chat_provider_attempt",
        entity_id=str(attempt.id),
        commit=False,
        detail=json.dumps(
            {
                "resolution": resolution,
                "evidence_ref": evidence_ref.strip(),
                "previous_state": previous_state,
                "effect_boundary": attempt.effect_boundary,
                "provider": attempt.provider,
                "model": attempt.model,
            }
        ),
    )
    db.commit()
    db.refresh(attempt)
    logger.warning(
        "chat attempt %s reconciled by %s as %s (evidence ref present)",
        attempt.id,
        actor_id,
        resolution,
    )
    return attempt
