"""Unit tests for app/rag/trust.py — Founder Knowledge Studio v1's active_truth_status
awareness and structural conflict detection (DEL 8). detect_conflicts hits a real local
Postgres (RLS included); assess_confidence/build_trust_instructions are pure functions."""

from app.models.document import Document, DocumentSource
from app.models.source_relationship import RelationshipType, SourceRelationship
from app.rag.trust import (
    STATUS_INSTRUCTIONS,
    CONFLICT_INSTRUCTION,
    assess_confidence,
    build_trust_instructions,
    detect_conflicts,
)


def _hit(document_id="11111111-1111-1111-1111-111111111111", score=0.9, status="active", title="Testdok"):
    return {"document_id": document_id, "title": title, "text": "innehall", "score": score, "active_truth_status": status}


def test_no_hits_is_confidence_none():
    result = assess_confidence([])
    assert result.level == "none"
    assert result.score == 0.0
    assert result.top_source_status is None


def test_high_score_active_source_is_high_confidence():
    result = assess_confidence([_hit(score=0.9, status="active")])
    assert result.level == "high"
    assert result.top_source_status == "active"


def test_high_score_historical_source_is_capped_at_medium():
    """The core Founder Knowledge Studio safety guarantee: a strong similarity match against
    historical/proposed/superseded/disputed material can never alone produce "high"
    confidence — the source's own declared status caps it."""
    result = assess_confidence([_hit(score=0.95, status="historical")])
    assert result.level == "medium"
    assert result.top_source_status == "historical"


def test_high_score_disputed_source_is_also_capped():
    result = assess_confidence([_hit(score=0.99, status="disputed")])
    assert result.level == "medium"


def test_medium_score_historical_source_stays_medium_not_upgraded():
    """The cap only ever pulls confidence down, never up — a mediocre match against
    historical material doesn't get promoted just because it's "only" medium already."""
    result = assess_confidence([_hit(score=0.6, status="historical")])
    assert result.level == "medium"


def test_top_hit_by_score_determines_status_not_first_in_list():
    hits = [_hit(document_id="a" * 8 + "-1111-1111-1111-111111111111", score=0.3, status="disputed"), _hit(score=0.9, status="active")]
    result = assess_confidence(hits)
    assert result.level == "high"
    assert result.top_source_status == "active"


def test_conflicts_detected_flag_propagates():
    result = assess_confidence([_hit()], conflicting_pairs=[("a", "b")])
    assert result.conflicts_detected is True
    assert result.conflicting_document_ids == [("a", "b")]


def test_no_conflicting_pairs_means_no_conflict():
    result = assess_confidence([_hit()], conflicting_pairs=[])
    assert result.conflicts_detected is False


def test_build_trust_instructions_includes_status_warning_for_non_active_top_source():
    text = build_trust_instructions("medium", top_source_status="superseded")
    assert STATUS_INSTRUCTIONS["superseded"] in text


def test_build_trust_instructions_omits_status_warning_for_active_source():
    text = build_trust_instructions("high", top_source_status="active")
    for warning in STATUS_INSTRUCTIONS.values():
        assert warning not in text


def test_build_trust_instructions_includes_conflict_warning():
    text = build_trust_instructions("medium", conflicts_detected=True)
    assert CONFLICT_INSTRUCTION in text


def test_build_trust_instructions_omits_conflict_warning_by_default():
    text = build_trust_instructions("high")
    assert CONFLICT_INSTRUCTION not in text


def _set_rls_user(session, owner_id) -> None:
    from sqlalchemy import text as sa_text

    from app.request_context import current_user_id as current_user_id_var

    # Sets the contextvar too, not just the raw SQL setting — detect_conflicts() below runs
    # its own SELECT in a fresh transaction (after this test's own db.commit() ends the one
    # SET LOCAL applied to), and app/db.py's after_begin listener only re-applies RLS by
    # reading this contextvar. See app/rag/library_import.py's identical fix/comment.
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_document(session, owner_id, title="Dok") -> Document:
    # documents and source_relationships are both RLS-protected (migration 0006) — each
    # needs app.current_user_id freshly re-set right before its own insert, since the
    # previous db.commit() (from the last _make_document call, if any) already ended the
    # transaction that setting was scoped to. See test_rls_isolation.py's identical note.
    _set_rls_user(session, owner_id)
    document = Document(title=title, source=DocumentSource.upload, uploaded_by=owner_id)
    session.add(document)
    session.commit()
    return document


class TestDetectConflicts:
    def test_returns_empty_for_fewer_than_two_documents(self, db_session, make_verified_user):
        user, _ = make_verified_user()
        doc = _make_document(db_session, user.id)
        assert detect_conflicts(db_session, user.id, [str(doc.id)]) == []
        assert detect_conflicts(db_session, user.id, []) == []

    def test_finds_a_recorded_contradiction_between_two_retrieved_documents(self, db_session, make_verified_user):
        user, _ = make_verified_user()
        doc_a = _make_document(db_session, user.id, "A")
        doc_b = _make_document(db_session, user.id, "B")
        _set_rls_user(db_session, user.id)
        db_session.add(
            SourceRelationship(
                owner_id=user.id, from_source_id=doc_a.id, to_source_id=doc_b.id, relationship_type=RelationshipType.contradicts
            )
        )
        db_session.commit()

        pairs = detect_conflicts(db_session, user.id, [str(doc_a.id), str(doc_b.id)])
        assert pairs == [(str(doc_a.id), str(doc_b.id))]

    def test_ignores_relationships_not_of_type_contradicts(self, db_session, make_verified_user):
        user, _ = make_verified_user()
        doc_a = _make_document(db_session, user.id, "A")
        doc_b = _make_document(db_session, user.id, "B")
        _set_rls_user(db_session, user.id)
        db_session.add(
            SourceRelationship(
                owner_id=user.id, from_source_id=doc_a.id, to_source_id=doc_b.id, relationship_type=RelationshipType.supersedes
            )
        )
        db_session.commit()

        assert detect_conflicts(db_session, user.id, [str(doc_a.id), str(doc_b.id)]) == []

    def test_ignores_a_contradiction_where_only_one_side_was_actually_retrieved(self, db_session, make_verified_user):
        """A 'contradicts' edge exists in the founder's library, but this particular chat
        answer only actually retrieved one of the two documents — no conflict should be
        reported for material that isn't even part of the current answer."""
        user, _ = make_verified_user()
        doc_a = _make_document(db_session, user.id, "A")
        doc_b = _make_document(db_session, user.id, "B")
        doc_c = _make_document(db_session, user.id, "C")
        _set_rls_user(db_session, user.id)
        db_session.add(
            SourceRelationship(
                owner_id=user.id, from_source_id=doc_a.id, to_source_id=doc_b.id, relationship_type=RelationshipType.contradicts
            )
        )
        db_session.commit()

        assert detect_conflicts(db_session, user.id, [str(doc_a.id), str(doc_c.id)]) == []
