"""Founder-to-Blueprint connected acceptance path — integration coverage for the stages the
existing Blueprint tests do NOT already cover.

The existing suite already proves (do not duplicate): the full HTTP workflow + real pgvector
retrieval + RLS + persistence/restart (test_founder_alpha_blueprint_e2e), versioning + ungrounded
refusal + cross-owner isolation + forge-id refusal + synthesis-failure rollback
(test_blueprint_draft), and approval immutability/authority (test_blueprint_approval_security).

This module extends coverage for the genuinely-missing acceptance stages named by the Founder
directive (2026-11-09):
  * STALE EVIDENCE — a draft grounded in a source later reclassified (ActiveTruthStatus) or
    removed is detectably stale (new read-only helper blueprint_source_staleness).
  * MISSING INFORMATION — a draft honestly carries the open questions / unresolved dependencies
    its synthesis could not ground (new optional open_questions -> provenance).
  * REVISION PRESERVES HISTORY — a revised Blueprint is a new immutable version; the prior one is
    preserved unchanged.
  * NO EXECUTION ON DRAFT — creating a draft authorizes NO agent dispatch / code execution.

SYNTHETIC-EVIDENCE LABEL: the synthesis step here is a deterministic in-process double
(`_synthetic_synthesizer`), NOT a real external-provider call. Retrieval `similarity`/`excerpt`
values are likewise synthetic. The real-model synthesis + real pgvector retrieval are proven in
test_founder_alpha_blueprint_e2e / the model-backed CI job; nothing here claims real external
execution. Everything else (RLS/ownership, persistence, versioning, ActiveTruthStatus) is real
against PostgreSQL.
"""

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.blueprint.service import (
    BlueprintError,
    blueprint_source_staleness,
    generate_blueprint_draft,
    latest_version,
    list_blueprint_drafts,
)
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.request_context import current_user_id as current_user_id_var


def _synthetic_synthesizer(title, query, hits):
    """Deterministic SYNTHETIC synthesis double — never a real external provider."""
    return f"BLUEPRINT (synthetic synthesis) for {title!r} grounded strictly in {len(hits)} source(s)."


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_source(session, owner_id, title, *, status=ActiveTruthStatus.active) -> Document:
    _set_rls_user(session, owner_id)
    doc = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        active_truth_status=status,
        checksum=uuid.uuid4().hex,
        original_filename=f"{title}.txt",
    )
    session.add(doc)
    session.commit()
    return doc


def _hit(document_id, excerpt="synthetic excerpt"):
    # Retrieval-result shape generate_blueprint_draft grounds on; similarity/excerpt are synthetic.
    return {"document_id": str(document_id), "similarity": 0.9, "excerpt": excerpt}


# --- STALE EVIDENCE ---------------------------------------------------------------------------

