"""Phase 1 acceptance: bounded candidate lookup against real Document rows.

These tests use the real Postgres test database, the real RLS policies and the real
content-addressed storage backend — not mocks. They deliberately do NOT run the full
embedding pipeline: the atlas reads documents that `app/rag/library_import.py` already
stored, and this suite proves the lookup works against that same storage/DB contract.
"""

from __future__ import annotations

import uuid

from sqlalchemy import text

from app.context_atlas.candidates import find_candidates
from app.models.document import Document, IndexStatus
from app.models.source_relationship import SourceRelationship
from app.request_context import current_user_id as current_user_id_var
from app.storage import get_storage


def _store(content: bytes):
    storage = get_storage()
    position = 0

    def read_chunk() -> bytes:
        nonlocal position
        chunk = content[position : position + (1 << 20)]
        position += len(chunk)
        return chunk

    return storage.write_stream(read_chunk, max_bytes=16 * 1024 * 1024)


def _scope(db, user) -> None:
    current_user_id_var.set(str(user.id))
    db.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})


def _document(db, owner, content: bytes, *, title: str = "source", project_id=None) -> Document:
    blob = _store(content)
    _scope(db, owner)
    document = Document(
        title=title,
        uploaded_by=owner.id,
        checksum=blob.sha256,
        storage_key=blob.storage_key,
        size_bytes=blob.size_bytes,
        status=IndexStatus.indexed,
        project_id=project_id,
    )
    db.add(document)
    db.commit()
    db.refresh(document)
    return document


def test_exact_identity_uses_the_checksum_index(db_session, make_verified_user):
    owner, _ = make_verified_user()
    content = b"the same source bytes"
    document = _document(db_session, owner, content)

    report = find_candidates(db_session, owner_id=owner.id, content=content)

    assert report.lookup_authoritative is True
    assert [c.document_id for c in report.exact] == [str(document.id)]
    assert report.exact[0].verdict.value == "exact_byte_identical"
    assert report.exact[0].evidence == "checksum_index"
    assert report.related == ()
    # No edge is recorded merely because two payloads match.
    assert db_session.query(SourceRelationship).count() == 0


def test_whitespace_variant_is_a_candidate_not_identity(db_session, make_verified_user):
    owner, _ = make_verified_user()
    _document(db_session, owner, b"alpha beta gamma\n")

    report = find_candidates(db_session, owner_id=owner.id, content=b"alpha    beta gamma")

    assert report.exact == ()
    assert report.lookup_authoritative is False
    assert len(report.related) == 1
    assert report.related[0].verdict.value == "normalized_identical"
    assert report.related[0].requires_review is True


def test_one_character_change_is_detected(db_session, make_verified_user):
    owner, _ = make_verified_user()
    _document(db_session, owner, b"the quick brown fox")

    report = find_candidates(db_session, owner_id=owner.id, content=b"the quick brown fix")

    assert report.exact == ()
    assert [c.verdict.value for c in report.related] == ["one_character_edit"]


def test_near_duplicate_is_review_material(db_session, make_verified_user):
    owner, _ = make_verified_user()
    words = " ".join(f"token{i}" for i in range(20))
    _document(db_session, owner, words.encode())
    incoming = words.replace("token0", "different").encode()

    report = find_candidates(db_session, owner_id=owner.id, content=incoming)

    assert report.exact == ()
    assert len(report.related) == 1
    assert report.related[0].verdict.value == "near_duplicate"
    assert report.related[0].similarity is not None


def test_cross_tenant_lookup_is_denied(db_session, make_verified_user):
    owner_a, _ = make_verified_user()
    owner_b, _ = make_verified_user()
    content = b"shared-looking bytes"
    document_a = _document(db_session, owner_a, content, title="a")
    document_b = _document(db_session, owner_b, content, title="b")

    _scope(db_session, owner_a)
    report_a = find_candidates(db_session, owner_id=owner_a.id, content=content)
    assert [c.document_id for c in report_a.exact] == [str(document_a.id)]

    _scope(db_session, owner_b)
    report_b = find_candidates(db_session, owner_id=owner_b.id, content=content)
    assert [c.document_id for c in report_b.exact] == [str(document_b.id)]

    # Without a bound user, RLS fails closed: no rows at all.
    from app.db import SessionLocal

    token = current_user_id_var.set(None)
    anonymous = SessionLocal()
    try:
        report_anonymous = find_candidates(anonymous, owner_id=owner_a.id, content=content)
        assert report_anonymous.exact == ()
    finally:
        anonymous.close()
        current_user_id_var.reset(token)


def test_scan_is_bounded_and_reports_truncation(db_session, make_verified_user):
    owner, _ = make_verified_user()
    base = " ".join(f"word{i}" for i in range(30))
    for index in range(3):
        _document(
            db_session,
            owner,
            base.replace("word0", f"variant{index}").encode(),
            title=f"variant-{index}",
        )

    report = find_candidates(db_session, owner_id=owner.id, content=base.encode(), max_scan=2)

    assert report.scanned_documents == 2
    assert report.truncated is True
    assert len(report.related) <= 2


def test_missing_original_is_reported_not_fatal(db_session, make_verified_user):
    owner, _ = make_verified_user()
    _scope(db_session, owner)
    document = Document(
        title="orphaned",
        uploaded_by=owner.id,
        checksum="0" * 64,
        storage_key=None,
        size_bytes=12,
        status=IndexStatus.indexed,
    )
    db_session.add(document)
    db_session.commit()
    db_session.refresh(document)

    report = find_candidates(db_session, owner_id=owner.id, content=b"some content")

    assert str(document.id) in report.unreadable_originals
    assert report.related == ()


def test_repeated_lookup_after_restart_is_stable_and_read_only(db_session, make_verified_user):
    owner, _ = make_verified_user()
    content = b"durable lookup input"
    _document(db_session, owner, content)
    documents_before = db_session.query(Document).count()

    first = find_candidates(db_session, owner_id=owner.id, content=content)
    db_session.close()

    from app.db import SessionLocal

    fresh = SessionLocal()
    try:
        _scope(fresh, owner)
        second = find_candidates(fresh, owner_id=owner.id, content=content)
        assert second == first
        assert fresh.query(Document).count() == documents_before
        assert fresh.query(SourceRelationship).count() == 0
    finally:
        fresh.close()
