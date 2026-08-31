"""Teacher evaluation and peer-learning — domain-specific, evidence required.

MORE MODELS AGREE != NECESSARILY TRUE.
AGENT OUTPUT != AUTOMATIC TRUTH.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TeacherScore:
    teacher_id: str
    domain: str
    teaching_value: float
    correctness_proxy: float
    cost: float
    notes: str


def score_teacher(
    *,
    teacher_id: str,
    domain: str,
    later_verified_correct: bool | None,
    critique_useful: bool,
    cost: float = 0.5,
) -> TeacherScore:
    correctness = 0.5 if later_verified_correct is None else (1.0 if later_verified_correct else 0.0)
    value = 0.5 * correctness + 0.4 * (1.0 if critique_useful else 0.2) - 0.1 * cost
    return TeacherScore(
        teacher_id=teacher_id,
        domain=domain,
        teaching_value=max(0.0, min(1.0, value)),
        correctness_proxy=correctness,
        cost=cost,
        notes="domain_specific_not_global_trust",
    )


def resolve_teacher_disagreement(
    *,
    positions: list[dict[str, Any]],
    has_primary_source: bool,
    has_deterministic_validator: bool,
    validator_result: bool | None = None,
) -> dict[str, Any]:
    """Do not majority-vote blindly."""
    if has_deterministic_validator and validator_result is not None:
        return {
            "resolution": "deterministic_validator",
            "accepted": validator_result,
            "majority_vote_used": False,
        }
    if has_primary_source:
        return {
            "resolution": "primary_source_preferred",
            "accepted": None,
            "majority_vote_used": False,
            "needs_founder_or_evidence": True,
        }
    return {
        "resolution": "unresolved_disagreement",
        "accepted": None,
        "majority_vote_used": False,
        "more_models_agree_is_not_truth": True,
        "needs_founder_or_evidence": True,
        "positions": positions,
    }


def peer_lesson_candidate(
    *,
    from_agent: str,
    to_agent: str,
    miss_summary: str,
    evidence: str,
) -> dict[str, Any]:
    evidence_ok = bool(str(evidence or "").strip())
    return {
        "from_agent": from_agent,
        "to_agent": to_agent,
        "miss_summary": miss_summary,
        "evidence": evidence,
        "automatic_truth": False,
        "requires_evidence": True,
        "accepted": evidence_ok,  # never accept without evidence
        "authorized": False,
    }
