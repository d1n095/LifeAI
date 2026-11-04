"""Requirement / Capability Extraction + Dedup/Supersession. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md §A.

NORMALIZED OBSERVATIONS -> PROVENANCE -> REQUIREMENT/CAPABILITY EXTRACTION -> DEDUP/
SUPERSESSION -> (feeds `omission_discovery.find_omissions()` for) COVERAGE TRACEABILITY ->
OMISSION CANDIDATES. Deterministic extraction only -- one `CapabilityClaim` per
`NormalizedObservation`, then `deduplicate_claims()` merges claims whose text matches via
`matching.layered_match()`, aggregating `mention_count` and unioning `source_records`
(provenance to ALL original occurrences is retained, never dropped -- matches
`app.mainai_cognitive_ops.information_lifecycle`'s own DEDUPLICATED RECORD MUST RETAIN
PROVENANCE TO ALL ORIGINAL OCCURRENCES doctrine)."""

from __future__ import annotations

import hashlib

from app.mainai_coverage.matching import layered_match
from app.mainai_coverage.types import CapabilityClaim, NormalizedObservation


def _claim_id_for(observation: NormalizedObservation) -> str:
    basis = f"{observation.source_kind.value}:{observation.source_identifier}:{observation.location or observation.text[:80]}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def extract_capability_claims(observations: tuple[NormalizedObservation, ...]) -> tuple[CapabilityClaim, ...]:
    """One claim per observation, before dedup -- deterministic field-copy, mirrors
    `mainai_workforce.capability_learning_loop.extract_reusable_procedure()`'s own "restructure
    what was actually observed, never infer beyond it" discipline. `mention_count` starts at 1
    per observation; `deduplicate_claims()` is what accumulates it across matching claims."""

    claims = []
    for obs in observations:
        claims.append(CapabilityClaim(
            claim_id=_claim_id_for(obs), description=obs.text, mention_count=1,
            last_seen_at=obs.timestamp, provenance=(obs.source_identifier,), source_records=(obs,),
        ))
    return tuple(claims)


def deduplicate_claims(claims: tuple[CapabilityClaim, ...]) -> tuple[CapabilityClaim, ...]:
    """SIMILAR != SAME: only claims `matching.layered_match()` actually judges as matched are
    merged -- never a bare "looks kind of similar" fold. MERGING is held to a STRICTER bar
    than the "is this already in canonical vision" check `omission_discovery.py` uses:
    `include_keyword_overlap=False` here, because KEYWORD_OVERLAP alone is exactly the
    "SIMILAR WORDING == SAME REQUIREMENT" failure mode the founder's own program explicitly
    warns against -- two distinct claims sharing common words (e.g. "revenue grew 10% in Q1"
    vs "...40% in Q3") must never collapse into one record merely because they scored above a
    keyword threshold. The surviving representative keeps the UNION of every matched claim's
    own `source_records`/`provenance` (DEDUPLICATED RECORD MUST RETAIN PROVENANCE TO ALL
    ORIGINAL OCCURRENCES) and the SUM of their `mention_count`s -- this is exactly what makes
    "discussed 4 times" in `omission_discovery.py` mean something real (4 distinct sources),
    not an artifact of one source being split into 4 near-identical observations."""

    merged: list[CapabilityClaim] = []
    for claim in claims:
        target_index = None
        for i, existing in enumerate(merged):
            if layered_match(claim.description, existing.description, include_keyword_overlap=False).matched:
                target_index = i
                break

        if target_index is None:
            merged.append(claim)
            continue

        existing = merged[target_index]
        merged[target_index] = CapabilityClaim(
            claim_id=existing.claim_id, description=existing.description,
            mention_count=existing.mention_count + claim.mention_count,
            stages_present=existing.stages_present | claim.stages_present,
            maturity=existing.maturity if existing.maturity is not None else claim.maturity,
            disposition=existing.disposition if existing.disposition is not None else claim.disposition,
            has_runtime_evidence=existing.has_runtime_evidence or claim.has_runtime_evidence,
            last_seen_at=max((t for t in (existing.last_seen_at, claim.last_seen_at) if t is not None), default=None),
            provenance=tuple(dict.fromkeys(existing.provenance + claim.provenance)),
            source_records=existing.source_records + claim.source_records,
        )

    return tuple(merged)


def build_claims_from_observations(observations: tuple[NormalizedObservation, ...]) -> tuple[CapabilityClaim, ...]:
    """The full A pipeline's deterministic middle: NORMALIZED OBSERVATIONS -> (extract) ->
    (dedup) -> claims ready for `omission_discovery.find_omissions()`."""

    return deduplicate_claims(extract_capability_claims(observations))
