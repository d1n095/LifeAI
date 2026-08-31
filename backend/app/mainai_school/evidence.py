"""Evidence hierarchy — TEACHER != TRUTH. MODEL CONSENSUS != TRUTH.

Prefer deterministic proof and primary sources over model opinion.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class EvidenceRank(IntEnum):
    """Lower number = stronger. Model opinion is weakest supporting evidence."""

    DETERMINISTIC_TEST = 1
    PRIMARY_SOURCE = 2
    DIRECTLY_OBSERVED_OUTCOME = 3
    AUTHORITATIVE_DOMAIN_SOURCE = 4
    MULTIPLE_INDEPENDENT_EXPERTS = 5
    HISTORICAL_EVIDENCE = 6
    MODEL_OPINION = 7


EVIDENCE_HIERARCHY = tuple(r.name for r in EvidenceRank)

INVARIANTS = (
    "TEACHER_IS_NOT_TRUTH",
    "MODEL_CONSENSUS_IS_NOT_TRUTH",
    "PRICE_IS_NOT_QUALITY",
    "FREE_IS_NOT_TRUSTWORTHY_BY_DEFINITION",
    "EXPENSIVE_IS_NOT_CORRECT",
    "LOCAL_IS_NOT_CORRECT_BY_DEFINITION",
    "EVIDENCE_OUTRANKS_OPINION",
    "PRIMARY_PROOF_OUTRANKS_MODEL_AGREEMENT",
    "THREE_MODELS_AGREEING_IS_NOT_AUTOMATICALLY_TRUE",
    "API_FAILURE_IS_NOT_MAINAI_FAILURE",
)


@dataclass(frozen=True)
class EvidenceItem:
    rank: EvidenceRank
    summary: str
    supports_local: bool | None  # True=supports local attempt, False=against, None=neutral
    source_id: str | None = None


@dataclass(frozen=True)
class Verdict:
    winner: str  # "local" | "teacher" | "neither" | "unresolved"
    reason: str
    strongest_rank: int | None
    teacher_overruled: bool
    local_overruled: bool
    majority_vote_used: bool = False
    evidence_used: list[str] | None = None


def strongest_evidence(items: list[EvidenceItem]) -> EvidenceItem | None:
    if not items:
        return None
    return sorted(items, key=lambda e: int(e.rank))[0]


def resolve_local_vs_teacher(
    *,
    local_claim: str,
    teacher_claim: str,
    evidence: list[EvidenceItem],
    teachers_agree: bool = False,
) -> Verdict:
    """Local may be right when evidence says so — even if teacher disagrees."""
    if not evidence:
        return Verdict(
            winner="unresolved",
            reason="no_evidence_teacher_opinion_insufficient",
            strongest_rank=None,
            teacher_overruled=False,
            local_overruled=False,
            evidence_used=[],
        )

    # Prefer strongest evidence that takes a side
    sided = [e for e in evidence if e.supports_local is not None]
    if not sided:
        return Verdict(
            winner="unresolved",
            reason="evidence_present_but_no_side",
            strongest_rank=int(strongest_evidence(evidence).rank) if evidence else None,
            teacher_overruled=False,
            local_overruled=False,
            evidence_used=[e.summary for e in evidence],
        )

    best = sorted(sided, key=lambda e: int(e.rank))[0]
    # Model opinion alone never wins against stronger ranks when mixed.
    if best.rank == EvidenceRank.MODEL_OPINION and teachers_agree:
        return Verdict(
            winner="unresolved",
            reason="model_consensus_is_not_truth_need_stronger_evidence",
            strongest_rank=int(best.rank),
            teacher_overruled=False,
            local_overruled=False,
            majority_vote_used=False,
            evidence_used=[e.summary for e in sided],
        )

    if best.supports_local:
        return Verdict(
            winner="local",
            reason=f"evidence_{best.rank.name.lower()}_supports_local",
            strongest_rank=int(best.rank),
            teacher_overruled=True,
            local_overruled=False,
            evidence_used=[e.summary for e in sided],
        )
    return Verdict(
        winner="teacher",
        reason=f"evidence_{best.rank.name.lower()}_supports_teacher_side",
        strongest_rank=int(best.rank),
        teacher_overruled=False,
        local_overruled=True,
        evidence_used=[e.summary for e in sided],
    )


def rank_name(rank: EvidenceRank | int) -> str:
    return EvidenceRank(int(rank)).name


def evidence_policy_dict() -> dict[str, Any]:
    return {
        "hierarchy": list(EVIDENCE_HIERARCHY),
        "invariants": list(INVARIANTS),
        "teacher_is_not_truth": True,
        "model_consensus_is_not_truth": True,
        "no_single_answer_key": True,
    }
