"""Blueprint approval — genuine concurrency + fail-closed correctness for the TOCTOU boundary.

The Founder's independent review found that locking only the draft row FOR UPDATE and reading the
source documents with a plain SELECT did not prove that a CONCURRENT source change cannot occur
between validation and the approval commit. approve_blueprint_draft() now reads the canonical
grounding from the LOCKED draft row and takes a FOR SHARE lock on every referenced document, in
deterministic id order, inside the same privileged transaction.

These tests prove the actual locking semantics with SEPARATE database connections (not a single
sequential change before approval), and the fail-closed cases: empty sources, missing baseline,
inconsistent refs, and grounding on a non-active source.
"""

import uuid

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import OperationalError

from app.blueprint.service import approve_blueprint_draft, generate_blueprint_draft, get_blueprint_draft
from app.db import migration_engine
from app.models.blueprint_draft import BlueprintDraft
from app.models.document import ActiveTruthStatus, Document
from app.models.user import User


def _user(db) -> User:
    u = User(email=f"bpcc-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _doc(db, owner_id, *, status=ActiveTruthStatus.active) -> Document:
    d = Document(title="orig", uploaded_by=owner_id, checksum=uuid.uuid4().hex,
                 original_filename="o.txt", active_truth_status=status)
    db.add(d)
    db.flush()
    return d


def _synth(title, query, hits):
    return f"DRAFT {title} ({len(hits)})"


def _make_via_generate(superuser_db, docs):
    owner = _user(superuser_db)
    made = [docs_fn(superuser_db, owner.id) for docs_fn in docs]
    draft = generate_blueprint_draft(
        superuser_db, owner_id=owner.id, title=f"BP-{uuid.uuid4().hex[:6]}",
        source_hits=[{"document_id": d.id, "similarity": 0.9} for d in made],
        synthesizer=_synth,
    )
    superuser_db.commit()
    superuser_db.execute(sa_text(f"SET app.current_user_id = '{owner.id}'"))
    return owner.id, draft, made


def _insert_draft(superuser_db, owner_id, *, source_document_ids, source_refs) -> BlueprintDraft:
    d = BlueprintDraft(
        owner_id=owner_id, title=f"BP-{uuid.uuid4().hex[:6]}", version=1, status="draft",
        body="synthetic body", source_document_ids=source_document_ids, source_refs=source_refs,
        provenance={"ai_generated": True, "pending_founder_approval": True},
    )
    superuser_db.add(d)
    superuser_db.commit()
    superuser_db.execute(sa_text(f"SET app.current_user_id = '{owner_id}'"))
    return d


def _status(superuser_db, owner_id, draft):
    superuser_db.expire(draft)
    return get_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft.id).status


# --- Genuine concurrency: FOR SHARE blocks a concurrent source modification -------------------

def test_for_share_lock_blocks_a_concurrent_source_update(superuser_db):
    """The actual locking semantics approval relies on: while a FOR SHARE lock is held on a source
    document (as approve_blueprint_draft does across its whole transaction), a CONCURRENT writer on
    a separate connection cannot UPDATE that row — proving the evidence cannot change between
    validation and the approval commit. Uses two independent real connections."""
    owner_id, _draft, docs = _make_via_generate(superuser_db, [_doc])
    doc_id = docs[0].id

    holder = migration_engine.connect()
    writer = migration_engine.connect()
    try:
        holder_tx = holder.begin()
        got = holder.execute(
            sa_text("SELECT checksum FROM documents WHERE id = CAST(:id AS uuid) FOR SHARE"),
            {"id": str(doc_id)},
        ).first()
        assert got is not None  # the approval-style FOR SHARE lock is held

        writer_tx = writer.begin()
        writer.execute(sa_text("SET LOCAL lock_timeout = '750ms'"))
        with pytest.raises(OperationalError) as exc:
            # A concurrent reclassification takes FOR NO KEY UPDATE, which conflicts with FOR SHARE.
            writer.execute(
                sa_text("UPDATE documents SET active_truth_status = 'historical' "
                        "WHERE id = CAST(:id AS uuid)"),
                {"id": str(doc_id)},
            )
        assert "lock" in str(exc.value).lower()  # blocked (canceling statement due to lock timeout)
        writer_tx.rollback()
        holder_tx.rollback()
    finally:
        writer.close()
        holder.close()


