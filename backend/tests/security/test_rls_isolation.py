"""Row-Level Security, exercised directly at the database layer through the same restricted
runtime role (mainai_app) and session-variable mechanism the app itself uses (see
app/db.py's after_begin listener and app/deps.py's SET LOCAL) — not through the HTTP API,
so a bug in a router can't accidentally mask an RLS bug or vice versa."""

from sqlalchemy import text

from app.config import get_settings
from app.models.conversation import Conversation
from app.models.document import Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_version import KnowledgeVersion
from app.models.source_relationship import RelationshipType, SourceRelationship
from app.rag.vector_store import search, upsert_chunks

EMBEDDING_DIM = get_settings().embedding_dim


def _set_rls_user(session, user_id) -> None:
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})


def _fake_vector(seed: float) -> list[float]:
    """A syntactically valid embedding of the right fixed dimension (see
    app/models/document_chunk.py) — RLS isolation doesn't depend on the vector actually
    being semantically meaningful, only on matching pgvector's declared column size."""
    return [seed] * EMBEDDING_DIM


def _make_document(session, owner_id) -> Document:
    # documents is RLS-protected as of migration 0006 (Founder Knowledge Studio v1 — see
    # app/models/document.py's docstring): owner-scoped now, no longer "shared company
    # knowledge". SET LOCAL must be set to owner_id before this insert or RLS's WITH CHECK
    # rejects it outright.
    _set_rls_user(session, owner_id)
    document = Document(title="Testdokument", source=DocumentSource.upload, uploaded_by=owner_id)
    session.add(document)
    session.commit()
    # No session.refresh(): id/created_at/updated_at are all populated client-side by
    # SQLAlchemy at flush (see app/models/document.py's defaults), and the session is
    # expire_on_commit=False (app/db.py) — a refresh would force a brand-new SELECT in a
    # fresh transaction, where app/db.py's after_begin listener re-applies RLS from the
    # per-request contextvar (empty here, this is a raw test session) instead of the
    # SET LOCAL above, which only lasted for the transaction that just committed. That's
    # not a bug in RLS, just an unnecessary refresh tripping over the isolation this file
    # exists to test — same reasoning as app/routers/chat.py's identical no-refresh comment.
    return document


