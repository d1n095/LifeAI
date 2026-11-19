"""Authority-aware ordering of retrieved chunks — version status participates in ranking.

Retrieval has always ranked purely by similarity, and the trust layer separately labels a
non-active source and flags contradictions. That split left a concrete failure: a superseded
draft and the final report that replaced it can be near-duplicates, so the draft sometimes
scored marginally higher and was presented first even though the founder had already marked it
as not current.

The fix is deliberately *not* to filter old material. Historical and superseded sources are
evidence: a founder asking "what did we decide last year?" must still find them, and a
contradiction is only visible if both sides are retrieved. Instead this module applies a small,
bounded penalty to non-active status (and to a document that another source `supersedes`), so a
slightly-better-matching draft sorts below an active document while a strongly-relevant
historical source still beats an unrelated active one.

Two properties matter and are covered by tests:

  * `score` is never rewritten. It stays the raw similarity, because `app/rag/trust.py`'s
    confidence thresholds are calibrated against it.
  * nothing is removed by this module. It reorders and annotates; returning fewer rows than
    requested is the caller's `top_k`, not a hidden suppression rule.
"""

from __future__ import annotations

import uuid

# Bounded, and smaller than any meaningful similarity gap on purpose. A draft at 0.92 vs a
# final at 0.88 should swap; a draft at 0.95 vs an unrelated active source at 0.20 should not.
STATUS_PENALTY: dict[str, float] = {
    "active": 0.0,
    "proposed": 0.06,
    "disputed": 0.10,
    "historical": 0.12,
    "superseded": 0.15,
}

# An explicit `supersedes` edge is stronger evidence than a status label alone: the founder (or
# an import) recorded that this exact document was replaced. It stacks with the status penalty.
SUPERSEDED_EDGE_PENALTY = 0.12

# Unknown/None status: treat like unclassified material, not like authoritative truth.
DEFAULT_PENALTY = 0.08


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


def superseded_document_ids(db, owner_id, document_ids: list[str]) -> set[str]:
    """Documents among `document_ids` that a `supersedes` edge names as the replaced source.

    Owner-scoped and restricted to the candidate set: this never walks the whole graph, and it
    can never demote a document belonging to another owner.
    """
    from app.models.source_relationship import RelationshipType, SourceRelationship

    ids = [u for u in (_uuid(d) for d in set(document_ids)) if u is not None]
    if len(ids) < 2:
        return set()
    rows = (
        db.query(SourceRelationship.to_source_id)
        .filter(
            SourceRelationship.owner_id == owner_id,
            SourceRelationship.relationship_type == RelationshipType.supersedes,
            SourceRelationship.from_source_id.in_(ids),
            SourceRelationship.to_source_id.in_(ids),
        )
        .all()
    )
    return {str(row[0]) for row in rows}


def rank_hits(
    hits: list[dict],
    *,
    superseded_ids: set[str] | None = None,
    top_k: int | None = None,
) -> list[dict]:
    """Return `hits` ordered by authority-adjusted relevance, annotated in place.

    Adds `status_penalty` and `ranking_score` to every hit. `ranking_score` is the value used
    for ordering; `score` remains the untouched similarity.
    """
    superseded_ids = superseded_ids or set()
    for hit in hits:
        status = hit.get("active_truth_status")
        penalty = STATUS_PENALTY.get(status, DEFAULT_PENALTY)
        if hit.get("document_id") in superseded_ids:
            penalty += SUPERSEDED_EDGE_PENALTY
        hit["status_penalty"] = round(penalty, 4)
        hit["ranking_score"] = round(float(hit.get("score") or 0.0) - penalty, 6)

    # Deterministic tie-break: ranking score, then raw similarity, then chunk id. Without the
    # last key two identically-scored chunks from different documents could swap between runs
    # and make acceptance measurements (and citations) non-reproducible.
    ordered = sorted(
        hits,
        key=lambda h: (
            -float(h.get("ranking_score") or 0.0),
            -float(h.get("score") or 0.0),
            str(h.get("chunk_id") or ""),
        ),
    )
    return ordered if top_k is None else ordered[:top_k]
