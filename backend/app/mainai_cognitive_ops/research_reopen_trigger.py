"""Continuous Knowledge Auditing -- Cross-Investigation Auto-Reopen. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md. Closes P1 #1 from the Research, Truth
& Advisory Intelligence round's own handoff doc.

Models: new information discovered during an UNRELATED task -> relevance detector ->
materiality threshold -> old investigation lookup -> reopen trigger -> both-side
reinvestigation flag -> confidence update or no-change.

Composes `app.mainai_research.research_ledger`'s existing (and this round's newly added,
additive, read-only) functions -- never a second investigation store. Does NOT reopen
everything for weak similarity: `RELEVANCE_REOPEN_THRESHOLD` is a real gate, proven by test in
both directions."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.mainai_research.research_ledger import list_investigations, reopen_investigation
from app.mainai_research.types import InvestigationStatus

RELEVANCE_REOPEN_THRESHOLD = 0.35
_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


def _keywords(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text) if len(w) > 2}


def score_relevance(*, investigation_question: str, new_evidence_summary: str) -> float:
    """Jaccard overlap of significant words -- a documented heuristic, not NLP. SIMILAR != SAME:
    this score is a candidate-selection signal only, never itself treated as proof the new
    evidence actually bears on the old investigation."""

    a, b = _keywords(investigation_question), _keywords(new_evidence_summary)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(frozen=True)
class ReopenCandidate:
    investigation_id: uuid.UUID
    question: str
    relevance_score: float
    materiality_met: bool
    reason: str


def assess_reopen_candidate(*, investigation: dict, relevance_score: float, materiality_threshold: float = RELEVANCE_REOPEN_THRESHOLD) -> ReopenCandidate:
    materiality_met = relevance_score >= materiality_threshold
    reason = (
        f"relevance {relevance_score:.2f} >= threshold {materiality_threshold:.2f} -- reopen candidate"
        if materiality_met
        else f"relevance {relevance_score:.2f} below threshold {materiality_threshold:.2f} -- weak similarity, NOT reopened"
    )
    return ReopenCandidate(
        investigation_id=investigation["id"], question=investigation["question"],
        relevance_score=relevance_score, materiality_met=materiality_met, reason=reason,
    )


def find_cross_investigation_reopen_candidates(
    db: Session, *, owner_id: uuid.UUID, new_evidence_summary: str, materiality_threshold: float = RELEVANCE_REOPEN_THRESHOLD,
) -> tuple[ReopenCandidate, ...]:
    """Scans this owner's ACTIVE and SATURATED_FOR_NOW investigations (never CLOSED --
    consistent with `research_ledger.reopen_investigation()`'s own terminal-state rule) and
    scores each against the new evidence. Read-only: does not itself reopen anything."""

    candidates: list[ReopenCandidate] = []
    for status in (InvestigationStatus.ACTIVE, InvestigationStatus.SATURATED_FOR_NOW):
        for investigation in list_investigations(db, owner_id=owner_id, status=status):
            score = score_relevance(investigation_question=investigation["question"], new_evidence_summary=new_evidence_summary)
            candidates.append(assess_reopen_candidate(investigation=investigation, relevance_score=score, materiality_threshold=materiality_threshold))
    return tuple(candidates)


def trigger_cross_investigation_reopen(
    db: Session, *, owner_id: uuid.UUID, new_evidence_summary: str, materiality_threshold: float = RELEVANCE_REOPEN_THRESHOLD,
) -> tuple[dict, ...]:
    """Actually reopens (via the existing, unmodified `reopen_investigation()`) every candidate
    that meets the materiality threshold; leaves SATURATED_FOR_NOW investigations below
    threshold untouched. Does NOT update any hypothesis confidence itself -- that remains the
    caller's own, separately-reasoned `update_hypothesis_confidence()` call (BOTH-SIDE
    reinvestigation is a follow-up action this function only flags, never performs)."""

    reopened = []
    for candidate in find_cross_investigation_reopen_candidates(db, owner_id=owner_id, new_evidence_summary=new_evidence_summary, materiality_threshold=materiality_threshold):
        if candidate.materiality_met:
            reopened.append(reopen_investigation(db, owner_id=owner_id, investigation_id=candidate.investigation_id))
    return tuple(reopened)
