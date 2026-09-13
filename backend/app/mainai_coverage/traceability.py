"""Traceability Chain. See docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

VISION -> REQUIREMENT -> WHY -> ARCHITECTURE -> IMPLEMENTATION -> TEST -> REVIEW ->
INTEGRATION -> ACTIVATION -> RUNTIME EVIDENCE. Detects the first missing link and produces a
deterministic narrative finding -- never an LLM judgment call.

Pure: no `db`, no I/O."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_coverage.types import TRACEABILITY_ORDER, TraceabilityStage

# Deterministic narrative per (last stage actually present) -> finding text. Hand-picked,
# documented, matches the founder's own worked examples verbatim where given.
_FINDING_BY_LAST_STAGE: dict[TraceabilityStage | None, str] = {
    None: "Mentioned or discussed, but never architected.",
    TraceabilityStage.VISION: "Mentioned or discussed, but never architected.",
    TraceabilityStage.REQUIREMENT: "A requirement exists, but no architecture was recorded.",
    TraceabilityStage.WHY: "The rationale is recorded, but no architecture was recorded.",
    TraceabilityStage.ARCHITECTURE: "Architected, but not yet implemented.",
    TraceabilityStage.IMPLEMENTATION: "Implemented but no integration test.",
    TraceabilityStage.TEST: "Tested but not independently reviewed.",
    TraceabilityStage.REVIEW: "Independently reviewed but not integrated.",
    TraceabilityStage.INTEGRATION: "Integrated but not activated.",
    TraceabilityStage.ACTIVATION: "Activated but no runtime evidence was recorded.",
    TraceabilityStage.RUNTIME_EVIDENCE: "Full chain present: vision through runtime evidence.",
}


@dataclass(frozen=True)
class TraceabilityGapReport:
    stages_present: frozenset[TraceabilityStage]
    furthest_stage: TraceabilityStage | None
    missing_stages: tuple[TraceabilityStage, ...]
    finding: str
    complete: bool


def assess_traceability_chain(stages_present: frozenset[TraceabilityStage]) -> TraceabilityGapReport:
    """The chain is ordinal: a stage counts as "reached" only up to the first GAP in
    `TRACEABILITY_ORDER` -- a later stage present without an earlier one (e.g. TEST present but
    IMPLEMENTATION absent) does not let the chain skip ahead; the missing earlier stage is
    still reported as the real gap (a stale/orphaned test with no current implementation is
    exactly the kind of finding this function must surface, not hide)."""

    furthest: TraceabilityStage | None = None
    missing: list[TraceabilityStage] = []
    broken = False
    for stage in TRACEABILITY_ORDER:
        if stage in stages_present and not broken:
            furthest = stage
        else:
            broken = True
            missing.append(stage)

    complete = not missing
    return TraceabilityGapReport(
        stages_present=stages_present, furthest_stage=furthest, missing_stages=tuple(missing),
        finding=_FINDING_BY_LAST_STAGE[furthest], complete=complete,
    )