def test_stale_source_evidence_is_detected_after_a_source_is_superseded(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc_a = _make_source(db_session, user.id, "Policy A")
    doc_b = _make_source(db_session, user.id, "Policy B")

    draft = generate_blueprint_draft(
        db_session,
        owner_id=user.id,
        title="Expansion plan",
        source_hits=[_hit(doc_a.id), _hit(doc_b.id)],
        synthesizer=_synthetic_synthesizer,
    )
    db_session.commit()

    # Fresh — both sources active -> not stale.
    fresh = blueprint_source_staleness(db_session, owner_id=user.id, draft=draft)
    assert fresh["is_stale"] is False
    assert fresh["stale_source_ids"] == []

    # Founder reclassifies one source as historical -> the draft's evidence is now partly stale.
    _set_rls_user(db_session, user.id)
    doc_a.active_truth_status = ActiveTruthStatus.historical
    db_session.commit()

    stale = blueprint_source_staleness(db_session, owner_id=user.id, draft=draft)
    assert stale["is_stale"] is True
    assert str(doc_a.id) in stale["stale_source_ids"]
    assert str(doc_b.id) not in stale["stale_source_ids"]
    assert any(d["reason"] == "historical" for d in stale["details"])


def test_removed_source_evidence_is_detected_as_missing(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id, "Grundkälla")
    gone_id = uuid.uuid4()  # a source that no longer exists / was never the owner's

    draft = generate_blueprint_draft(
        db_session,
        owner_id=user.id,
        title="Plan med saknad källa",
        source_hits=[_hit(doc.id), _hit(gone_id)],
        synthesizer=_synthetic_synthesizer,
    )
    db_session.commit()

    result = blueprint_source_staleness(db_session, owner_id=user.id, draft=draft)
    assert result["is_stale"] is True
    assert str(gone_id) in result["stale_source_ids"]
    assert any(d["document_id"] == str(gone_id) and d["reason"] == "missing" for d in result["details"])


# --- MISSING INFORMATION ----------------------------------------------------------------------

def test_blueprint_honestly_records_missing_information_open_questions(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id, "Delvis underlag")

    draft = generate_blueprint_draft(
        db_session,
        owner_id=user.id,
        title="Plan med öppna frågor",
        source_hits=[_hit(doc.id)],
        synthesizer=_synthetic_synthesizer,
        open_questions=[
            "Budgettak saknas i underlaget",
            "Ingen källa anger leveranstidslinje",
        ],
    )
    db_session.commit()

    assert draft.provenance["has_open_questions"] is True
    assert "Budgettak saknas i underlaget" in draft.provenance["open_questions"]
    # Backward compatibility: omitting open_questions yields an explicit empty list, not an error.
    plain = generate_blueprint_draft(
        db_session,
        owner_id=user.id,
        title="Plan utan öppna frågor",
        source_hits=[_hit(doc.id)],
        synthesizer=_synthetic_synthesizer,
    )
    db_session.commit()
    assert plain.provenance["has_open_questions"] is False
    assert plain.provenance["open_questions"] == []


def test_missing_information_at_the_limit_an_ungrounded_request_is_refused(db_session, make_verified_user):
    """The strongest 'missing information' case: NO authorized source at all -> refuse to invent
    a Blueprint (insufficient_sources), persisting nothing."""
    user, _ = make_verified_user()
    with pytest.raises(BlueprintError, match="insufficient_sources"):
        generate_blueprint_draft(
            db_session,
            owner_id=user.id,
            title="Helt ogrundad plan",
            source_hits=[],
            synthesizer=_synthetic_synthesizer,
        )


# --- REVISION PRESERVES HISTORY ---------------------------------------------------------------

def test_revision_creates_a_new_version_and_preserves_the_prior_immutably(db_session, make_verified_user):
    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id, "Underlag v1")

    v1 = generate_blueprint_draft(
        db_session, owner_id=user.id, title="Produktplan",
        source_hits=[_hit(doc.id, "ursprunglig excerpt")], synthesizer=_synthetic_synthesizer,
    )
    db_session.commit()
    v1_body = v1.body

    # A revision for the SAME title is a new, immutable version — not an in-place rewrite.
    v2 = generate_blueprint_draft(
        db_session, owner_id=user.id, title="Produktplan",
        source_hits=[_hit(doc.id, "reviderad excerpt")], synthesizer=_synthetic_synthesizer,
        open_questions=["Ny öppen fråga i v2"],
    )
    db_session.commit()

    assert v1.version == 1 and v2.version == 2
    assert latest_version(db_session, owner_id=user.id, title="Produktplan") == 2

    all_versions = [d for d in list_blueprint_drafts(db_session, owner_id=user.id, title="Produktplan")]
    assert [d.version for d in all_versions] == [1, 2]
    # The prior version is preserved byte-for-byte — history is not overwritten by the revision.
    db_session.refresh(v1)
    assert v1.body == v1_body
    assert v1.provenance.get("has_open_questions") is False


# --- NO EXECUTION ON DRAFT --------------------------------------------------------------------

def test_creating_a_draft_authorizes_no_agent_dispatch_or_execution(db_session, make_verified_user, monkeypatch):
    """Acceptance stage 10: generating a draft must NOT dispatch any agent or execute code. We
    arm the dispatch entry point to explode if touched and prove generation never reaches it."""
    import app.agent_coordination.dispatch as dispatch_mod

    def _explode(*args, **kwargs):
        raise AssertionError("generate_blueprint_draft must never dispatch an agent / execute code")

    monkeypatch.setattr(dispatch_mod, "dispatch_assignment", _explode, raising=False)

    user, _ = make_verified_user()
    doc = _make_source(db_session, user.id, "Underlag")
    draft = generate_blueprint_draft(
        db_session, owner_id=user.id, title="Plan utan exekvering",
        source_hits=[_hit(doc.id)], synthesizer=_synthetic_synthesizer,
    )
    db_session.commit()

    # Draft created, but strictly pending — approval/execution are separate, later, Founder-gated.
    assert draft.status == "draft"
    assert draft.provenance["pending_founder_approval"] is True
    assert draft.provenance["ai_generated"] is True
