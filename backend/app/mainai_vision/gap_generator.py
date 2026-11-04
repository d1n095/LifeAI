"""Vision Gap Generator -- infers missing supporting requirements from an accepted capability,
proposes them via the EXISTING, unchanged `project_entities.service.record_interpretation_
proposal()` staging pipeline. See
docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture decision.

IMPLIED REQUIREMENT != EXECUTION AUTHORITY, structurally enforced, not just documented: this
module has no import of, and no code path that reaches, `promote_interpretation_proposal()` --
it can PROPOSE, CLASSIFY, PRIORITIZE, EXPLAIN, never self-authorize (verified by this package's
own AST-based structural test, matching `resource_intelligence.decision`'s own technique).

Composes with the real, existing `app.mainai_executive.missing_piece.detect_missing_pieces()` as
ONE input signal (existing-package coverage heuristic) -- never re-implements that scan."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.mainai_executive.missing_piece import detect_missing_pieces

# Documented, hand-picked capability-implication table -- same convention as every other
# threshold/table in this program (`decision.py`'s CONTEXT_UTILIZATION_*, `judgment.py`'s
# CONFIDENCE_BAR): no historical data exists yet to fit this against; it is a starting point,
# not a claim of completeness. Keyed by a lowercase substring match against the founder's own
# capability description -- deterministic, never an LLM judgment call, matching
# `missing_piece.detect_missing_pieces()`'s own "heuristic keyword map, not LLM architecture"
# doctrine exactly.
CAPABILITY_IMPLICATION_TABLE: dict[str, tuple[str, ...]] = {
    "autonomous development": (
        "durable jobs", "agent supervision", "restart recovery", "independent review separation",
        "context lifecycle management", "cost/quota tracking", "provider handling",
        "currentness tracking", "owner isolation", "evidence capture", "handoff protocol",
        "operational metrics",
    ),
    "personal recall": (
        "encryption key hierarchy", "session/grant authorization", "restart recovery",
        "owner isolation", "stale-index detection", "currentness tracking", "deduplication",
    ),
    "continuous supervision": (
        "canonical job authority", "restart recovery", "idempotent delivery", "cost tracking",
        "owner isolation", "builder/examiner separation", "blocker classification",
    ),
    "resource intelligence": (
        "context lifecycle policy", "cost-to-finish estimation", "quota awareness",
        "efficiency profiling", "founder attention accounting",
    ),
}


@dataclass(frozen=True)
class ImpliedRequirement:
    title: str
    rationale: str
    priority: str  # "high" | "medium" | "low" -- never "authorized"
    classification: str  # "implied_by_capability_table" | "missing_piece_scan"
    authorized: bool = False


@dataclass(frozen=True)
class GapReport:
    capability_description: str
    matched_capability_keys: tuple[str, ...]
    implied_requirements: tuple[ImpliedRequirement, ...]
    missing_piece_scan: dict
    authorized: bool = False


def propose_implied_requirements(*, capability_description: str) -> GapReport:
    """Pure: no `db`, no I/O. Returns proposed, classified, prioritized implied requirements --
    never authorizes or persists anything itself. A caller wanting these durable calls
    `persist_gap_proposals()` below, which only ever reaches
    `record_interpretation_proposal()` (staging), never `promote_interpretation_proposal()`."""

    lower = capability_description.lower()
    matched: list[str] = []
    implied: list[ImpliedRequirement] = []
    for key, requirements in CAPABILITY_IMPLICATION_TABLE.items():
        if key in lower:
            matched.append(key)
            for requirement in requirements:
                implied.append(
                    ImpliedRequirement(
                        title=requirement,
                        rationale=f"implied by accepted capability {key!r} (CAPABILITY_IMPLICATION_TABLE)",
                        priority="high",
                        classification="implied_by_capability_table",
                    )
                )

    scan = detect_missing_pieces(founder_request=capability_description)
    for missing in scan.get("missing_pieces", []):
        implied.append(
            ImpliedRequirement(
                title=str(missing),
                rationale="flagged by missing_piece.detect_missing_pieces() existing-coverage scan",
                priority="medium",
                classification="missing_piece_scan",
            )
        )

    # De-duplicate by title -- the same requirement can legitimately surface from both signals;
    # a caller should see it once, not twice.
    seen: set[str] = set()
    deduped: list[ImpliedRequirement] = []
    for item in implied:
        if item.title in seen:
            continue
        seen.add(item.title)
        deduped.append(item)

    return GapReport(
        capability_description=capability_description,
        matched_capability_keys=tuple(matched),
        implied_requirements=tuple(deduped),
        missing_piece_scan=scan,
    )


def persist_gap_proposals(db, *, owner_id: uuid.UUID, source_claim_id: uuid.UUID, report: GapReport) -> list:
    """The ONLY function in this module that touches the database -- and it reaches ONLY
    `record_interpretation_proposal()` (staging), never `promote_interpretation_proposal()`.
    Structurally verified by this package's own AST-based test that this module never imports
    `promote_interpretation_proposal`."""

    from app.project_entities.service import record_interpretation_proposal

    rows = []
    for requirement in report.implied_requirements:
        rows.append(
            record_interpretation_proposal(
                db,
                owner_id=owner_id,
                source_claim_id=source_claim_id,
                proposed_entity_type="implied_requirement",
                idempotency_key=f"vision-gap:{owner_id}:{requirement.title}",
                classifier_strategy=requirement.classification,
                classifier_confidence="uncertain",
                classifier_reasoning=requirement.rationale,
                provenance={"authorized": False, "vision_gap_generator": True, "priority": requirement.priority},
            )
        )
    return rows
