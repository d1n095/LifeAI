"""External Dependency Reduction. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

ONE EXPENSIVE DAY != REMOVE PROVIDER. ONE GOOD RESULT != UPGRADE. LOW USAGE != LOW STRATEGIC
VALUE. Recommends reducing an external provider's role only when sustained, diverse, examined
local evidence supports it -- composes `promotion_policy.assess_promotion_eligibility()` (the
same evidence bar) rather than inventing a second one, and defers entirely to
`teacher_value.assess_teacher_value()` when the external provider still catches something
unique.

Pure: no `db`, no I/O."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_workforce.promotion_policy import PromotionAssessment
from app.mainai_workforce.teacher_value import TeacherValueAssessment
from app.mainai_workforce.types import AutonomyStage, ProviderDependenceRecommendation


@dataclass(frozen=True)
class DependenceRecommendation:
    recommendation: ProviderDependenceRecommendation
    reason: str


def recommend_dependence_reduction(
    *,
    current_stage: AutonomyStage,
    promotion: PromotionAssessment,
    teacher_value: TeacherValueAssessment,
    quality_parity_with_external: bool,
) -> DependenceRecommendation:
    """First real signal wins: a high-value teacher (unique bugs caught, real redundancy) is
    NEVER reduced, regardless of how strong the local evidence looks -- matches the founder's
    own worked example (Claude still catches concurrency/recovery bugs -> do NOT reduce yet)."""

    if teacher_value.high_value:
        return DependenceRecommendation(ProviderDependenceRecommendation.DO_NOT_REDUCE_YET, f"teacher remains high-value: {teacher_value.reason}")

    if not promotion.eligible:
        return DependenceRecommendation(ProviderDependenceRecommendation.DO_NOT_REDUCE_YET, f"local evidence insufficient: {promotion.reason}")

    if not quality_parity_with_external:
        return DependenceRecommendation(ProviderDependenceRecommendation.DO_NOT_REDUCE_YET, "local quality has not yet reached parity with the external provider on comparable jobs")

    if current_stage >= AutonomyStage.LOCAL_DEFAULT_EXTERNAL_FALLBACK:
        return DependenceRecommendation(ProviderDependenceRecommendation.MOVE_TO_FALLBACK, f"stage={current_stage.name}, sustained parity evidence: {promotion.reason}")

    return DependenceRecommendation(ProviderDependenceRecommendation.KEEP_AS_EXAMINER, f"evidence supports promotion but stage={current_stage.name} is not yet at the fallback threshold -- keep external as examiner one more round")
