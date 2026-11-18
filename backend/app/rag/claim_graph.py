"""M3 (Round 8): claim-graph REASONING services — traceability, shift detection, and the
founder's claim-relationship writer. Built on the existing owner-scoped, cycle-safe graph
primitives (graph_traverse + second_order_consequences) instead of duplicating them.

Three capabilities, each answering a question the mission names:

  trace_evidence_chain()   "Trace conclusions back to their supporting evidence." One
                           structured, bounded walk from a claim to EVERYTHING its verdict
                           rests on: the ladder verdict with its reasons, the source's own
                           trust state, the chunk grounding, BOTH sides of every incident
                           contradicts edge (a contradiction is reported as a conflict,
                           never a winner), the stamped study metadata verdict, and the
                           second-order consequences touching this claim's source.

  detect_epistemic_shift() "Detect when new information changes an earlier conclusion."
                           Called when a contradicts edge is created: classifies the two
                           endpoint claims WITH the edge (live) and WITHOUT it
                           (counterfactual, classify_claim's ignore_relationship_id) and
                           reports exactly which verdicts moved. The caller records the
                           shift on the audit trail — the old conclusion is never
                           overwritten, it is superseded WITH its provenance.

  record_claim_relationship()  The production WRITER for claim_relationships — until this
                           existed the claim graph had no input path at all (only tests
                           created edges), so "connect relevant claims across domains"
                           could never happen in a real session. Owner-scoped twice (both
                           endpoint claims must be visible to the caller) + RLS + the
                           self-relationship and duplicate-edge guards. Type vocabulary is
                           the table's own enum; an unknown type is refused, not guessed.

Anti-inflation rules that hold across all three: no traversal follows a relationship type
that could raise a verdict (supports edges inform the confidence bucket, never the ladder);
contradictions are surfaced symmetrically; every structure returned names its evidence so
a reviewer can disagree with the reasoning while keeping the trail.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
from app.models.document import Document
from app.models.knowledge_claim import KnowledgeClaim
from app.rag.epistemics import EpistemicVerdict, classify_claim
from app.rag.evidence_metadata import cap_from_row, quality_metadata_for_claims
from app.rag.trust import assess_claim_confidence

_VALID_RELATIONSHIP_TYPES = {t.value for t in ClaimRelationshipType}


@dataclass
class EpistemicShift:
    """One claim's verdict movement caused by ONE new contradicts edge. `prior` is the
    counterfactual (edge excluded), `current` is live — both with their own reasons, so
    the audit row shows WHY the conclusion changed, not just that it did."""

    claim_id: uuid.UUID
    claim_text: str
    relationship_id: uuid.UUID
    prior_status: str
    current_status: str
    prior_reasons: list[str]
    current_reasons: list[str]

    @property
    def changed(self) -> bool:
        return self.prior_status != self.current_status


@dataclass
class EvidenceChain:
    """The full traceability record for one claim. Everything here is live-recomputed or
    stamped-with-provenance — nothing is trusted because it was said before."""

    claim_id: uuid.UUID
    claim_text: str
    claim_type: str
    claim_status: str
    verdict: EpistemicVerdict
    confidence: str
    source: dict
    chunk_grounded: bool
    quality_metadata: dict | None
    supporting_edges: list[dict] = field(default_factory=list)
    contradicting_edges: list[dict] = field(default_factory=list)
    other_edges: list[dict] = field(default_factory=list)  # supersedes / duplicates — recorded, not adjudicated
    second_order_consequences: list[dict] = field(default_factory=list)


def record_claim_relationship(
    db: Session,
    owner_id: uuid.UUID,
    *,
    from_claim_id: uuid.UUID,
    to_claim_id: uuid.UUID,
    relationship_type: str,
    note: str | None = None,
) -> tuple[ClaimRelationship, list[EpistemicShift]]:
    """Create one claim_graph edge (owner-scoped) and detect what it changes.

    Fail-closed validation: both endpoint claims must be visible to THIS owner (a claim
    from another owner is a 404-shaped LookupError, never a cross-owner edge); the type
    must be in the closed vocabulary; a claim cannot relate to itself; an exact duplicate
    edge returns the EXISTING row with an empty shift list (idempotent replays never
    double-count contradictions — the ladder counts edge EXISTENCE, not edge copies).

    The shifts are computed INSIDE the caller's transaction (before commit): each
    endpoint's live verdict vs its counterfactual without this edge. Only a `contradicts`
    edge can move a ladder verdict; for the other types the shift list is empty by design
    — supports/derives edges feed the confidence bucket, and attributing a verdict change
    to them would manufacture causality."""
    if relationship_type not in _VALID_RELATIONSHIP_TYPES:
        raise ValueError(
            f"unknown relationship_type '{relationship_type}'; valid: {', '.join(sorted(_VALID_RELATIONSHIP_TYPES))}"
        )
    if from_claim_id == to_claim_id:
        raise ValueError("a claim cannot relate to itself — that is circularity, not evidence")

    from_claim = (
        db.query(KnowledgeClaim).filter(KnowledgeClaim.id == from_claim_id, KnowledgeClaim.owner_id == owner_id).first()
    )
    to_claim = (
        db.query(KnowledgeClaim).filter(KnowledgeClaim.id == to_claim_id, KnowledgeClaim.owner_id == owner_id).first()
    )
    if from_claim is None or to_claim is None:
        raise LookupError("one or both claims are missing or not visible to this owner")

    existing = (
        db.query(ClaimRelationship)
        .filter(
            ClaimRelationship.owner_id == owner_id,
            ClaimRelationship.from_claim_id == from_claim_id,
            ClaimRelationship.to_claim_id == to_claim_id,
            ClaimRelationship.relationship_type == ClaimRelationshipType(relationship_type),
        )
        .first()
    )
    if existing is not None:
        return existing, []

    edge = ClaimRelationship(
        owner_id=owner_id,
        from_claim_id=from_claim_id,
        to_claim_id=to_claim_id,
        relationship_type=ClaimRelationshipType(relationship_type),
        note=note,
    )
    db.add(edge)
    db.flush()  # the shift detection below classifies WITH this edge present

    shifts: list[EpistemicShift] = []
    if edge.relationship_type is ClaimRelationshipType.contradicts:
        for claim in (from_claim, to_claim):
            current = classify_claim(db, owner_id, claim)
            prior = classify_claim(db, owner_id, claim, ignore_relationship_id=edge.id)
            shifts.append(
                EpistemicShift(
                    claim_id=claim.id,
                    claim_text=claim.claim_text,
                    relationship_id=edge.id,
                    prior_status=prior.status.value,
                    current_status=current.status.value,
                    prior_reasons=prior.reasons,
                    current_reasons=current.reasons,
                )
            )
    return edge, shifts


def trace_evidence_chain(db: Session, owner_id: uuid.UUID, claim_id: uuid.UUID) -> EvidenceChain:
    """The bounded, structured answer to "why does MainAI believe/dispute this?".

    Bounded by construction: one claim, its source, one chunk, its incident edges
    (complementary index), its stamped metadata row, and the second-order consequences of
    ONE source root — no unbounded walk, no N+1 (edges and metadata are batch-fetched).
    Contradicting edges are listed BOTH sides with their texts — the chain presents the
    conflict, it does not adjudicate it."""
    claim = (
        db.query(KnowledgeClaim).filter(KnowledgeClaim.id == claim_id, KnowledgeClaim.owner_id == owner_id).first()
    )
    if claim is None:
        raise LookupError(f"claim {claim_id} not found for this owner")

    verdict = classify_claim(db, owner_id, claim)
    source = db.query(Document).filter(Document.id == claim.source_id, Document.uploaded_by == owner_id).first()

    # M4 hardening: bounded even for adversarial data — a claim with an absurd number of
    # incident edges degrades to a bounded trace, never an unbounded response. 200 is far
    # above any real claim's edge count and matches the graph surfaces' LIMIT discipline.
    incident = (
        db.query(ClaimRelationship)
        .filter(
            ClaimRelationship.owner_id == owner_id,
            (ClaimRelationship.from_claim_id == claim.id) | (ClaimRelationship.to_claim_id == claim.id),
        )
        .limit(200)
        .all()
    )
    other_ids = {e.from_claim_id if e.to_claim_id == claim.id else e.to_claim_id for e in incident}
    others = (
        db.query(KnowledgeClaim)
        .filter(KnowledgeClaim.owner_id == owner_id, KnowledgeClaim.id.in_(other_ids or {uuid.uuid4()}))
        .all()
    )
    other_text = {o.id: o.claim_text for o in others}

    supporting: list[dict] = []
    contradicting: list[dict] = []
    other: list[dict] = []
    for e in incident:
        counterpart = e.to_claim_id if e.from_claim_id == claim.id else e.from_claim_id
        record = {
            "relationship_id": str(e.id),
            "type": e.relationship_type.value,
            "other_claim_id": str(counterpart),
            "other_claim_text": other_text.get(counterpart),
            "note": e.note,
        }
        if e.relationship_type is ClaimRelationshipType.contradicts:
            contradicting.append(record)
        elif e.relationship_type is ClaimRelationshipType.supports:
            supporting.append(record)
        else:
            other.append(record)

    metadata_rows = quality_metadata_for_claims(db, owner_id, [claim.id])
    row = metadata_rows.get(claim.id)
    quality = (
        {
            "grade": row.quality_grade,
            "cap": row.quality_cap,
            "caveats": list(row.quality_caveats),
            "declared_via": row.declared_via,
            "recorded_at": row.recorded_at.isoformat(),
        }
        if row is not None
        else None
    )

    from app.rag.epistemics import second_order_consequences

    consequences = (
        second_order_consequences(db, owner_id, [claim.source_id], max_depth=2) if source is not None else []
    )

    return EvidenceChain(
        claim_id=claim.id,
        claim_text=claim.claim_text,
        claim_type=claim.claim_type.value,
        claim_status=claim.status.value,
        verdict=verdict,
        confidence=assess_claim_confidence(db, claim).value,
        source=(
            {
                "source_id": str(source.id),
                "title": source.title,
                "active_truth_status": source.active_truth_status,
                "deleted": source.deleted_at is not None,
            }
            if source is not None
            else {"source_id": str(claim.source_id), "title": None, "active_truth_status": None, "deleted": True}
        ),
        chunk_grounded=claim.chunk_id is not None,
        quality_metadata=quality,
        supporting_edges=supporting,
        contradicting_edges=contradicting,
        other_edges=other,
        second_order_consequences=[
            {
                "kind": c.kind,
                "trigger_edge": [str(x) for x in c.trigger_edge],
                "dependent_source_id": str(c.dependent_source_id),
                "depth": c.depth,
                "path": [str(p) for p in c.path],
            }
            for c in consequences
        ],
    )


def shifts_to_audit_detail(shifts: list[EpistemicShift]) -> str:
    """The audit-line rendering for an epistemic shift (the caller writes the AuditLog row):
    prior -> current for each moved verdict, with the unchanged ones named as unchanged.
    A shift where prior == current is still RECORDED information (the new edge did not move
    the verdict — e.g. the claim was already disputed), never dropped as uninteresting."""
    if not shifts:
        return "no ladder verdicts were in play for this edge"
    parts = []
    for s in shifts:
        moved = "CHANGED" if s.changed else "unchanged"
        parts.append(f"claim {s.claim_id}: {s.prior_status} -> {s.current_status} ({moved})")
    return "; ".join(parts)
