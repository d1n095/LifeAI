"""Historical Omission Discovery. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

WHAT DID WE DISCUSS BUT NEVER BUILD? WHAT DID WE BUILD BUT NEVER VERIFY? WHAT WAS LOST BETWEEN
SUMMARIES? NOT_IN_ROADMAP != INTENTIONALLY_REJECTED -- a claim never explicitly marked REJECTED
is never auto-resurrected AS rejected, but it also never counts as complete just because no gap
was noticed yet (NO_KNOWN_GAP != COMPLETE).

Pure: a documented keyword-overlap heuristic against caller-supplied canonical-vision text,
never NLP/LLM judgment (matches `mainai_vision.gap_generator`'s own doctrine). This module has
no corpus/conversation-history scanner of its own -- see `adapters.py` for the honestly
disclosed seam."""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.mainai_coverage.types import CapabilityClaim, CoverageDisposition, MaturityState, maturity_at_least

MENTION_THRESHOLD_FOR_OMISSION_CANDIDACY = 2  # "discussed 4 times, never architected" needs >1 mention to matter
CANONICAL_MATCH_THRESHOLD = 0.3
_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


def _keywords(text: str) -> set[str]:
    return {w.lower() for w in _WORD_RE.findall(text) if len(w) > 2}


def _best_canonical_overlap(claim_text: str, canonical_vision_texts: frozenset[str]) -> float:
    claim_kw = _keywords(claim_text)
    if not claim_kw or not canonical_vision_texts:
        return 0.0
    best = 0.0
    for text in canonical_vision_texts:
        other = _keywords(text)
        if not other:
            continue
        score = len(claim_kw & other) / len(claim_kw | other)
        best = max(best, score)
    return best


DISCUSSED_NEVER_BUILT = "discussed_never_built"
BUILT_NEVER_VERIFIED = "built_never_verified"
VERIFIED_NEVER_INTEGRATED = "verified_never_integrated"
ACTIVATED_NEVER_PRODUCTION_PROVEN = "activated_never_production_proven"


@dataclass(frozen=True)
class OmissionFinding:
    claim_id: str
    kind: str
    reason: str
    canonical_overlap_score: float
    recommend_denominator_expansion: bool


def find_omissions(*, claims: tuple[CapabilityClaim, ...], canonical_vision_texts: frozenset[str]) -> tuple[OmissionFinding, ...]:
    """NOT_IN_ROADMAP != INTENTIONALLY_REJECTED: a claim explicitly `disposition=REJECTED` (or
    `SUPERSEDED`) is NEVER flagged for denominator expansion here, regardless of mention count
    or missing canonical overlap -- an intentional past decision is not automatically
    resurrected merely because a later scan doesn't see it in the current vision graph."""

    findings: list[OmissionFinding] = []
    for claim in claims:
        if claim.disposition in (CoverageDisposition.REJECTED, CoverageDisposition.SUPERSEDED):
            continue

        overlap = _best_canonical_overlap(claim.description, canonical_vision_texts)
        in_canonical_vision = overlap >= CANONICAL_MATCH_THRESHOLD

        if not in_canonical_vision and claim.mention_count >= MENTION_THRESHOLD_FOR_OMISSION_CANDIDACY:
            findings.append(OmissionFinding(
                claim_id=claim.claim_id, kind=DISCUSSED_NEVER_BUILT,
                reason=f"discussed {claim.mention_count} time(s), not present in current canonical vision (best overlap {overlap:.2f})",
                canonical_overlap_score=overlap, recommend_denominator_expansion=True,
            ))
            continue

        if claim.maturity is None:
            continue

        if maturity_at_least(claim.maturity, MaturityState.IMPLEMENTED) and not maturity_at_least(claim.maturity, MaturityState.UNIT_TESTED):
            findings.append(OmissionFinding(
                claim_id=claim.claim_id, kind=BUILT_NEVER_VERIFIED,
                reason=f"maturity={claim.maturity.value} -- implemented but never verified",
                canonical_overlap_score=overlap, recommend_denominator_expansion=False,
            ))
        elif maturity_at_least(claim.maturity, MaturityState.INDEPENDENTLY_REVIEWED) and not maturity_at_least(claim.maturity, MaturityState.INTEGRATED):
            findings.append(OmissionFinding(
                claim_id=claim.claim_id, kind=VERIFIED_NEVER_INTEGRATED,
                reason=f"maturity={claim.maturity.value} -- independently reviewed but never integrated",
                canonical_overlap_score=overlap, recommend_denominator_expansion=False,
            ))
        elif maturity_at_least(claim.maturity, MaturityState.ACTIVATED) and not maturity_at_least(claim.maturity, MaturityState.PRODUCTION_PROVEN):
            findings.append(OmissionFinding(
                claim_id=claim.claim_id, kind=ACTIVATED_NEVER_PRODUCTION_PROVEN,
                reason=f"maturity={claim.maturity.value} -- activated but never production-proven",
                canonical_overlap_score=overlap, recommend_denominator_expansion=False,
            ))

    return tuple(findings)
