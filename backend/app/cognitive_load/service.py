"""Stage P — cognitive load reduction (search before asking founder)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.founder_memory import list_founder_memory
from app.personal_intent import AmbiguityClass, resolve_with_learned_intent
from app.temporal_intelligence import RecapWindow, build_recap


@dataclass
class FounderAskDecision:
    should_ask_founder: bool
    inferred_answer: str | None = None
    decision_prompt: str | None = None
    tradeoff: str | None = None
    avoided_question: bool = False
    evidence_refs: list[dict] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def consider_founder_question(
    db: Session,
    *,
    owner_id: uuid.UUID,
    question: str,
) -> FounderAskDecision:
    """Before asking founder: search memory/history/plans. Ask only for true decisions."""
    metrics = {
        "unnecessary_questions_avoided": 0,
        "duplicate_explanations_avoided": 0,
        "manual_context_reload_avoided": 0,
    }
    resolution = resolve_with_learned_intent(
        db, owner_id=owner_id, raw_expression=question, persist=True, idempotency_key=f"p:{uuid.uuid4()}"
    )
    # Search durable memory for a direct answer
    q = (question or "").lower()
    notes = list_founder_memory(db, owner_id=owner_id, status="active")
    matches = [n for n in notes if any(tok in (n.content or "").lower() for tok in q.split() if len(tok) > 3)]
    recap = build_recap(db, owner_id=owner_id, window=RecapWindow.WEEK, include_project_wide=False, limit=20)

    if resolution.must_surface or resolution.ambiguity == AmbiguityClass.CONSEQUENTIAL:
        return FounderAskDecision(
            should_ask_founder=True,
            decision_prompt=question,
            tradeoff="Consequential ambiguity — irreversible effect risk.",
            evidence_refs=[{"kind": "intent_binding", "id": str(resolution.binding_id)}] if resolution.binding_id else [],
            metrics=metrics,
        )

    if matches:
        metrics["unnecessary_questions_avoided"] = 1
        metrics["manual_context_reload_avoided"] = 1
        return FounderAskDecision(
            should_ask_founder=False,
            inferred_answer=matches[0].content,
            avoided_question=True,
            evidence_refs=[{"kind": "founder_memory_note", "id": str(matches[0].id)}],
            metrics=metrics,
        )

    if resolution.auto_resolved and resolution.confidence >= 0.7:
        metrics["unnecessary_questions_avoided"] = 1
        metrics["duplicate_explanations_avoided"] = 1
        return FounderAskDecision(
            should_ask_founder=False,
            inferred_answer=resolution.interpreted_intent,
            avoided_question=True,
            evidence_refs=[{"kind": "intent_binding", "id": str(resolution.binding_id)}],
            metrics=metrics,
        )

    # Default: surface a narrow decision if question looks like a choice
    if any(tok in q for tok in ("ska vi", "should we", "eller", "or ", "?")):
        return FounderAskDecision(
            should_ask_founder=True,
            decision_prompt=question,
            tradeoff="No durable answer found; founder decision required.",
            evidence_refs=[{"kind": "recap_items", "count": len(recap.items)}],
            metrics=metrics,
        )

    metrics["unnecessary_questions_avoided"] = 1
    return FounderAskDecision(
        should_ask_founder=False,
        inferred_answer=resolution.interpreted_intent or None,
        avoided_question=True,
        metrics=metrics,
    )
