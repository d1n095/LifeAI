"""Stage J — Personal intent learning (founder-specific phrasing → durable bindings).

Interpretation remains separate from authority. Wrong terminology never overwrites
canonical entity titles — corrections attach as phrasing history / aliases only.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.concept_reconciliation.normalize import normalize_concept_text
from app.founder_language import resolve_founder_expression
from app.models.founder_intent import FounderIntentBinding, FounderIntentCorrection

# Ambiguity that may auto-resolve (low-risk) vs must surface (consequential).
_CONSEQUENTIAL_MARKERS = re.compile(
    r"\b(radera|delete|deploy|production|prod|merge|force|authority|behörighet|godkänn|approve)\b",
    re.IGNORECASE,
)


class AmbiguityClass(str, Enum):
    NONE = "none"
    LOW_RISK = "low_risk"  # may auto-resolve via learned phrasing
    CONSEQUENTIAL = "consequential"  # must surface before irreversible effect


@dataclass
class IntentResolution:
    raw_expression: str
    interpreted_intent: str
    canonical_entity_id: uuid.UUID | None
    confidence: float
    context: dict = field(default_factory=dict)
    binding_id: uuid.UUID | None = None
    correction_history: list[dict] = field(default_factory=list)
    retrieval_trigger: str | None = None
    ambiguity: AmbiguityClass = AmbiguityClass.NONE
    must_surface: bool = False
    auto_resolved: bool = False
    authority_claimed: bool = False  # always False from this module


class PersonalIntentError(ValueError):
    pass


def classify_ambiguity(raw: str, *, residual_intent: str, confidence: float) -> AmbiguityClass:
    if _CONSEQUENTIAL_MARKERS.search(raw or "") or _CONSEQUENTIAL_MARKERS.search(residual_intent or ""):
        return AmbiguityClass.CONSEQUENTIAL
    if confidence < 0.5 or len((residual_intent or "").split()) < 2:
        # unfinished / vague reference
        if confidence < 0.45:
            return AmbiguityClass.CONSEQUENTIAL if _CONSEQUENTIAL_MARKERS.search(raw or "") else AmbiguityClass.LOW_RISK
        return AmbiguityClass.LOW_RISK
    return AmbiguityClass.NONE


def _phrase_key(raw: str) -> str:
    return normalize_concept_text(raw)[:512]


def get_binding_by_phrase(db: Session, *, owner_id: uuid.UUID, raw_expression: str) -> FounderIntentBinding | None:
    key = _phrase_key(raw_expression)
    if not key:
        return None
    return db.execute(
        select(FounderIntentBinding).where(
            FounderIntentBinding.owner_id == owner_id,
            FounderIntentBinding.phrase_normalized == key,
            FounderIntentBinding.status == "active",
        )
    ).scalar_one_or_none()


def list_corrections(db: Session, *, owner_id: uuid.UUID, binding_id: uuid.UUID) -> list[FounderIntentCorrection]:
    return list(
        db.execute(
            select(FounderIntentCorrection)
            .where(
                FounderIntentCorrection.owner_id == owner_id,
                FounderIntentCorrection.binding_id == binding_id,
            )
            .order_by(FounderIntentCorrection.created_at)
        ).scalars().all()
    )


def record_intent_binding(
    db: Session,
    *,
    owner_id: uuid.UUID,
    raw_expression: str,
    interpreted_intent: str,
    canonical_entity_id: uuid.UUID | None,
    confidence: float,
    context: dict | None = None,
    retrieval_trigger: str | None = None,
    idempotency_key: str,
) -> FounderIntentBinding:
    """Create or reinforce a phrasing→intent binding. Never rewrites entity canonical titles."""
    existing = db.execute(
        select(FounderIntentBinding).where(
            FounderIntentBinding.owner_id == owner_id,
            FounderIntentBinding.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    phrase_norm = _phrase_key(raw_expression)
    by_phrase = get_binding_by_phrase(db, owner_id=owner_id, raw_expression=raw_expression)
    if by_phrase is not None:
        # Reinforce: bump hit_count / confidence toward observed (never invent authority).
        by_phrase.hit_count = int(by_phrase.hit_count or 0) + 1
        by_phrase.last_seen_at = datetime.utcnow()
        if confidence > float(by_phrase.confidence or 0):
            by_phrase.confidence = confidence
        if interpreted_intent and interpreted_intent != by_phrase.interpreted_intent:
            # Do not silently overwrite — record as soft reinforcement of alternate surface only
            # when the new intent normalizes equal; otherwise leave for explicit correct_intent.
            if normalize_concept_text(interpreted_intent) == normalize_concept_text(by_phrase.interpreted_intent):
                by_phrase.interpreted_intent = interpreted_intent
        if canonical_entity_id and by_phrase.canonical_entity_id is None:
            by_phrase.canonical_entity_id = canonical_entity_id
        if retrieval_trigger:
            by_phrase.retrieval_trigger = retrieval_trigger
        if context:
            prov = dict(by_phrase.context or {})
            prov.update(context)
            by_phrase.context = prov
        db.flush()
        return by_phrase

    row = FounderIntentBinding(
        owner_id=owner_id,
        raw_expression=raw_expression,
        phrase_normalized=phrase_norm,
        interpreted_intent=interpreted_intent,
        canonical_entity_id=canonical_entity_id,
        confidence=confidence,
        context=dict(context or {}),
        retrieval_trigger=retrieval_trigger,
        hit_count=1,
        status="active",
        idempotency_key=idempotency_key,
        last_seen_at=datetime.utcnow(),
    )
    db.add(row)
    db.flush()
    return row


def correct_intent_binding(
    db: Session,
    *,
    owner_id: uuid.UUID,
    binding_id: uuid.UUID,
    corrected_intent: str,
    reason: str,
    canonical_entity_id: uuid.UUID | None = None,
    wrong_terminology: str | None = None,
) -> FounderIntentBinding:
    """Founder correction path. Preserves prior interpretation in correction history.

    Wrong terminology must NOT overwrite canonical entity truth — we only update the binding's
    interpreted_intent and optionally point at an existing canonical entity.
    """
    row = db.execute(
        select(FounderIntentBinding)
        .where(FounderIntentBinding.id == binding_id, FounderIntentBinding.owner_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise PersonalIntentError("binding missing or wrong owner")

    prior = row.interpreted_intent
    db.add(
        FounderIntentCorrection(
            owner_id=owner_id,
            binding_id=row.id,
            prior_intent=prior,
            corrected_intent=corrected_intent,
            wrong_terminology=wrong_terminology,
            reason=reason,
            prior_entity_id=row.canonical_entity_id,
            corrected_entity_id=canonical_entity_id or row.canonical_entity_id,
        )
    )
    row.interpreted_intent = corrected_intent
    if canonical_entity_id is not None:
        row.canonical_entity_id = canonical_entity_id
    row.confidence = max(float(row.confidence or 0), 0.85)
    row.last_seen_at = datetime.utcnow()
    db.flush()
    return row


def resolve_with_learned_intent(
    db: Session,
    *,
    owner_id: uuid.UUID,
    raw_expression: str,
    context: dict | None = None,
    persist: bool = True,
    idempotency_key: str | None = None,
) -> IntentResolution:
    """Resolve founder phrasing using learned bindings first, then Stage G heuristics.

    Low-risk ambiguity may auto-resolve via a prior binding. Consequential ambiguity surfaces
    (`must_surface=True`) and does not claim authority.
    """
    intent, base_conf = resolve_founder_expression(raw_expression)
    learned = get_binding_by_phrase(db, owner_id=owner_id, raw_expression=raw_expression)
    auto_resolved = False
    confidence = base_conf
    entity_id = None
    binding_id = None
    corrections: list[dict] = []
    trigger = None

    if learned is not None:
        intent = learned.interpreted_intent
        confidence = max(base_conf, float(learned.confidence or 0))
        entity_id = learned.canonical_entity_id
        binding_id = learned.id
        trigger = learned.retrieval_trigger or f"phrase:{learned.phrase_normalized}"
        auto_resolved = True
        corrections = [
            {
                "prior": c.prior_intent,
                "corrected": c.corrected_intent,
                "reason": c.reason,
                "wrong_terminology": c.wrong_terminology,
                "at": c.created_at.isoformat() if c.created_at else None,
            }
            for c in list_corrections(db, owner_id=owner_id, binding_id=learned.id)
        ]
        # Reinforce on use
        if persist:
            learned.hit_count = int(learned.hit_count or 0) + 1
            learned.last_seen_at = datetime.utcnow()
            db.flush()

    ambiguity = classify_ambiguity(raw_expression, residual_intent=intent, confidence=confidence)
    # If we auto-resolved via learned binding, downgrade LOW_RISK ambiguity.
    if auto_resolved and ambiguity == AmbiguityClass.LOW_RISK:
        ambiguity = AmbiguityClass.NONE
    must_surface = ambiguity == AmbiguityClass.CONSEQUENTIAL and not auto_resolved

    if persist and binding_id is None:
        key = idempotency_key or f"intent:{owner_id}:{_phrase_key(raw_expression)}:{uuid.uuid4()}"
        binding = record_intent_binding(
            db,
            owner_id=owner_id,
            raw_expression=raw_expression,
            interpreted_intent=intent,
            canonical_entity_id=entity_id,
            confidence=confidence,
            context=context,
            retrieval_trigger=trigger or f"raw:{_phrase_key(raw_expression)}",
            idempotency_key=key,
        )
        binding_id = binding.id
        trigger = binding.retrieval_trigger

    return IntentResolution(
        raw_expression=raw_expression,
        interpreted_intent=intent,
        canonical_entity_id=entity_id,
        confidence=confidence,
        context=dict(context or {}),
        binding_id=binding_id,
        correction_history=corrections,
        retrieval_trigger=trigger,
        ambiguity=ambiguity,
        must_surface=must_surface,
        auto_resolved=auto_resolved,
        authority_claimed=False,
    )
