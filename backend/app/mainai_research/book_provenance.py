"""Book-Grade Provenance + Research Council Output (founder-facing synthesis). See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture decision.

Every strong claim must be traceable: claim -> conclusion -> reasoning record -> evidence set ->
source lineage -> primary source -> exact passage -> date/version. `trace_claim_lineage()`
composes `research_ledger.py`'s own real, durable rows -- never re-derives or fabricates a
lineage the ledger does not actually contain."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.mainai_research import research_ledger


@dataclass(frozen=True)
class ClaimLineage:
    hypothesis: dict[str, Any]
    supporting_evidence: tuple[dict[str, Any], ...]
    contradicting_evidence: tuple[dict[str, Any], ...]
    confidence_history: tuple[dict[str, Any], ...]
    primary_vs_secondary_counts: dict[str, int]
    independent_source_count: int


def trace_claim_lineage(db: Session, *, owner_id: uuid.UUID, hypothesis_id: uuid.UUID) -> ClaimLineage:
    from app.mainai_vision.evidence import EvidenceState, RawEvidence, count_independent_sources

    evidence_rows = research_ledger.list_evidence_for_hypothesis(db, owner_id=owner_id, hypothesis_id=hypothesis_id)
    support = tuple(r for r in evidence_rows if r["role"] == "support")
    contradiction = tuple(r for r in evidence_rows if r["role"] == "contradiction")
    history = tuple(research_ledger.list_confidence_history(db, owner_id=owner_id, hypothesis_id=hypothesis_id))

    raw_evidence = tuple(
        RawEvidence(evidence_id=str(r["id"]), state=EvidenceState(r["evidence_state"]), underlying_source_id=r["underlying_source_id"])
        for r in evidence_rows
    )
    independent_count = count_independent_sources(raw_evidence)

    return ClaimLineage(
        hypothesis={"id": str(hypothesis_id), "investigation_owner": str(owner_id)},
        supporting_evidence=support, contradicting_evidence=contradiction, confidence_history=history,
        primary_vs_secondary_counts={"total": len(evidence_rows), "independent": independent_count},
        independent_source_count=independent_count,
    )


@dataclass(frozen=True)
class ResearchCouncilOutput:
    """Founder-facing synthesis (§22) -- compresses complexity, never transfers it."""

    current_best_explanation: str
    confidence: float | None
    directly_observed: tuple[str, ...]
    inferred: tuple[str, ...]
    supports: tuple[str, ...]
    contradicts: tuple[str, ...]
    alternative_explanations: tuple[str, ...]
    words_vs_actions_summary: str | None
    money_incentive_summary: str | None
    relationship_summary: str | None
    source_independence_summary: str
    falsification_attempts_summary: str
    still_unknown: tuple[str, ...]
    what_would_change_our_mind: tuple[str, ...]
    saturation_status: str
    authorized: bool = False


def synthesize_research_council_output(
    *,
    current_best_explanation: str,
    confidence: float | None,
    directly_observed: tuple[str, ...] = (),
    inferred: tuple[str, ...] = (),
    supports: tuple[str, ...] = (),
    contradicts: tuple[str, ...] = (),
    alternative_explanations: tuple[str, ...] = (),
    words_vs_actions_summary: str | None = None,
    money_incentive_summary: str | None = None,
    relationship_summary: str | None = None,
    independent_source_count: int,
    total_cited_count: int,
    falsification_rounds_survived: int,
    still_unknown: tuple[str, ...] = (),
    what_would_change_our_mind: tuple[str, ...] = (),
    saturation_status: str = "active",
) -> ResearchCouncilOutput:
    """Pure. Never fabricates certainty for narrative impact: `current_best_explanation` is
    reported alongside its own real `confidence` (which may be `None`, honestly, if genuinely
    unassessed) and `still_unknown`/`what_would_change_our_mind` are always present fields, never
    omitted just because they are empty."""

    source_independence = (
        f"{total_cited_count} citation(s) collapse to {independent_source_count} independent underlying source(s)"
        if total_cited_count > independent_source_count
        else f"{independent_source_count} genuinely independent source(s), no collapse observed"
    )
    falsification_summary = f"survived {falsification_rounds_survived} falsification round(s) -- FAILURE TO FIND COUNTEREVIDENCE != PROOF"

    return ResearchCouncilOutput(
        current_best_explanation=current_best_explanation, confidence=confidence,
        directly_observed=directly_observed, inferred=inferred, supports=supports, contradicts=contradicts,
        alternative_explanations=alternative_explanations, words_vs_actions_summary=words_vs_actions_summary,
        money_incentive_summary=money_incentive_summary, relationship_summary=relationship_summary,
        source_independence_summary=source_independence, falsification_attempts_summary=falsification_summary,
        still_unknown=still_unknown, what_would_change_our_mind=what_would_change_our_mind,
        saturation_status=saturation_status,
    )
