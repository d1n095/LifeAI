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
    # Only an explicit Founder action approves.
    approve_blueprint_draft(superuser_db, owner_id=owner.id, draft_id=d1.id, approved_by="founder")
    superuser_db.refresh(d1)
    assert d1.status == "approved" and d1.approved_by == "founder"
    assert d1.provenance["pending_founder_approval"] is False


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
