"""Assumption review — investigate a disagreement, do not auto-agree and do not defend.

A founder objection is a signal to investigate, not evidence that the founder is right. A
model's confidence is not evidence either. This module ranks supplied evidence with the
existing ``app.mainai_school.evidence`` hierarchy, changes a conclusion only when stronger
evidence actually contradicts it, and preserves the reason for the change.

No AI conflict judgment is invoked here; it is deterministic and caller-driven.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.mainai_school.evidence import EvidenceRank
from app.problem_learning.service import transition_assumption

# Evidence at or below this rank is strong enough to overturn a prior conclusion.
# DIRECTLY_OBSERVED_OUTCOME (3) and stronger; authoritative/historical/model opinion are not.
_DECISIVE_RANK = EvidenceRank.DIRECTLY_OBSERVED_OUTCOME


@dataclass(frozen=True)
class ReviewEvidence:
    rank: str
    summary: str
    supports: bool | None
    source_id: str | None = None


def _rank(value: str) -> EvidenceRank:
    normalized = value.strip().upper()
    try:
        return EvidenceRank[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown evidence rank: {value}") from exc


def review_objection(
    db: Session,
    *,
    owner_id: uuid.UUID,
    problem_id: uuid.UUID,
    objection: str,
    evidence: tuple[ReviewEvidence, ...] | list[ReviewEvidence] = (),
    current_claim: str | None = None,
    assumption_id: uuid.UUID | None = None,
    apply_transition: bool = False,
    actor: str = "deterministic_verifier",
) -> dict[str, Any]:
    """Evaluate an objection against supplied evidence and report whether anything changed.

    Outcomes:
      * ``revised`` — decisive evidence contradicts the prior conclusion.
      * ``confirmed`` — decisive evidence supports it.
      * ``unresolved`` — no decisive evidence, or only model opinion.

    ``apply_transition=True`` marks the linked assumption ``contradicted`` through the
    existing problem-learning service; the transition reason names the evidence that caused
    it. The status vocabulary is the schema's own (0042), not a new one.
    """
    if not objection.strip():
        raise ValueError("objection text is required")

    considered: list[dict[str, Any]] = []
    for item in evidence:
        rank = _rank(item.rank)
        considered.append(
            {
                "rank": rank.name,
                "rank_value": int(rank),
                "summary": item.summary,
                "supports_prior_conclusion": item.supports,
                "source_id": item.source_id,
            }
        )

    sided = [item for item in evidence if item.supports is not None]
    strongest = min(sided, key=lambda item: int(_rank(item.rank))) if sided else None

    if strongest is None:
        outcome = "unresolved"
        reason = (
            "objection_is_not_evidence"
            if not evidence
            else "evidence_present_but_takes_no_side"
        )
        strongest_rank = None
    else:
        strongest_rank = int(_rank(strongest.rank))
        if _rank(strongest.rank) > _DECISIVE_RANK:
            outcome = "unresolved"
            reason = (
                "model_opinion_is_not_evidence"
                if _rank(strongest.rank) == EvidenceRank.MODEL_OPINION
                else "evidence_too_weak_to_overturn_conclusion"
            )
        elif strongest.supports:
            outcome = "confirmed"
            reason = f"decisive_evidence_supports_conclusion:{_rank(strongest.rank).name}"
        else:
            outcome = "revised"
            reason = f"decisive_evidence_contradicts_conclusion:{_rank(strongest.rank).name}"

    transitioned = False
    if outcome == "revised" and apply_transition and assumption_id is not None:
        transition_assumption(
            db,
            owner_id=owner_id,
            assumption_id=assumption_id,
            status="contradicted",
            reason=(
                f"{reason}; objection={objection[:200]}; "
                f"strongest_evidence={strongest.summary[:200] if strongest else 'none'}"
            ),
            actor=actor,
        )
        transitioned = True

    return {
        "problem_id": str(problem_id),
        "objection": objection[:500],
        "current_claim": current_claim,
        "outcome": outcome,
        "conclusion_changed": outcome == "revised",
        "reason": reason,
        "strongest_evidence_rank": strongest_rank,
        "evidence_considered": considered,
        "evidence_rejected_as_insufficient": [
            item["summary"]
            for item in considered
            if item["rank_value"] > int(_DECISIVE_RANK)
        ],
        "assumption_transitioned": transitioned,
        "founder_objection_is_evidence": False,
        "model_confidence_is_evidence": False,
        "ai_conflict_judgment_invoked": False,
        "authority_impact": "NONE — review only; no execution authority is created",
        "execution_authorized": False,
    }