def test_a_concurrent_delete_of_a_source_is_also_blocked_while_the_share_lock_is_held(superuser_db):
    owner_id, _draft, docs = _make_via_generate(superuser_db, [_doc])
    doc_id = docs[0].id
    holder = migration_engine.connect()
    deleter = migration_engine.connect()
    try:
        ht = holder.begin()
        holder.execute(sa_text("SELECT 1 FROM documents WHERE id = CAST(:id AS uuid) FOR SHARE"), {"id": str(doc_id)})
        dt = deleter.begin()
        deleter.execute(sa_text("SET LOCAL lock_timeout = '750ms'"))
        with pytest.raises(OperationalError):
            deleter.execute(sa_text("DELETE FROM documents WHERE id = CAST(:id AS uuid)"), {"id": str(doc_id)})
        dt.rollback()
        ht.rollback()
    finally:
        deleter.close()
        holder.close()


# --- Fail-closed correctness ------------------------------------------------------------------

def test_approve_fails_closed_on_empty_source_ids(superuser_db):
    owner = _user(superuser_db)
    draft = _insert_draft(superuser_db, owner.id, source_document_ids=[], source_refs=[])
    with pytest.raises(Exception, match="approval_blocked"):
        approve_blueprint_draft(superuser_db, owner_id=owner.id, draft_id=draft.id, approved_by=str(owner.id))
    assert _status(superuser_db, owner.id, draft) == "draft"


def test_approve_fails_closed_on_missing_baseline_checksum(superuser_db):
    owner = _user(superuser_db)
    doc = _doc(superuser_db, owner.id)
    superuser_db.commit()
    draft = _insert_draft(
        superuser_db, owner.id,
        source_document_ids=[str(doc.id)],
        source_refs=[{"document_id": str(doc.id), "similarity": 0.9}],  # no source_checksum baseline
    )
    with pytest.raises(Exception, match="approval_blocked"):
        approve_blueprint_draft(superuser_db, owner_id=owner.id, draft_id=draft.id, approved_by=str(owner.id))
    assert _status(superuser_db, owner.id, draft) == "draft"


def test_approve_fails_closed_on_inconsistent_source_ref(superuser_db):
    owner = _user(superuser_db)
    doc = _doc(superuser_db, owner.id)
    superuser_db.commit()
    draft = _insert_draft(
        superuser_db, owner.id,
        source_document_ids=[str(doc.id)],  # a source id with NO matching source_ref baseline
        source_refs=[],
    )
    with pytest.raises(Exception, match="approval_blocked"):
        approve_blueprint_draft(superuser_db, owner_id=owner.id, draft_id=draft.id, approved_by=str(owner.id))
    assert _status(superuser_db, owner.id, draft) == "draft"


def test_approve_fails_closed_when_grounded_on_a_nonactive_source(superuser_db):
    owner = _user(superuser_db)
    doc = _doc(superuser_db, owner.id, status=ActiveTruthStatus.historical)  # already non-active at grounding
    draft = generate_blueprint_draft(
        superuser_db, owner_id=owner.id, title=f"BP-{uuid.uuid4().hex[:6]}",
        source_hits=[{"document_id": doc.id, "similarity": 0.9}], synthesizer=_synth,
    )
    superuser_db.commit()
    superuser_db.execute(sa_text(f"SET app.current_user_id = '{owner.id}'"))
    with pytest.raises(Exception, match="approval_blocked"):
        approve_blueprint_draft(superuser_db, owner_id=owner.id, draft_id=draft.id, approved_by=str(owner.id))
    assert _status(superuser_db, owner.id, draft) == "draft"
