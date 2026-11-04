"""Historical Omission Discovery. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md.

WHAT DID WE DISCUSS BUT NEVER BUILD? WHAT DID WE BUILD BUT NEVER VERIFY? WHAT WAS LOST BETWEEN
SUMMARIES? NOT_IN_ROADMAP != INTENTIONALLY_REJECTED -- a claim never explicitly marked REJECTED
is never auto-resurrected AS rejected, but it also never counts as complete just because no gap
was noticed yet (NO_KNOWN_GAP != COMPLETE).

Uses `matching.layered_match()` (§B's layered comparison strategy) rather than a single
keyword-overlap score -- KEYWORD_OVERLAP remains one layer among several and is never
mistaken for semantic understanding (a caller can always see `match_layer` on the result).
This module has no corpus/conversation-history scanner of its own -- see `source_adapters.py`
for real ingestion and `requirement_extraction.py` for turning observations into claims."""

from __future__ import annotations

from dataclasses import dataclass

from app.mainai_coverage.matching import MatchResult, layered_match
from app.mainai_coverage.types import CapabilityClaim, CoverageDisposition, MaturityState, maturity_at_least

MENTION_THRESHOLD_FOR_OMISSION_CANDIDACY = 2  # "discussed 4 times, never architected" needs >1 mention to matter


def _best_canonical_match(claim_text: str, canonical_vision_texts: frozenset[str]) -> MatchResult:
    best = MatchResult(False, None, 0.0)
    for text in canonical_vision_texts:
        result = layered_match(claim_text, text)
        better = (result.matched, result.score) > (best.matched, best.score)
        if better:
            best = result
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
    match_layer: str | None = None  # which layer of matching.layered_match() decided this -- None means no layer matched


def find_omissions(*, claims: tuple[CapabilityClaim, ...], canonical_vision_texts: frozenset[str]) -> tuple[OmissionFinding, ...]:
    """NOT_IN_ROADMAP != INTENTIONALLY_REJECTED: a claim explicitly `disposition=REJECTED` (or
    `SUPERSEDED`) is NEVER flagged for denominator expansion here, regardless of mention count
    or missing canonical match -- an intentional past decision is not automatically
    resurrected merely because a later scan doesn't see it in the current vision graph.

    `in_canonical_vision` now comes from `matching.layered_match()` (§B) rather than a bare
    keyword score -- `match_layer` on the resulting finding always discloses whether the
    verdict came from an exact/lexical/alias/structured match or only KEYWORD_OVERLAP (never
    silently presented as more certain than it is)."""

    findings: list[OmissionFinding] = []
    for claim in claims:
        if claim.disposition in (CoverageDisposition.REJECTED, CoverageDisposition.SUPERSEDED):
            continue

        match = _best_canonical_match(claim.description, canonical_vision_texts)

        if not match.matched and claim.mention_count >= MENTION_THRESHOLD_FOR_OMISSION_CANDIDACY:
            findings.append(OmissionFinding(
                claim_id=claim.claim_id, kind=DISCUSSED_NEVER_BUILT,
                reason=f"discussed {claim.mention_count} time(s), not present in current canonical vision (best score {match.score:.2f}, no layer matched)",
                canonical_overlap_score=match.score, recommend_denominator_expansion=True, match_layer=None,
            ))
            continue

        if claim.maturity is None:
            continue

        layer_name = match.layer.value if match.layer else None
        if maturity_at_least(claim.maturity, MaturityState.IMPLEMENTED) and not maturity_at_least(claim.maturity, MaturityState.UNIT_TESTED):
            findings.append(OmissionFinding(
                claim_id=claim.claim_id, kind=BUILT_NEVER_VERIFIED,
                reason=f"maturity={claim.maturity.value} -- implemented but never verified",
                canonical_overlap_score=match.score, recommend_denominator_expansion=False, match_layer=layer_name,
            ))
        elif maturity_at_least(claim.maturity, MaturityState.INDEPENDENTLY_REVIEWED) and not maturity_at_least(claim.maturity, MaturityState.INTEGRATED):
            findings.append(OmissionFinding(
                claim_id=claim.claim_id, kind=VERIFIED_NEVER_INTEGRATED,
                reason=f"maturity={claim.maturity.value} -- independently reviewed but never integrated",
                canonical_overlap_score=match.score, recommend_denominator_expansion=False, match_layer=layer_name,
            ))
        elif maturity_at_least(claim.maturity, MaturityState.ACTIVATED) and not maturity_at_least(claim.maturity, MaturityState.PRODUCTION_PROVEN):
            findings.append(OmissionFinding(
                claim_id=claim.claim_id, kind=ACTIVATED_NEVER_PRODUCTION_PROVEN,
                reason=f"maturity={claim.maturity.value} -- activated but never production-proven",
                canonical_overlap_score=match.score, recommend_denominator_expansion=False, match_layer=layer_name,
            ))

    return tuple(findings)
