"""Statistics Integrity -- challenges statistical presentation explicitly. See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture decision.

Extends `app.mainai_vision.evidence.evaluate_confounds()`/`count_independent_sources()`
(imported, never re-implemented) with the specific claim-shaped checks this program's own
research council needs: relative-vs-absolute risk framing and source-count collapse.

REPEATED SOURCE != INDEPENDENT EVIDENCE. LARGE PERCENTAGE != LARGE ABSOLUTE EFFECT. METRIC
IMPROVEMENT != SYSTEM IMPROVEMENT."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_vision.evidence import RawEvidence, count_independent_sources


@dataclass(frozen=True)
class RelativeRiskClaim:
    relative_change_pct: float  # e.g. 200 for "risk increased 200%"
    absolute_baseline: float | None  # the rate this is relative TO, if disclosed
    absolute_change: float | None  # the resulting absolute rate/count change, if computable


@dataclass(frozen=True)
class RelativeRiskAssessment:
    claim: RelativeRiskClaim
    baseline_disclosed: bool
    absolute_effect_computable: bool
    flagged: bool
    reason: str


def assess_relative_risk_claim(claim: RelativeRiskClaim) -> RelativeRiskAssessment:
    """"risk increased 200%" must trigger: from what absolute baseline? Flags any relative-risk
    claim whose absolute baseline is undisclosed -- LARGE PERCENTAGE != LARGE ABSOLUTE EFFECT."""

    baseline_disclosed = claim.absolute_baseline is not None
    absolute_computable = claim.absolute_change is not None
    flagged = not baseline_disclosed
    reason = (
        f"relative change of {claim.relative_change_pct:.0f}% has no disclosed absolute baseline -- "
        "the real-world size of this effect cannot be judged from the percentage alone"
        if flagged
        else f"baseline disclosed ({claim.absolute_baseline}); absolute change {'computable' if absolute_computable else 'not computable'}"
    )
    return RelativeRiskAssessment(claim=claim, baseline_disclosed=baseline_disclosed, absolute_effect_computable=absolute_computable, flagged=flagged, reason=reason)


@dataclass(frozen=True)
class SourceCollapseAssessment:
    cited_count: int
    independent_count: int
    collapsed: bool
    reason: str


def assess_source_collapse(evidence: tuple[RawEvidence, ...]) -> SourceCollapseAssessment:
    """"10 articles report this" must trigger: 10 independent sources or 10 retellings of 1
    source? Reuses `count_independent_sources()` verbatim -- never re-implemented."""

    independent = count_independent_sources(evidence)
    collapsed = len(evidence) > independent
    reason = (
        f"{len(evidence)} citation(s) collapse to {independent} independent underlying source(s) -- "
        "REPEATED SOURCE != INDEPENDENT EVIDENCE"
        if collapsed
        else f"{len(evidence)} citation(s) are {independent} genuinely independent source(s) -- no collapse"
    )
    return SourceCollapseAssessment(cited_count=len(evidence), independent_count=independent, collapsed=collapsed, reason=reason)


@dataclass(frozen=True)
class MetricVsSystemAssessment:
    metric_name: str
    metric_improved: bool
    system_quality_evidence: str | None
    conflated: bool
    reason: str


def assess_metric_vs_system_quality(*, metric_name: str, metric_improved: bool, system_quality_evidence: str | None, system_quality_worsened: bool = False) -> MetricVsSystemAssessment:
    """METRIC IMPROVEMENT != SYSTEM IMPROVEMENT: flags the case where a tracked metric improved
    while independent evidence shows real system quality worsened -- never silently assumes the
    metric IS the system's quality."""

    conflated = metric_improved and system_quality_worsened
    if conflated:
        reason = f"metric {metric_name!r} improved but independent evidence shows system quality WORSENED -- do not conflate the two"
    elif system_quality_evidence is None:
        reason = f"metric {metric_name!r} improved, but no independent system-quality evidence was supplied -- improvement is UNVERIFIED, not assumed"
    else:
        reason = f"metric {metric_name!r} improvement is consistent with the supplied independent system-quality evidence"
    return MetricVsSystemAssessment(metric_name=metric_name, metric_improved=metric_improved, system_quality_evidence=system_quality_evidence, conflated=conflated, reason=reason)
