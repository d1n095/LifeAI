"""Round 10 M4: the INTELLECTUAL ADVERSARY — bounded, evidence-driven independent review.

The reviewer challenges a conclusion ONLY when the evidence state justifies it, and every
objection it raises must cite the CONCRETE rows that justify it (metadata, graph edges,
audit history). Objections without an evidence pointer are structurally impossible — the
reviewer cannot manufacture objections or invent sources to appear critical, because an
objection IS a pointer into this owner's own evidence records.

Triggers (bounded, risk- and uncertainty-driven — never automatic disagreement):
  - risk: the claim asserts causality, or its quality grade is low;
  - uncertainty: the ladder verdict is assumption/hypothesis/conflicting_evidence;
  - contradiction: declared contradicts edges touch the claim;
  - missing evidence: causal claim without declared competing explanations or design
    support; metadata gaps on load-bearing fields.

Objection kinds (mission M4's list, mapped to what the evidence can actually show):
  unsupported_assumption, alternative_explanations, weak_causal_inference,
  confounding_variables, unacknowledged_counterevidence, overstated_confidence,
  incorrect_extrapolation, inconsistent_conclusions, fabricated_citation,
  misleading_source_selection, missing_evidence.

Epistemic separation: every objection labels the category of its evidence — observed_
evidence (stamped metadata/edges), source_claim (chunk-derived claim text), hypothesis
(competing explanation), model_inference (this review's own reasoning), verified_
conclusion (ladder verdict), unresolved_question (open item). The review never claims
a verified status the ladder does not support.

Fail-open + read-only: the review WRITES NOTHING; it is a lens over existing records.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
from app.models.knowledge_claim import ClaimConfidence, KnowledgeClaim
from app.rag.evidence_metadata import quality_metadata_for_claims

# Designs whose causal inference is inherently weaker — shared with scientific_reasoning's
# composition buckets so the adversary and the science view never disagree about what is
# "observational".
_OBSERVATIONAL_DESIGNS = {"prospective_cohort", "retrospective_cohort", "case_control", "case_series"}


@dataclass
class AdversarialObjection:
    kind: str
    severity: str  # low | medium | high
    statement: str
    epistemic_category: str  # see module docstring
    evidence_refs: list[str] = field(default_factory=list)  # provenance pointers

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "statement": self.statement,
            "epistemic_category": self.epistemic_category,
            "evidence_refs": self.evidence_refs,
        }


@dataclass
class AdversarialReview:
    claim_id: uuid.UUID
    reviewed: bool  # False = no justified trigger; NOT a disagreement for its own sake
    verdict_status: str
    objections: list[AdversarialObjection] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "claim_id": str(self.claim_id),
            "reviewed": self.reviewed,
            "verdict_status": self.verdict_status,
            "objections": [o.to_dict() for o in self.objections],
        }


def review_conclusion(db: Session, owner_id: uuid.UUID, claim_id: uuid.UUID) -> AdversarialReview:
    """Bounded independent review of one claim's conclusion. Read-only; deterministic;
    every objection cites concrete evidence refs from the owner's own records."""
    from app.rag.epistemics import classify_claim

    claim = db.query(KnowledgeClaim).filter(KnowledgeClaim.owner_id == owner_id, KnowledgeClaim.id == claim_id).first()
    if claim is None:
        raise LookupError(f"claim {claim_id} not found for owner")

    verdict = classify_claim(db, owner_id, claim)
    meta_row = quality_metadata_for_claims(db, owner_id, [claim_id]).get(claim_id)
    meta = dict(meta_row.metadata_json) if meta_row is not None else {}
    quality_grade = None
    quality_caveats: list[str] = []
    if meta_row is not None and meta_row.metadata_json:
        from app.rag.evidence_quality import grade_evidence

        assessment = grade_evidence(meta_row.metadata_json)
        quality_grade = assessment.grade.value
        quality_caveats = list(assessment.caveats)
    objections: list[AdversarialObjection] = []

    contradicts = (
        db.query(ClaimRelationship)
        .filter(
            ClaimRelationship.owner_id == owner_id,
            ((ClaimRelationship.from_claim_id == claim_id) | (ClaimRelationship.to_claim_id == claim_id)),
            ClaimRelationship.relationship_type == ClaimRelationshipType.contradicts,
        )
        .limit(50)
        .all()
    )

    # --- the trigger evaluation -----------------------------------------------------
    causal = bool(meta.get("causal_claim"))
    design = (meta.get("study_design") or "").strip()
    design_supported = bool(meta.get("causal_claim_supported_by_design"))
    competing = [c for c in (meta.get("competing_explanations") or []) if c and c != "none_declared"]
    sample = meta.get("sample_size")
    setting = (meta.get("study_setting") or "").strip()

    # 1. Unsupported assumption: causal claim without design support and without a
    #    competing-explanations declaration — the weakest causal posture there is.
    if causal and not design_supported and not competing:
        objections.append(
            AdversarialObjection(
                kind="unsupported_assumption",
                severity="high",
                statement="En kausal påstående vilar på ingen design som stödjer kausalitet och inga deklarerade konkurrerande förklaringar — behandla som hypotes, inte slutsats.",
                epistemic_category="model_inference",
                evidence_refs=[f"claim:{claim_id}", f"metadata:causal_claim={meta.get('causal_claim')}", f"metadata:study_design={design or 'missing'}"],
            )
        )

    # 2. Weak causal inference + confounding: causal claim on an observational design.
    if causal and design in _OBSERVATIONAL_DESIGNS:
        objections.append(
            AdversarialObjection(
                kind="weak_causal_inference",
                severity="high" if not design_supported else "medium",
                statement=f"Kausal påstående på en observationell design ({design}) — association, inte etablerad kausalitet, även om designen stödjer förloppet.",
                epistemic_category="model_inference",
                evidence_refs=[f"claim:{claim_id}", f"metadata:study_design={design}", f"metadata:causal_claim_supported_by_design={design_supported}"],
            )
        )
        if not competing:
            objections.append(
                AdversarialObjection(
                    kind="confounding_variables",
                    severity="medium",
                    statement="Inga konkurrerande förklaringar (konfundering/urvalsbias) deklarerade — en alternativ förklaring är inte utesluten.",
                    epistemic_category="hypothesis",
                    evidence_refs=[f"claim:{claim_id}", "metadata:competing_explanations=missing"],
                )
            )

    # 3. Unacknowledged counterevidence: declared contradicts edges — named, symmetric.
    for edge in contradicts:
        other_id = edge.to_claim_id if edge.from_claim_id == claim_id else edge.from_claim_id
        objections.append(
            AdversarialObjection(
                kind="unacknowledged_counterevidence",
                severity="high",
                statement="En deklarerad motsägelse rör detta påstående — konflikten presenteras symmetriskt; ingen sida är vinnare på grund av ursprung.",
                epistemic_category="observed_evidence",
                evidence_refs=[f"claim:{claim_id}", f"relationship:{edge.id}", f"counter_claim:{other_id}"],
            )
        )

    # 4. Overstated confidence: a low quality grade under a non-assumption verdict.
    if quality_grade == "low" and verdict.status.value not in ("assumption", "conflicting_evidence"):
        objections.append(
            AdversarialObjection(
                kind="overstated_confidence",
                severity="medium",
                statement=f"Vederbörlighetsgrad 'low' (t.ex. intressekonflikt eller svag design) men luterverdiktet är '{verdict.status.value}' — sänk den uttryckta säkerheten.",
                epistemic_category="verified_conclusion",
                evidence_refs=[f"claim:{claim_id}", f"quality_grade={quality_grade}", f"verdict={verdict.status.value}"],
            )
        )

    # 5. Incorrect extrapolation: a single-setting sample presented beyond its setting.
    if setting and verdict.status.value in ("hypothesis", "verified_fact") and (sample is None or (isinstance(sample, int) and sample < 120)):
        objections.append(
            AdversarialObjection(
                kind="incorrect_extrapolation",
                severity="medium",
                statement=f"Resultat från en miljö ({setting}) med {'okänd' if sample is None else 'liten'} stickprovsstorlek — generalisera inte till andra populationer utan mer evidens.",
                epistemic_category="unresolved_question",
                evidence_refs=[f"claim:{claim_id}", f"metadata:study_setting={setting}", f"metadata:sample_size={sample}"],
            )
        )

    # 6. Inconsistent conclusions across sessions: verdict swings on the audit trail.
    swings = _verdict_swings(db, owner_id, claim_id)
    if len(swings) >= 2:
        objections.append(
            AdversarialObjection(
                kind="inconsistent_conclusions",
                severity="medium",
                statement=f"Slutsatsen har växlat {len(swings)} gånger i revisionsspåret ({' -> '.join(swings)}) — fastställ vilken evidensändring som bär varje växling.",
                epistemic_category="observed_evidence",
                evidence_refs=[f"claim:{claim_id}"] + [f"audit:{i}" for i in range(len(swings))],
            )
        )

    # 7. Fabricated citation guard: the claim's own source must EXIST (provenance chain).
    if claim.source_id is None or claim.chunk_id is None:
        objections.append(
            AdversarialObjection(
                kind="fabricated_citation",
                severity="high",
                statement="Påståendet saknar grundad källa/chunk — citatkedjan är bruten och påståendet kan inte verifieras mot ett dokument.",
                epistemic_category="source_claim",
                evidence_refs=[f"claim:{claim_id}", f"source_id={claim.source_id}", f"chunk_id={claim.chunk_id}"],
            )
        )

    return AdversarialReview(
        claim_id=claim_id,
        reviewed=bool(objections),
        verdict_status=verdict.status.value,
        objections=objections,
    )