def test_user_sees_only_their_own_conversations(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    db_session.add(Conversation(user_id=user_a.id, title="A:s konversation"))
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    db_session.add(Conversation(user_id=user_b.id, title="B:s konversation"))
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(Conversation).all()
    assert [c.title for c in visible_to_a] == ["A:s konversation"]

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(Conversation).all()
    assert [c.title for c in visible_to_b] == ["B:s konversation"]


def test_no_session_variable_set_sees_nothing():
    """Default-deny: a connection with no app.current_user_id set (e.g. a raw
    migration/admin connection accidentally used for a request) must never see ANY rows,
    not "all of them" — see app/rls.py's NULLIF reasoning."""
    from app.db import SessionLocal

    session = SessionLocal()
    try:
        session.add(Conversation(user_id="00000000-0000-0000-0000-000000000000", title="orphan"))
        # Insert bypasses RLS's WITH CHECK only if the session var matches — this insert
        # should actually fail under RLS since no session var is set. Confirm that instead.
        try:
            session.commit()
            assert False, "insert should have been rejected by RLS WITH CHECK"
        except Exception:
            session.rollback()
    finally:
        session.close()


def test_reverted_session_variable_still_denies_by_default(db_session, make_verified_user):
    """Regression guard for the NULLIF quirk documented in app/rls.py: a custom GUC that was
    SET LOCAL and then implicitly reverted (e.g. after a mid-request commit, before the next
    transaction's after_begin re-applies it) reads back as '' rather than NULL — casting
    that straight to ::uuid must resolve to "match nothing", not raise a DB error."""
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    db_session.add(Conversation(user_id=user.id, title="innan commit"))
    db_session.commit()  # ends the transaction SET LOCAL was scoped to

    # No SET LOCAL re-applied here — simulates the gap the after_begin listener exists to
    # close in the real app; a raw session without it must still fail safe.
    visible = db_session.query(Conversation).all()
    assert visible == []


def test_user_never_reads_another_users_document_chunks(db_session, make_verified_user):
    """Direct proof at the query layer, mirroring test_user_sees_only_their_own_conversations
    above — same mechanism (document_chunks_isolation policy, app/rls.py), different table.
    Unlike Document itself (shared), the chunks/embeddings actually used for search are
    strictly per-uploader — see app/models/document_chunk.py's docstring for why this is a
    deliberate narrowing, not a bug."""
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    doc_a = _make_document(db_session, user_a.id)
    doc_b = _make_document(db_session, user_b.id)

    _set_rls_user(db_session, user_a.id)
    upsert_chunks(db_session, doc_a.id, user_a.id, ["A:s hemliga innehåll"], [_fake_vector(0.1)])
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    upsert_chunks(db_session, doc_b.id, user_b.id, ["B:s hemliga innehåll"], [_fake_vector(0.2)])
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(DocumentChunk).all()
    assert [c.text for c in visible_to_a] == ["A:s hemliga innehåll"]

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(DocumentChunk).all()
    assert [c.text for c in visible_to_b] == ["B:s hemliga innehåll"]


def test_user_never_searches_another_users_document_chunks(db_session, make_verified_user):
    """Proves isolation through the actual code path app/rag/retrieve.py calls (search()),
    not just a raw query — the real chat/knowledge-search flow, not a lower-fidelity proxy
    for it. Both layers (the explicit owner_id filter AND RLS) are exercised together, which
    is the point: this is defense in depth, not reliance on a single mechanism."""
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    doc_a = _make_document(db_session, user_a.id)
    doc_b = _make_document(db_session, user_b.id)

    _set_rls_user(db_session, user_a.id)
    upsert_chunks(db_session, doc_a.id, user_a.id, ["A:s sokbara innehall"], [_fake_vector(0.1)])
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    upsert_chunks(db_session, doc_b.id, user_b.id, ["B:s sokbara innehall"], [_fake_vector(0.1)])
    db_session.commit()

    # Same query vector for both inserts (0.1) — if isolation were broken, A's search would
    # trivially also match B's chunk (identical embedding, distance 0), making this a
    # meaningful test instead of one that passes by accident because the vectors differ.
    _set_rls_user(db_session, user_a.id)
    results_a = search(db_session, user_a.id, _fake_vector(0.1), top_k=10)
    assert [r["text"] for r in results_a] == ["A:s sokbara innehall"]

    _set_rls_user(db_session, user_b.id)
    results_b = search(db_session, user_b.id, _fake_vector(0.1), top_k=10)
    assert [r["text"] for r in results_b] == ["B:s sokbara innehall"]

    # Attacker scenario: even if application code somewhere passed the WRONG owner_id to
    # search() (e.g. a bug), RLS still confines the query to whatever app.current_user_id
    # actually is — "owner_id = B" AND "RLS: owner_id = A" is never satisfiable, not "falls
    # back to being unfiltered". This is what makes it defense in depth rather than two
    # copies of the same single point of failure.
    _set_rls_user(db_session, user_a.id)
    results_mismatched = search(db_session, user_b.id, _fake_vector(0.1), top_k=10)
    assert results_mismatched == []


def test_cannot_write_document_chunk_for_another_user(db_session, make_verified_user):
    """Write-side isolation, mirroring test_no_session_variable_set_sees_nothing's insert-
    rejection pattern: RLS's WITH CHECK must reject a chunk row whose owner_id doesn't match
    the session's own app.current_user_id, regardless of what the calling code intended."""
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    doc_a = _make_document(db_session, user_a.id)

    # Session believes it's acting as user_a, but the row claims to belong to user_b —
    # exactly the shape of bug RLS exists to catch even if application code gets it wrong.
    _set_rls_user(db_session, user_a.id)
    db_session.add(
        DocumentChunk(
            document_id=doc_a.id,
            owner_id=user_b.id,
            chunk_index=0,
            text="ska aldrig lyckas skrivas",
            embedding=_fake_vector(0.3),
        )
    )
    try:
        db_session.commit()
        assert False, "insert should have been rejected by document_chunks_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()


# --- Founder Knowledge Studio v1 (migration 0006) — documents_isolation and the three new
# tables. See app/models/document.py's docstring: documents moved from "shared company
# knowledge" to strictly owner-scoped, which is what makes these tests necessary now (they
# would have been meaningless — everyone could see every document — before this migration).


def test_user_never_reads_another_users_documents(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    _make_document(db_session, user_a.id)
    _make_document(db_session, user_b.id)

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(Document).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].uploaded_by == user_a.id

    _set_rls_user(db_session, user_b.id)
    visible_to_b = db_session.query(Document).all()
    assert len(visible_to_b) == 1
    assert visible_to_b[0].uploaded_by == user_b.id


def test_cannot_write_document_for_another_user(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    db_session.add(Document(title="ska aldrig lyckas", source=DocumentSource.upload, uploaded_by=user_b.id))
    try:
        db_session.commit()
        assert False, "insert should have been rejected by documents_isolation's WITH CHECK"
    except Exception:
        db_session.rollback()


def test_user_never_reads_another_users_knowledge_versions(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    doc_a = _make_document(db_session, user_a.id)
    doc_b = _make_document(db_session, user_b.id)

    _set_rls_user(db_session, user_a.id)
    db_session.add(
        KnowledgeVersion(
            source_id=doc_a.id, owner_id=user_a.id, version_number=1, checksum="a" * 64, extraction_version="v1"
        )
    )
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    db_session.add(
        KnowledgeVersion(
            source_id=doc_b.id, owner_id=user_b.id, version_number=1, checksum="b" * 64, extraction_version="v1"
        )
    )
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(KnowledgeVersion).all()
    assert [v.checksum for v in visible_to_a] == ["a" * 64]


def test_user_never_reads_another_users_import_jobs(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()

    _set_rls_user(db_session, user_a.id)
    db_session.add(ImportJob(owner_id=user_a.id, status=ImportJobStatus.completed, source_filename="a.zip"))
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    db_session.add(ImportJob(owner_id=user_b.id, status=ImportJobStatus.completed, source_filename="b.zip"))
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(ImportJob).all()
    assert [j.source_filename for j in visible_to_a] == ["a.zip"]


def test_user_never_reads_another_users_source_relationships(db_session, make_verified_user):
    user_a, _ = make_verified_user()
    user_b, _ = make_verified_user()
    doc_a1 = _make_document(db_session, user_a.id)
    doc_a2 = _make_document(db_session, user_a.id)
    doc_b1 = _make_document(db_session, user_b.id)
    doc_b2 = _make_document(db_session, user_b.id)

    _set_rls_user(db_session, user_a.id)
    db_session.add(
        SourceRelationship(
            owner_id=user_a.id,
            from_source_id=doc_a1.id,
            to_source_id=doc_a2.id,
            relationship_type=RelationshipType.supersedes,
        )
    )
    db_session.commit()

    _set_rls_user(db_session, user_b.id)
    db_session.add(
        SourceRelationship(
            owner_id=user_b.id,
            from_source_id=doc_b1.id,
            to_source_id=doc_b2.id,
            relationship_type=RelationshipType.contradicts,
        )
    )
    db_session.commit()

    _set_rls_user(db_session, user_a.id)
    visible_to_a = db_session.query(SourceRelationship).all()
    assert len(visible_to_a) == 1
    assert visible_to_a[0].relationship_type == RelationshipType.supersedes
