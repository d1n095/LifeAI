import uuid
from dataclasses import dataclass, field

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
from app.models.knowledge_claim import ClaimConfidence, KnowledgeClaim
from app.models.source_relationship import RelationshipType, SourceRelationship

HIGH_THRESHOLD = 0.75
MEDIUM_THRESHOLD = 0.55

NON_ACTIVE_STATUSES = {"historical", "proposed", "superseded", "disputed"}


@dataclass
class TrustAssessment:
    level: str  # "high" | "medium" | "low" | "none"
    score: float
    # Founder Knowledge Studio v1 additions (see docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md) — the
    # top-matching source's own declared epistemic status, and whether any two sources
    # actually retrieved for this answer are known (via a "contradicts" SourceRelationship)
    # to disagree with each other. Both are machine-readable, not just baked into the prose
    # instruction string, so a caller (or a future UI) can act on them directly.
    top_source_status: str | None = None
    conflicts_detected: bool = False
    conflicting_document_ids: list[tuple[str, str]] = field(default_factory=list)


def assess_confidence(hits: list[dict], conflicting_pairs: list[tuple[str, str]] | None = None) -> TrustAssessment:
    """Confidence is derived from the strongest retrieved source, not from the model itself —
    an LLM cannot reliably self-report how well-grounded its own answer is, but a similarity
    score against the actual knowledge base is a concrete, checkable signal.

    A source's own active_truth_status now participates directly: a chunk that matches the
    query extremely well is still, by the founder's own classification, historical/proposed/
    superseded/disputed material — MainAI must never let a strong similarity score alone
    manufacture "high" confidence out of content the founder has explicitly marked as not
    currently authoritative. See app/models/document.py's ActiveTruthStatus docstring.
    """
    if not hits:
        return TrustAssessment(level="none", score=0.0)

    top_hit = max(hits, key=lambda h: h["score"])
    top_score = top_hit["score"]
    top_status = top_hit.get("active_truth_status")

    if top_score >= HIGH_THRESHOLD:
        level = "high"
    elif top_score >= MEDIUM_THRESHOLD:
        level = "medium"
    else:
        level = "low"

    if top_status in NON_ACTIVE_STATUSES and level == "high":
        level = "medium"

    conflicting_pairs = conflicting_pairs or []
    return TrustAssessment(
        level=level,
        score=top_score,
        top_source_status=top_status,
        conflicts_detected=bool(conflicting_pairs),
        conflicting_document_ids=conflicting_pairs,
    )


def detect_conflicts(db: Session, owner_id: uuid.UUID, document_ids: list[str]) -> list[tuple[str, str]]:
    """Structural conflict detection: two sources actually retrieved for this answer that
    are linked by a `contradicts` SourceRelationship (see app/models/source_relationship.py).
    Deliberately NOT an attempt to detect disagreement from the chunk text itself — that
    would require real NLP/LLM judgement this function has no access to and shouldn't
    fabricate; a `contradicts` edge only exists because the founder (or a future workbench
    analysis, see DEL 9) explicitly recorded it. No edge recorded means no conflict is
    reported, even if two retrieved chunks happen to disagree in fact — that's a known,
    honestly-scoped limitation, not a silent claim of completeness."""
    if len(document_ids) < 2:
        return []
    ids = [uuid.UUID(d) for d in set(document_ids)]
    rows = (
        db.query(SourceRelationship)
        .filter(
            SourceRelationship.owner_id == owner_id,
            SourceRelationship.relationship_type == RelationshipType.contradicts,
            SourceRelationship.from_source_id.in_(ids),
            SourceRelationship.to_source_id.in_(ids),
        )
        .all()
    )
    return [(str(r.from_source_id), str(r.to_source_id)) for r in rows]


# Appended to the system prompt based on confidence — this is the actual enforcement
# mechanism (a prompt instruction), not just a cosmetic label in the response. It cannot
# guarantee the model complies, but it makes uncertainty the explicit default framing
# instead of something the model has to volunteer on its own.
TRUST_INSTRUCTIONS: dict[str, str] = {
    "high": (
        "Underlaget från kunskapsbiblioteket är starkt (hög likhet med frågan). Svara normalt "
        "utifrån källorna, men ange alltid vilka källor du bygger svaret på."
    ),
    "medium": (
        "Underlaget från kunskapsbiblioteket är begränsat (måttlig likhet med frågan). Du får "
        "svara, men måste tydligt markera vilka delar av svaret som är osäkra eller bygger på "
        "ofullständig information — skriv t.ex. 'Detta är osäkert utifrån tillgängligt underlag'."
    ),
    "low": (
        "Underlaget från kunskapsbiblioteket är svagt (låg likhet med frågan). Du FÅR INTE "
        "presentera gissningar som fakta. Säg uttryckligen att underlaget är otillräckligt för "
        "att svara säkert, och skilj tydligt mellan det du faktiskt vet från källorna och det "
        "du inte vet."
    ),
    "none": (
        "Ingen relevant källa hittades i kunskapsbiblioteket. Du FÅR INTE hitta på ett "
        "faktapåstående och presentera det som om det kom från företagets kunskap. Säg tydligt "
        "att du saknar underlag i kunskapsbiblioteket för denna fråga. Om du ändå svarar utifrån "
        "allmän kunskap, markera explicit att svaret INTE kommer från företagets egna källor."
    ),
}

