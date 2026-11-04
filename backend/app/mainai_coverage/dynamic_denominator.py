"""Dynamic Completion Denominator -- composition only. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

100% means 100% OF CURRENT CANONICAL VISION AT CURRENT VERIFIED QUALITY THRESHOLD -- already
fully implemented in `app.mainai_vision.completion`/`types` (the expandable-denominator
mechanic was proven there: 100% -> new vision node -> completion drops). This module does NOT
reimplement that math. It only proves the missing half of the loop the founder's own §4 names:
a valid omission FOUND by `omission_discovery.find_omissions()` gets staged into the REAL
canonical vision via the EXISTING, unchanged `app.mainai_vision.gap_generator.
propose_implied_requirements()` + `persist_gap_proposals()` pipeline (staging only --
VISION != AUTHORITY; this module has no import of, and no code path to,
`promote_interpretation_proposal()`), after which a caller's next
`app.mainai_vision.completion.assess_program_completion()` call will, for real, recompute a
lower percentage against the now-larger denominator."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.mainai_coverage.omission_discovery import OmissionFinding
from app.mainai_vision.gap_generator import GapReport, persist_gap_proposals, propose_implied_requirements


@dataclass(frozen=True)
class DenominatorExpansionResult:
    claim_id: str
    gap_report: GapReport
    staged_proposal_ids: tuple[uuid.UUID, ...]


def stage_omission_for_vision_expansion(
    db: Session, *, owner_id: uuid.UUID, source_claim_id: uuid.UUID, finding: OmissionFinding, capability_description: str,
) -> DenominatorExpansionResult | None:
    """Only stages when `finding.recommend_denominator_expansion` is True (a
    BUILT_NEVER_VERIFIED/VERIFIED_NEVER_INTEGRATED/etc finding is about an EXISTING vision
    node's own maturity, not a missing one -- there is nothing new to add to the denominator).
    Returns None rather than staging anything when that flag is False, or when
    `propose_implied_requirements()` itself finds nothing to propose (an empty `GapReport` is a
    real, valid outcome, not an error)."""

    if not finding.recommend_denominator_expansion:
        return None

    report = propose_implied_requirements(capability_description=capability_description)
    if not report.implied_requirements:
        return DenominatorExpansionResult(claim_id=finding.claim_id, gap_report=report, staged_proposal_ids=())

    proposals = persist_gap_proposals(db, owner_id=owner_id, source_claim_id=source_claim_id, report=report)
    return DenominatorExpansionResult(
        claim_id=finding.claim_id, gap_report=report, staged_proposal_ids=tuple(p.id for p in proposals),
    )
