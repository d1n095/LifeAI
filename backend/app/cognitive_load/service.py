"""Stage P — cognitive load reduction (search before asking founder)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.founder_memory import list_current_founder_memory, list_founder_memory
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


def _select_current_founder_truth(matches: list) -> object:
    """Canonical current selection — never rely on incidental DB / list order.

    Among active matches: prefer tip of supersession chain, then newest
    (observed_at, created_at, id). Superseded notes must not be selected as current.
    """
    if not matches:
        raise ValueError("no matches")
    ids = {n.id for n in matches}
    superseded_targets = {
        n.supersedes_note_id for n in matches if getattr(n, "supersedes_note_id", None) is not None
    }
    # Drop notes that another match explicitly supersedes (belt if status lag).
    candidates = [n for n in matches if n.id not in superseded_targets]
    if not candidates:
        candidates = list(matches)
    # Prefer corrections / explicit supersession tips, then temporal order.
    def _key(n):
        is_correction = 1 if (getattr(n, "note_type", None) == "correction" or getattr(n, "supersedes_note_id", None)) else 0
        observed = getattr(n, "observed_at", None) or getattr(n, "created_at", None)
        created = getattr(n, "created_at", None) or observed
        return (is_correction, observed, created, str(n.id))

    return max(candidates, key=_key)


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
    notes = list_current_founder_memory(db, owner_id=owner_id)
    # Also consider active notes that may not yet be tip-of-chain filtered the same way —
    # union with status=active listing, then apply deterministic current selection.
    active = list_founder_memory(db, owner_id=owner_id, status="active")
    by_id = {n.id: n for n in active}
    for n in notes:
        by_id[n.id] = n
    pool = list(by_id.values())
    matches = [n for n in pool if any(tok in (n.content or "").lower() for tok in q.split() if len(tok) > 3)]
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
        current = _select_current_founder_truth(matches)
        metrics["unnecessary_questions_avoided"] = 1
        metrics["manual_context_reload_avoided"] = 1
        return FounderAskDecision(
            should_ask_founder=False,
            inferred_answer=current.content,
            avoided_question=True,
            evidence_refs=[{"kind": "founder_memory_note", "id": str(current.id)}],
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
