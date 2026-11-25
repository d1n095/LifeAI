"""F09 — real-database RAG quality matrix.

These tests intentionally do not mock retrieval, RLS, trust assessment or the source
relationships. Each test seeds ordinary Document/DocumentChunk rows for its own user,
then runs the same hybrid_search()/detect_conflicts()/assess_confidence() path production
uses. The lexical channel is used where the question is relevance/exclusion; the semantic
channel is used for the confidence-cap case so the score is genuinely high enough to test
the historical cap rather than merely asserting the pure helper again.
"""

from sqlalchemy import text as sa_text

from app.config import get_settings
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.source_relationship import RelationshipType, SourceRelationship
from app.rag.trust import (
    CONFLICT_INSTRUCTION,
    STATUS_INSTRUCTIONS,
    assess_confidence,
    build_trust_instructions,
    detect_conflicts,
)
from app.rag.vector_store import hybrid_search

_EMBEDDING_DIM = get_settings().embedding_dim


def _set_rls_user(session, owner_id) -> None:
    from app.request_context import current_user_id as current_user_id_var

    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_document(
    session,
    owner_id,
    title: str,
    chunk_text: str,
    *,
    status: ActiveTruthStatus = ActiveTruthStatus.active,
    vector: list[float] | None = None,
) -> Document:
    _set_rls_user(session, owner_id)
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=status,
    )
    session.add(document)
    session.flush()
    session.add(
        DocumentChunk(
            document_id=document.id,
            owner_id=owner_id,
            chunk_index=0,
            text=chunk_text,
            embedding=vector or [0.01] * _EMBEDDING_DIM,
        )
    )
    session.commit()
    return document


def _link(session, owner_id, from_source_id, to_source_id, relationship_type: RelationshipType) -> None:
    _set_rls_user(session, owner_id)
    session.add(
        SourceRelationship(
            owner_id=owner_id,
            from_source_id=from_source_id,
            to_source_id=to_source_id,
            relationship_type=relationship_type,
        )
    )
    session.commit()


def test_relevant_source_is_retrieved_with_citation_fields(db_session, make_verified_user):
    user, _ = make_verified_user()
    source = _make_document(
        db_session,
        user.id,
        "Produktplan Orion",
        "Lanseringsdatum for Orion ar 2027-03-15.",
    )
    _set_rls_user(db_session, user.id)

    hits = hybrid_search(db_session, user.id, None, "Orion", top_k=5)

    assert [hit["document_id"] for hit in hits] == [str(source.id)]
    assert hits[0]["title"] == "Produktplan Orion"
    assert hits[0]["text"] == "Lanseringsdatum for Orion ar 2027-03-15."
    assert hits[0]["active_truth_status"] == "active"
    assert hits[0]["text_match"] is True


def test_irrelevant_source_is_not_retrieved_for_unrelated_query(db_session, make_verified_user):
    user, _ = make_verified_user()
    relevant = _make_document(db_session, user.id, "Produktplan Orion", "Orion lanseras 2027.")
    irrelevant = _make_document(db_session, user.id, "Semesterplan", "Semester i juli 2027.")
    _set_rls_user(db_session, user.id)

    hits = hybrid_search(db_session, user.id, None, "Orion", top_k=10)
    hit_ids = {hit["document_id"] for hit in hits}

    assert str(relevant.id) in hit_ids
    assert str(irrelevant.id) not in hit_ids


def test_contradictory_sources_are_retrieved_together_and_flagged(db_session, make_verified_user):
    user, _ = make_verified_user()
    low = _make_document(db_session, user.id, "Budget 2027 A", "Budget 2027 ar 10 MSEK.")
    high = _make_document(db_session, user.id, "Budget 2027 B", "Budget 2027 ar 20 MSEK.")
    _link(db_session, user.id, low.id, high.id, RelationshipType.contradicts)
    _set_rls_user(db_session, user.id)

    hits = hybrid_search(db_session, user.id, None, "Budget 2027", top_k=10)
    hit_ids = {hit["document_id"] for hit in hits}
    pairs = detect_conflicts(db_session, user.id, list(hit_ids))
    assessment = assess_confidence(hits, pairs)
    instructions = build_trust_instructions(assessment.level, conflicts_detected=assessment.conflicts_detected)

    assert {str(low.id), str(high.id)}.issubset(hit_ids)
    assert pairs == [(str(low.id), str(high.id))]
    assert assessment.conflicts_detected is True
    assert CONFLICT_INSTRUCTION in instructions


def test_historical_source_is_retrieved_with_capped_confidence_and_warning(db_session, make_verified_user):
    user, _ = make_verified_user()
    source = _make_document(
        db_session,
        user.id,
        "Historisk prislista",
        "Priset var 100 kr i den historiska prislistan.",
        status=ActiveTruthStatus.historical,
        vector=[1.0] * _EMBEDDING_DIM,
    )
    _set_rls_user(db_session, user.id)

    hits = hybrid_search(
        db_session,
        user.id,
        [1.0] * _EMBEDDING_DIM,
        "historisk prislista",
        top_k=5,
    )
    assessment = assess_confidence(hits)
    instructions = build_trust_instructions(assessment.level, top_source_status=assessment.top_source_status)

    assert [hit["document_id"] for hit in hits] == [str(source.id)]
    assert hits[0]["active_truth_status"] == "historical"
    assert assessment.score >= 0.99
    assert assessment.level == "medium"
    assert assessment.top_source_status == "historical"
    assert STATUS_INSTRUCTIONS["historical"] in instructions


def test_explicitly_superseded_source_is_not_retrieved(db_session, make_verified_user):
    user, _ = make_verified_user()
    old = _make_document(db_session, user.id, "Prislista 2026", "Pris: 100 kr.")
    new = _make_document(db_session, user.id, "Prislista 2027", "Pris: 150 kr.")
    _link(db_session, user.id, new.id, old.id, RelationshipType.supersedes)
    _set_rls_user(db_session, user.id)

    hits = hybrid_search(db_session, user.id, None, "Pris", top_k=10)
    hit_ids = {hit["document_id"] for hit in hits}

    assert str(new.id) in hit_ids
    assert str(old.id) not in hit_ids


def test_no_match_returns_no_citations_and_uncertainty_instruction(db_session, make_verified_user):
    user, _ = make_verified_user()
    _make_document(db_session, user.id, "Produktplan", "Orion lanseras 2027.")
    _set_rls_user(db_session, user.id)

    hits = hybrid_search(db_session, user.id, None, "helt-utanfor-datasetet-xyz", top_k=5)
    assessment = assess_confidence(hits)
    instructions = build_trust_instructions(assessment.level)

    assert hits == []
    assert assessment.level == "none"
    assert "Ingen relevant källa hittades" in instructions
