"""Cross-Specialist Procurement Review + User-Harm/Deceptive-Design Review. See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture decision.

Before recommending removal/upgrade/addition of a provider: ANALYST + ECONOMIST +
ADVERSARIAL_COUNSEL + LEGAL_COUNSEL each weigh in via `council.py` (composed, never
re-implemented) -- `provider_economics.py`'s own recommendation is only ONE input, always
subject to adversarial challenge before this module treats it as the council's own output.

PROVIDER ADVISORY != PURCHASE AUTHORITY (structurally: `authorized` is always False)."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_research.council import CouncilSynthesis, SpecialistReview, synthesize_council_review
from app.mainai_research.provider_economics import ProviderEconomicsAssessment


@dataclass(frozen=True)
class ProcurementDecision:
    provider_id: str
    economics_assessment: ProviderEconomicsAssessment
    council_synthesis: CouncilSynthesis
    challenged: bool
    final_recommendation_stands: bool
    authorized: bool = False


def review_procurement_recommendation(
    *, economics_assessment: ProviderEconomicsAssessment, specialist_reviews: tuple[SpecialistReview, ...],
) -> ProcurementDecision:
    """Pure. The economics recommendation `final_recommendation_stands` only when the council
    is unanimous OR the dissent does not include ADVERSARIAL_COUNSEL specifically challenging
    it -- an adversarial dispute on a procurement call must be visible to whoever ultimately
    authorizes it, never silently overridden by a numeric majority."""

    from app.mainai_research.types import SpecialistRole

    synthesis = synthesize_council_review(specialist_reviews)
    adversarial_dissent = SpecialistRole.ADVERSARIAL_COUNSEL in synthesis.dissenting_roles
    stands = synthesis.unanimous or not adversarial_dissent
    return ProcurementDecision(
        provider_id=economics_assessment.provider_id, economics_assessment=economics_assessment,
        council_synthesis=synthesis, challenged=adversarial_dissent, final_recommendation_stands=stands,
    )


@dataclass(frozen=True)
class UserHarmReviewFinding:
    concern: str  # e.g. "misleading default", "buried limitation"
    evidence: str
    severity: str  # "low" | "medium" | "high"


@dataclass(frozen=True)
class UserHarmReviewResult:
    findings: tuple[UserHarmReviewFinding, ...]
    has_high_severity_finding: bool
    recommendation: str
    authorized: bool = False


def review_user_facing_wording(*, wording: str, findings: tuple[UserHarmReviewFinding, ...] = ()) -> UserHarmReviewResult:
    """USER BENEFIT CLAIM != PROOF OF USER BENEFIT. This function is a plain, typed
    constructor/aggregator -- the actual dark-pattern/misleading-wording analysis is the
    caller's own real Legal+Adversarial review (matching this program's own "caller supplies
    the real signal" convention); it exists so a review is always a complete, structured
    object, never a bare pass/fail with no supporting findings."""

    if not wording.strip():
        raise ValueError("review_user_facing_wording requires real wording to review")
    high = any(f.severity == "high" for f in findings)
    recommendation = "do not ship as worded -- revise before release" if high else (
        "revise before release" if findings else "no concern found in this pass"
    )
    return UserHarmReviewResult(findings=findings, has_high_severity_finding=high, recommendation=recommendation)
