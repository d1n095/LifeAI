"""Teacher Value. See docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

LOW USAGE != LOW STRATEGIC VALUE: a provider can be valuable even if used rarely, when it
teaches something unique, catches novel failure classes, or provides real redundancy. This is
a DIFFERENT axis from `app.mainai_research.provider_economics` (which reasons over $ cost/
quota/procurement); this module reasons over pedagogical/strategic value, and is deliberately
NOT merged into that module's own signal (the two axes can point in opposite directions at
once -- an expensive, rarely-used provider can still be a valuable teacher).

Pure: no `db`, no I/O."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TeacherValueAssessment:
    high_value: bool
    reason: str


def assess_teacher_value(
    *,
    unique_capability_taught: bool = False,
    novel_failure_discovery_count: int = 0,
    rework_reduction_fraction: float = 0.0,
    strategic_redundancy: bool = False,
    usage_count: int = 0,
) -> TeacherValueAssessment:
    """LOW USAGE != LOW STRATEGIC VALUE: `usage_count` alone never drives this assessment
    toward low value -- a provider used once that taught a unique capability or caught a novel
    failure class is still high-value."""

    if unique_capability_taught:
        return TeacherValueAssessment(True, "teaches a capability no other available provider/agent currently has -- LOW USAGE != LOW STRATEGIC VALUE")

    if novel_failure_discovery_count > 0:
        return TeacherValueAssessment(True, f"found {novel_failure_discovery_count} novel failure class(es) local agents missed")

    if strategic_redundancy:
        return TeacherValueAssessment(True, "provides real redundancy against a single point of failure")

    if rework_reduction_fraction >= 0.3:
        return TeacherValueAssessment(True, f"reduces rework by {rework_reduction_fraction:.0%} when used")

    return TeacherValueAssessment(False, f"no unique capability, novel failure discovery, redundancy, or material rework reduction observed over {usage_count} use(s)")