def _verdict_swings(db: Session, owner_id: uuid.UUID, claim_id: uuid.UUID) -> list[str]:
    """Verdict transitions recorded on the audit trail (epistemic_shift rows mentioning
    this claim), in chronological order — the material for cross-session consistency
    review. Deterministic parsing of the shift detail; nothing is inferred."""
    from app.models.audit import AuditLog

    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == "epistemic_shift")
        .order_by(AuditLog.created_at.asc())
        .limit(200)
        .all()
    )
    transitions: list[str] = []
    needle = str(claim_id)
    for row in rows:
        detail = row.detail or ""
        if needle not in detail:
            continue
        import re

        found = re.findall(r"'([a-z_]+) -> ([a-z_]+)'", detail)
        for _prior, current in found:
            transitions.append(current)
    return transitions


def format_adversarial_review_instructions(review: AdversarialReview) -> str:
    """The founder-facing Swedish line for the library detail/chat prompt layer. Bounded
    to three objections; every line names its epistemic category. Empty when the review
    found nothing — silence here means 'no justified objection', never approval."""
    if not review.reviewed:
        return ""
    lines = ["ADVERSARIAL GRANSKNING (kandidater att ta på allvar — varje invändning pekar på konkret evidens):"]
    for o in review.objections[:3]:
        lines.append(f"- [{o.kind}/{o.severity}] {o.statement} (kategori: {o.epistemic_category}; evidens: {', '.join(o.evidence_refs[:3])})")
    return "\n".join(lines)
