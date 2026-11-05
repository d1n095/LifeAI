"""Blueprint draft generation — Founder Alpha item 8/9 (needs independent review).

Real Postgres (migration 0070 applied by conftest) + RLS. A real Document stands in for an
uploaded immutable original; a deterministic stub synthesizer stands in for the local model
(the real-model synthesis is exercised end-to-end in the Ollama CI, same split as #334/#336).
Verifies: versioning, source-traceability, draft-pending-approval, originals-not-mutated,
ungrounded-refused, explicit Founder approval, and cross-owner RLS isolation.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy import text as sa_text
from sqlalchemy.exc import IntegrityError

from app.blueprint.service import (
    BlueprintError,
    approve_blueprint_draft,
    generate_blueprint_draft,
    list_blueprint_drafts,
)
from app.models.blueprint_draft import BlueprintDraft
from app.models.document import Document
from app.models.user import User
from app.request_context import current_user_id as current_user_id_var


def _user(db) -> User:
    u = User(email=f"bp-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _doc(db, owner_id, *, title, checksum) -> Document:
    d = Document(title=title, uploaded_by=owner_id, checksum=checksum, original_filename=title)
    db.add(d)
    db.flush()
    return d


def _synth(title, query, hits):
    return f"DRAFT Blueprint: {title}\nGrounded in {len(hits)} source(s); query={query!r}."


def test_blueprint_draft_versioned_traceable_and_approval_gated(superuser_db):
    owner = _user(superuser_db)
    doc = _doc(superuser_db, owner.id, title="Vault memo", checksum="a" * 64)
    hits = [{"document_id": doc.id, "text": "Architectural decision about X.", "similarity": 0.91}]

    d1 = generate_blueprint_draft(
        superuser_db, owner_id=owner.id, title="Master Blueprint", source_hits=hits,
        synthesizer=_synth, query="summarize architecture", model_provider="ollama", model_name="qwen2.5:0.5b",
    )
    assert d1.version == 1 and d1.status == "draft"
    # Traceable to the immutable original.
    assert str(doc.id) in d1.source_document_ids
    assert d1.source_refs[0]["document_id"] == str(doc.id)
    # AI interpretation, explicitly a draft pending approval, originals not mutated.
    assert d1.provenance["ai_generated"] is True
    assert d1.provenance["originals_mutated"] is False
    assert d1.provenance["pending_founder_approval"] is True
    assert d1.model_provider == "ollama" and d1.model_name == "qwen2.5:0.5b"
    # The original Document is untouched by generation.
    superuser_db.refresh(doc)
    assert doc.checksum == "a" * 64 and doc.title == "Vault memo"

    # Regeneration is a new immutable version; v1 is retained.
    d2 = generate_blueprint_draft(
        superuser_db, owner_id=owner.id, title="Master Blueprint", source_hits=hits, synthesizer=_synth,
    )
    assert d2.version == 2
    assert len(list_blueprint_drafts(superuser_db, owner_id=owner.id, title="Master Blueprint")) == 2

    # Life never self-approves: d1 is still a draft after generation.
    assert d1.status == "draft"
    # Only an explicit Founder action approves. Approval runs through the owner-scoped SECURITY
    # DEFINER function (migrations 0071/0072) on the app's PRIVILEGED connection — the runtime role
    # is refused by the 0072 session_user guard. Because that runs on a separate connection, the
    # draft must be committed first (mirroring the real two-request flow: generate commits, then a
    # later request approves).
    superuser_db.commit()
    approved = approve_blueprint_draft(superuser_db, owner_id=owner.id, draft_id=d1.id, approved_by="founder")
    assert approved.status == "approved" and approved.approved_by == "founder"
    assert approved.provenance["pending_founder_approval"] is False


def test_ungrounded_draft_is_refused(superuser_db):
    owner = _user(superuser_db)
    with pytest.raises(BlueprintError):
        generate_blueprint_draft(
            superuser_db, owner_id=owner.id, title="No sources", source_hits=[], synthesizer=_synth,
        )
    # Nothing persisted.
    assert superuser_db.execute(
        select(func.count()).select_from(BlueprintDraft).where(BlueprintDraft.owner_id == owner.id)
    ).scalar_one() == 0


def test_cross_owner_isolation_rls(superuser_db, db_session):
    owner_a = _user(superuser_db)
    owner_b = _user(superuser_db)
    doc = _doc(superuser_db, owner_a.id, title="A doc", checksum="b" * 64)
    generate_blueprint_draft(
        superuser_db, owner_id=owner_a.id, title="A Blueprint",
        source_hits=[{"document_id": doc.id, "text": "x", "similarity": 0.8}], synthesizer=_synth,
    )
    superuser_db.commit()

    # Restricted runtime role scoped to owner B must not see owner A's drafts (RLS).
    current_user_id_var.set(str(owner_b.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_b.id)})
    assert db_session.execute(select(func.count()).select_from(BlueprintDraft)).scalar_one() == 0
    assert list_blueprint_drafts(db_session, owner_id=owner_a.id) == []

    # Scoped to owner A, the draft is visible — proving the zero above is RLS, not an empty DB.
    db_session.rollback()
    current_user_id_var.set(str(owner_a.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_a.id)})
    assert db_session.execute(select(func.count()).select_from(BlueprintDraft)).scalar_one() >= 1


def test_non_unique_integrity_error_is_not_relabelled_as_version_conflict(superuser_db):
    """Examiner P2 (PR #343 probe, adopted unmodified): a missing owner is an FK error, never a
    retryable version uniqueness race — it must surface as IntegrityError, not be swallowed and
    relabelled as version_allocation_conflict."""
    missing_owner = uuid.uuid4()
    with pytest.raises(IntegrityError):
        generate_blueprint_draft(
            superuser_db,
            owner_id=missing_owner,
            title="examiner-non-unique-integrity",
            source_hits=[
                {
                    "document_id": str(uuid.uuid4()),
                    "text": "examiner source",
                    "similarity": 1.0,
                }
            ],
            synthesizer=lambda _title, _query, _hits: "examiner body",
        )


def test_version_collision_retries_to_next_version(superuser_db, monkeypatch):
    """Positive side of P2: a genuine (owner,title,version) unique collision IS retried, and the
    retry allocates the next free version rather than failing."""
    import app.blueprint.service as svc

    owner = _user(superuser_db)
    doc = _doc(superuser_db, owner.id, title="src", checksum="c" * 64)
    hits = [{"document_id": doc.id, "text": "decision", "similarity": 0.9}]

    d1 = generate_blueprint_draft(superuser_db, owner_id=owner.id, title="Plan", source_hits=hits, synthesizer=_synth)
    superuser_db.commit()
    assert d1.version == 1

    # Force the first allocation attempt to recompute version 1 (collides with the committed
    # d1 → 23505 on ix_blueprint_drafts_owner_title_version → retry), then fall back to the real
    # max on the retry so it allocates version 2.
    real_latest = svc.latest_version
    calls = {"n": 0}

    def fake_latest(db, *, owner_id, title):
        calls["n"] += 1
        if calls["n"] == 1:
            return 0  # -> attempt version 1, which already exists
        return real_latest(db, owner_id=owner_id, title=title)

    monkeypatch.setattr(svc, "latest_version", fake_latest)

    d2 = generate_blueprint_draft(superuser_db, owner_id=owner.id, title="Plan", source_hits=hits, synthesizer=_synth)
    assert d2.version == 2
    assert calls["n"] >= 2  # it actually retried after the collision
