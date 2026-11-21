"""Authority-aware ranking: a replaced draft must not outrank the final report.

These are unit tests over `app/rag/ranking.py` — no database, no model. The real-corpus
acceptance test (`tests/acceptance/test_real_retrieval_quality.py`) proves the same property
through ingest + real embeddings + pgvector; this file pins the ordering rule itself so a
threshold change is visible without needing a model.
"""

from __future__ import annotations

from app.rag.ranking import STATUS_PENALTY, rank_hits


def _hit(chunk_id: str, document_id: str, score: float, status: str | None = "active") -> dict:
    return {
        "chunk_id": chunk_id,
        "document_id": document_id,
        "score": score,
        "active_truth_status": status,
        "text": "",
    }


def test_a_superseded_draft_sorts_below_the_final_report():
    draft = _hit("c-draft", "doc-draft", 0.92, "proposed")
    final = _hit("c-final", "doc-final", 0.88, "active")

    ranked = rank_hits([draft, final])

    assert [h["chunk_id"] for h in ranked] == ["c-final", "c-draft"]
    # ...but the draft is still present. Historical evidence is never suppressed.
    assert len(ranked) == 2
    assert draft["score"] == 0.92  # raw similarity untouched
    assert final["score"] == 0.88


def test_an_explicit_supersedes_edge_demotes_even_with_identical_status():
    old = _hit("c-old", "doc-old", 0.90, "active")
    new = _hit("c-new", "doc-new", 0.85, "active")

    ranked = rank_hits([old, new], superseded_ids={"doc-old"})

    assert [h["chunk_id"] for h in ranked] == ["c-new", "c-old"]
    assert ranked[0]["status_penalty"] == 0.0
    assert ranked[1]["status_penalty"] > 0.0


def test_a_strong_historical_match_still_beats_an_irrelevant_active_source():
    historical = _hit("c-hist", "doc-hist", 0.93, "historical")
    irrelevant = _hit("c-active", "doc-active", 0.20, "active")

    ranked = rank_hits([historical, irrelevant])

    assert [h["chunk_id"] for h in ranked] == ["c-hist", "c-active"]


def test_top_k_truncates_but_never_filters_inside_the_cut():
    hits = [
        _hit("c1", "d1", 0.9, "active"),
        _hit("c2", "d2", 0.8, "proposed"),
        _hit("c3", "d3", 0.7, "superseded"),
    ]
    ranked = rank_hits(hits, top_k=2)
    assert len(ranked) == 2
    assert [h["chunk_id"] for h in ranked] == ["c1", "c2"]


def test_unknown_status_is_treated_as_unclassified_not_authoritative():
    unknown = _hit("c-unknown", "d-unknown", 0.90, None)
    active = _hit("c-active", "d-active", 0.87, "active")

    ranked = rank_hits([unknown, active])

    assert ranked[0]["chunk_id"] == "c-active"
    assert unknown["status_penalty"] > 0.0


def test_ordering_is_deterministic_for_equal_scores():
    a = _hit("c-a", "d-a", 0.5, "active")
    b = _hit("c-b", "d-b", 0.5, "active")
    first = rank_hits([a, b])
    second = rank_hits([b, a])
    assert [h["chunk_id"] for h in first] == [h["chunk_id"] for h in second] == ["c-a", "c-b"]


def test_every_declared_penalty_is_bounded_below_a_meaningful_similarity_gap():
    # A guard against a future edit turning ranking into de-facto filtering: the largest combined
    # penalty must stay small enough that a genuinely relevant old source still ranks.
    assert max(STATUS_PENALTY.values()) <= 0.15
