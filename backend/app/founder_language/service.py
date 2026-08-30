"""Stage G — real founder-language vertical slice (no manual test-side translation)."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.concept_reconciliation import classify_against_corpus, reconcile_and_promote_idea
from app.inspectable_memory import founder_add_memory_note
from app.memory_work_linkage import TimingClass, apply_memory_work_linkage, find_affected_work
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.knowledge_claim import KnowledgeClaim
from app.project_entities import record_interpretation_proposal

_FILLER = re.compile(
    r"\b(få med det här med|gör samma på|den där grejen|grejen|den andra|förut|fixade)\b",
    re.IGNORECASE,
)


@dataclass
class FounderLanguageResult:
    raw_expression: str
    normalized_intent: str
    confidence: float
    memory_note_id: uuid.UUID | None = None
    canonical_entity_id: uuid.UUID | None = None
    linkage_thread_id: uuid.UUID | None = None
    affected_work_ids: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    persisted: bool = False


def resolve_founder_expression(raw: str) -> tuple[str, float]:
    """Deterministic intent extraction from messy founder Swedish/English.

    Strips filler phrases; does not invent component names. Low confidence when the residual
    text is empty or extremely short.
    """
    text = (raw or "").strip()
    cleaned = _FILLER.sub(" ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .,!?:;")
    if not cleaned:
        # Keep original tokens minus pure filler leftovers.
        cleaned = re.sub(r"\s+", " ", text).strip()
    confidence = 0.9 if len(cleaned.split()) >= 3 else 0.55 if cleaned else 0.2
    return cleaned, confidence


def process_founder_language(
    db: Session,
    *,
    owner_id: uuid.UUID,
    raw_expression: str,
    idempotency_key: str,
    timing: TimingClass = TimingClass.NOW,
) -> FounderLanguageResult:
    """End-to-end: raw expression → intent → memory → concept reconcile → work linkage → verify.

    No manual test-side memory/task translation after input — this function is the production path.
    """
    intent, confidence = resolve_founder_expression(raw_expression)
    # RAW EXPRESSION != INTERPRETATION: store founder words verbatim in content/source;
    # keep normalized intent + confidence in provenance (and return fields).
    note, _claim = founder_add_memory_note(
        db,
        owner_id=owner_id,
        content=raw_expression,
        note_type="observation",
        idempotency_key=idempotency_key,
        source=raw_expression,
        provenance={
            "stage": "G",
            "raw_expression": raw_expression,
            "normalized_intent": intent,
            "confidence": confidence,
        },
    )

    # Persist as knowledge claim → interpretation → reconcile (may SAME-collapse).
    document = Document(
        title="founder-language",
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=ActiveTruthStatus.active,
    )
    db.add(document)
    db.flush()
    claim = KnowledgeClaim(
        owner_id=owner_id,
        source_id=document.id,
        claim_text=intent,
        extraction_version="founder_language_v1",
    )
    db.add(claim)
    db.flush()
    proposal = record_interpretation_proposal(
        db,
        owner_id=owner_id,
        source_claim_id=claim.id,
        proposed_entity_type="idea",
        idempotency_key=f"fl-prop:{idempotency_key}",
    )
    db.flush()
    classify = classify_against_corpus(db, owner_id=owner_id, title=intent)
    reconcile = reconcile_and_promote_idea(
        db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        title=intent,
        entity_idempotency_key=f"fl-ent:{idempotency_key}",
    )
    linkage = apply_memory_work_linkage(
        db,
        owner_id=owner_id,
        note_id=note.id,
        timing=timing,
        park_candidate=True,
    )
    affected = find_affected_work(db, owner_id=owner_id, text=intent, note_id=note.id)

    # Verify persistence: note still readable.
    from app.founder_memory import get_founder_memory

    persisted = get_founder_memory(db, owner_id=owner_id, note_id=note.id) is not None

    return FounderLanguageResult(
        raw_expression=raw_expression,
        normalized_intent=intent,
        confidence=confidence,
        memory_note_id=note.id,
        canonical_entity_id=reconcile.canonical_entity_id,
        linkage_thread_id=linkage.thread_id,
        affected_work_ids=[f"{a.kind}:{a.id}" for a in affected[:20]],
        provenance={
            "classification_hits": [
                {"entity_id": str(h.entity_id), "relationship": h.relationship_type, "score": h.score}
                for h in classify[:5]
            ],
            "reconcile_outcome": reconcile.outcome,
            "linkage_actions": [a.value for a in linkage.actions],
        },
        persisted=persisted,
    )
