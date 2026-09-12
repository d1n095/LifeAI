"""Cross-Agent Duplication Control. See docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

SAME GOAL != NECESSARILY SAME JOB. UNINTENTIONAL DUPLICATION = WASTE. Independent-examiner
duplication is intentional and must remain allowed.

Pure: reasons over caller-supplied `WorkItem`s, never maintains its own work registry."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_cognitive_ops.types import DuplicationVerdict, WorkItem

OVERLAP_SCORE_DUPLICATE_BAR = 0.6


def _overlap_score(a: WorkItem, b: WorkItem) -> float:
    a_kw, b_kw = {k.lower() for k in a.keywords}, {k.lower() for k in b.keywords}
    if not a_kw or not b_kw:
        return 0.0
    return len(a_kw & b_kw) / len(a_kw | b_kw)


@dataclass(frozen=True)
class DuplicationAssessment:
    candidate_id: str
    overlapping_item_id: str | None
    overlap_score: float
    verdict: DuplicationVerdict
    recommendation: str  # "reuse" | "link" | "extend" | "verify" | "proceed"
    reason: str


def assess_duplication(
    *, candidate: WorkItem, existing_work: tuple[WorkItem, ...], is_independent_examiner_context: bool = False,
) -> DuplicationAssessment:
    """Prefers REUSE/LINK/EXTEND/VERIFY before REBUILD. When
    `is_independent_examiner_context=True` and an overlapping item is found, the verdict is
    INTENTIONAL_EXAMINER_DUPLICATE (allowed), never ACCIDENTAL_DUPLICATE (waste) -- the caller
    must explicitly disclose examiner context; it is never inferred."""

    best_item, best_score = None, 0.0
    for item in existing_work:
        if item.item_id == candidate.item_id:
            continue
        score = _overlap_score(candidate, item)
        if score > best_score:
            best_item, best_score = item, score

    if best_item is None or best_score < OVERLAP_SCORE_DUPLICATE_BAR:
        return DuplicationAssessment(
            candidate_id=candidate.item_id, overlapping_item_id=best_item.item_id if best_item else None,
            overlap_score=best_score, verdict=DuplicationVerdict.NOT_DUPLICATE,
            recommendation="proceed", reason=f"no overlapping item scored >= {OVERLAP_SCORE_DUPLICATE_BAR}",
        )

    if is_independent_examiner_context:
        return DuplicationAssessment(
            candidate_id=candidate.item_id, overlapping_item_id=best_item.item_id, overlap_score=best_score,
            verdict=DuplicationVerdict.INTENTIONAL_EXAMINER_DUPLICATE, recommendation="proceed",
            reason=f"overlaps {best_item.item_id} (score={best_score:.2f}) but caller discloses independent-examiner context -- intentional, not waste",
        )

    if best_item.status.value in ("complete",):
        return DuplicationAssessment(
            candidate_id=candidate.item_id, overlapping_item_id=best_item.item_id, overlap_score=best_score,
            verdict=DuplicationVerdict.REUSE_CANDIDATE, recommendation="reuse",
            reason=f"{best_item.item_id} already reports COMPLETE with overlap score {best_score:.2f} -- reuse before rebuilding",
        )

    return DuplicationAssessment(
        candidate_id=candidate.item_id, overlapping_item_id=best_item.item_id, overlap_score=best_score,
        verdict=DuplicationVerdict.ACCIDENTAL_DUPLICATE, recommendation="link",
        reason=f"{best_item.item_id} is already active with overlap score {best_score:.2f} and no examiner-duplication context was disclosed -- likely unintentional waste",
    )