STATUS_INSTRUCTIONS: dict[str, str] = {
    "historical": (
        "OBS: Den starkast matchande källan är märkt som HISTORISK material av grundaren — "
        "den beskriver ett tidigare läge, inte nödvändigtvis hur det ser ut nu. Presentera "
        "aldrig historiskt material som aktuell sanning; säg uttryckligen att källan är "
        "historisk och att läget kan ha ändrats."
    ),
    "proposed": (
        "OBS: Den starkast matchande källan är märkt som ETT FÖRSLAG av grundaren, inte ett "
        "beslut. Presentera den aldrig som om den redan gäller — säg uttryckligen att det är "
        "ett förslag som inte (eller inte nödvändigtvis) är beslutat."
    ),
    "superseded": (
        "OBS: Den starkast matchande källan är märkt som ERSATT av grundaren — ett nyare "
        "beslut eller dokument har tagit dess plats. Presentera den aldrig som gällande utan "
        "att säga att den är ersatt och att svaret kan vara inaktuellt."
    ),
    "disputed": (
        "OBS: Den starkast matchande källan är märkt som OMTVISTAD av grundaren — dess "
        "innehåll är inte bekräftat eller är ifrågasatt. Presentera den aldrig som säker "
        "fakta; säg uttryckligen att källan är omtvistad."
    ),
}

CONFLICT_INSTRUCTION = (
    "OBS: Minst två av de hämtade källorna är märkta som varandras MOTSÄGELSER av grundaren "
    "(en 'contradicts'-relation). Dölj aldrig detta — säg uttryckligen att källorna motsäger "
    "varandra, beskriv båda ståndpunkterna separat, och gissa inte vilken som är korrekt."
)


def detect_claim_conflicts(db: Session, owner_id: uuid.UUID, chunk_ids: list[str]) -> bool:
    """Claim-level analogue of detect_conflicts() above: True if ANY claim bound to one of
    the actually-retrieved chunks currently resolves to `conflict` (assess_claim_confidence
    below). This can flag a conflict detect_conflicts() alone would miss — two claims can
    contradict each other even when their SOURCE documents have no `contradicts`
    SourceRelationship recorded between them (e.g. two otherwise-agreeing documents that
    happen to state one fact differently). Used by app/routers/chat.py to OR into
    ChatMessageOut's conflicts_detected, not to replace the source-level check."""
    if not chunk_ids:
        return False
    ids = [uuid.UUID(c) for c in set(chunk_ids)]
    claims = db.query(KnowledgeClaim).filter(KnowledgeClaim.owner_id == owner_id, KnowledgeClaim.chunk_id.in_(ids)).all()
    return any(assess_claim_confidence(db, claim) == ClaimConfidence.conflict for claim in claims)


def assess_claim_confidence(db: Session, claim: KnowledgeClaim) -> ClaimConfidence:
    """STEG 10's claim-level analogue of assess_confidence() above: the stored,
    extraction-time confidence (app/rag/claims.py's grounding-score heuristic) is only the
    starting point — this recomputes the CURRENT confidence from the claim's
    claim_relationships every time it's asked, so a relationship added after extraction
    (e.g. the founder or a later import creates a `contradicts`/`supports` edge) is reflected
    immediately without needing to re-run extraction.

    Any `contradicts` edge caps the result at `conflict`, unconditionally — a claim is never
    "certain" no matter how much independent corroboration it has if it's also known to be
    disputed by another claim. This mirrors build_trust_instructions()'s source-level rule
    that a detected conflict is always surfaced, never silently outvoted.

    A claim starting at `no_basis` (extraction-time grounding score too low — see
    app/rag/claims.py's _confidence_for_score) can never be rescued into a higher bucket by
    corroboration: weak textual grounding in its OWN source chunk is a property of the claim
    itself, not something another claim agreeing with it can fix — two ungrounded claims
    agreeing with each other is not evidence.

    `likely` is promoted to `certain` only by INDEPENDENT corroboration — a `supports` edge
    from a claim whose source_id differs from this claim's own. A source repeating itself
    (e.g. two chunks of the same document both stating the same fact) is not independent
    corroboration and must never count toward "certain".
    """
    contradicting = (
        db.query(ClaimRelationship)
        .filter(
            ClaimRelationship.owner_id == claim.owner_id,
            ClaimRelationship.relationship_type == ClaimRelationshipType.contradicts,
            or_(ClaimRelationship.from_claim_id == claim.id, ClaimRelationship.to_claim_id == claim.id),
        )
        .first()
    )
    if contradicting is not None:
        return ClaimConfidence.conflict

    if claim.confidence == ClaimConfidence.no_basis:
        return ClaimConfidence.no_basis

    if claim.confidence != ClaimConfidence.likely:
        return claim.confidence

    supporting_rows = (
        db.query(ClaimRelationship, KnowledgeClaim.source_id)
        .join(KnowledgeClaim, KnowledgeClaim.id == ClaimRelationship.from_claim_id)
        .filter(
            ClaimRelationship.owner_id == claim.owner_id,
            ClaimRelationship.relationship_type == ClaimRelationshipType.supports,
            ClaimRelationship.to_claim_id == claim.id,
        )
        .all()
    )
    independent_sources = {source_id for _, source_id in supporting_rows if source_id != claim.source_id}
    return ClaimConfidence.certain if independent_sources else claim.confidence


def build_trust_instructions(
    level: str, top_source_status: str | None = None, conflicts_detected: bool = False
) -> str:
    parts = [TRUST_INSTRUCTIONS.get(level, TRUST_INSTRUCTIONS["none"])]
    if top_source_status in STATUS_INSTRUCTIONS:
        parts.append(STATUS_INSTRUCTIONS[top_source_status])
    if conflicts_detected:
        parts.append(CONFLICT_INSTRUCTION)
    return "\n\n".join(parts)
