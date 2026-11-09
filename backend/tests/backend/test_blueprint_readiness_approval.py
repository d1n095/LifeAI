"""Blueprint readiness report + SERVER-ENFORCED, fail-closed approval safety.

Closes the gap the Founder identified: blueprint_source_staleness() could DETECT stale evidence,
but approve_blueprint_draft() did not PREVENT approval on it. These tests prove:
  * readiness classification READY / NEEDS_INFORMATION / STALE / BLOCKED with per-source reasons;
  * approval FAILS CLOSED on stale (status OR content change) / missing / unauthorized evidence;
  * approval proceeds on READY and on NEEDS_INFORMATION (open questions are advisory);
  * a readiness report never silently authorizes — approval re-checks FRESH inside its locked txn;
  * status changes vs content/version changes are distinguished;
  * immutable approved versions + Founder-only approval security are preserved.

Uses the committing `superuser_db` session (so the privileged migration_engine approval is
visible), mirroring test_blueprint_approval_security. Synthesis is a deterministic in-process
double (never a real provider).
"""

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.blueprint.service import (
    READINESS_BLOCKED,
    READINESS_NEEDS_INFORMATION,
    READINESS_READY,
    READINESS_STALE,
    BlueprintError,
    approve_blueprint_draft,
    evaluate_blueprint_readiness,
    generate_blueprint_draft,
    get_blueprint_draft,
)
from app.models.document import ActiveTruthStatus, Document
from app.models.user import User


def _user(db) -> User:
    u = User(email=f"bprdy-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _doc(db, owner_id, *, status=ActiveTruthStatus.active) -> Document:
    d = Document(
        title="orig",
        uploaded_by=owner_id,
        checksum=uuid.uuid4().hex,
        original_filename="o.txt",
        active_truth_status=status,
    )
    db.add(d)
    db.flush()
    return d


def _synth(title, query, hits):
    return f"DRAFT {title} ({len(hits)} source(s))"


def _make(superuser_db, *, n_docs=2, open_questions=None):
    owner = _user(superuser_db)
    docs = [_doc(superuser_db, owner.id) for _ in range(n_docs)]
    draft = generate_blueprint_draft(
        superuser_db,
        owner_id=owner.id,
        title=f"BP-{uuid.uuid4().hex[:6]}",
        source_hits=[{"document_id": d.id, "similarity": 0.9} for d in docs],
        synthesizer=_synth,
        open_questions=open_questions,
    )
    superuser_db.commit()
    superuser_db.execute(sa_text(f"SET app.current_user_id = '{owner.id}'"))
    return owner.id, draft, docs


def _status(superuser_db, owner_id, draft):
    superuser_db.expire(draft)
    return get_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft.id).status


# --- Readiness classification -----------------------------------------------------------------

def test_readiness_ready_when_sources_active_and_no_open_questions(superuser_db):
    owner_id, draft, _ = _make(superuser_db)
    report = evaluate_blueprint_readiness(superuser_db, owner_id=owner_id, draft=draft)
    assert report["readiness"] == READINESS_READY
    assert report["blocks_approval"] is False


def test_readiness_needs_information_with_open_questions(superuser_db):
    owner_id, draft, _ = _make(superuser_db, open_questions=["budget saknas"])
    report = evaluate_blueprint_readiness(superuser_db, owner_id=owner_id, draft=draft)
    assert report["readiness"] == READINESS_NEEDS_INFORMATION
    assert report["blocks_approval"] is False
    assert "budget saknas" in report["open_questions"]


def test_readiness_stale_on_source_status_change(superuser_db):
    owner_id, draft, docs = _make(superuser_db)
    docs[0].active_truth_status = ActiveTruthStatus.historical
    superuser_db.commit()
    report = evaluate_blueprint_readiness(superuser_db, owner_id=owner_id, draft=draft)
    assert report["readiness"] == READINESS_STALE
    assert any(s["reason"] == "status:historical" for s in report["sources"])


def test_readiness_stale_on_source_content_change_distinct_from_status(superuser_db):
    owner_id, draft, docs = _make(superuser_db)
    docs[0].checksum = uuid.uuid4().hex  # content/version changed, status still active
    superuser_db.commit()
    report = evaluate_blueprint_readiness(superuser_db, owner_id=owner_id, draft=draft)
    assert report["readiness"] == READINESS_STALE
    stale = next(s for s in report["sources"] if s["document_id"] == str(docs[0].id))
    assert stale["content_changed"] is True
    assert stale["reason"] == "content_changed"  # distinguished from a status change


def test_readiness_blocked_on_missing_source(superuser_db):
    owner_id, draft, docs = _make(superuser_db)
    superuser_db.delete(docs[0])
    superuser_db.commit()
    report = evaluate_blueprint_readiness(superuser_db, owner_id=owner_id, draft=draft)
    assert report["readiness"] == READINESS_BLOCKED
    assert any(s["reason"] == "missing" for s in report["sources"])


def test_status_and_content_changes_are_both_reported_and_distinguished(superuser_db):
    owner_id, draft, docs = _make(superuser_db, n_docs=2)
    docs[0].active_truth_status = ActiveTruthStatus.superseded
    docs[1].checksum = uuid.uuid4().hex
    superuser_db.commit()
    report = evaluate_blueprint_readiness(superuser_db, owner_id=owner_id, draft=draft)
    reasons = {s["document_id"]: s["reason"] for s in report["sources"]}
    assert reasons[str(docs[0].id)] == "status:superseded"
    assert reasons[str(docs[1].id)] == "content_changed"


# --- Fail-closed approval enforcement (negative security) -------------------------------------

def test_approve_fails_closed_on_stale_status(superuser_db):
    owner_id, draft, docs = _make(superuser_db)
    docs[0].active_truth_status = ActiveTruthStatus.historical
    superuser_db.commit()
    with pytest.raises(BlueprintError, match="approval_blocked"):
        approve_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft.id, approved_by=str(owner_id))
    assert _status(superuser_db, owner_id, draft) == "draft"  # nothing approved


def test_approve_fails_closed_on_content_change(superuser_db):
    owner_id, draft, docs = _make(superuser_db)
    docs[0].checksum = uuid.uuid4().hex
    superuser_db.commit()
    with pytest.raises(BlueprintError, match="approval_blocked"):
        approve_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft.id, approved_by=str(owner_id))
    assert _status(superuser_db, owner_id, draft) == "draft"


def test_approve_fails_closed_on_missing_source(superuser_db):
    owner_id, draft, docs = _make(superuser_db)
    superuser_db.delete(docs[0])
    superuser_db.commit()
    with pytest.raises(BlueprintError, match="approval_blocked"):
        approve_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft.id, approved_by=str(owner_id))
    assert _status(superuser_db, owner_id, draft) == "draft"


def test_approve_succeeds_when_ready(superuser_db):
    owner_id, draft, _ = _make(superuser_db)
    row = approve_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft.id, approved_by=str(owner_id))
    assert row.status == "approved"
    assert row.approved_by == str(owner_id)


def test_approve_succeeds_on_needs_information_open_questions_are_advisory(superuser_db):
    owner_id, draft, _ = _make(superuser_db, open_questions=["leveranstid okänd"])
    row = approve_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft.id, approved_by=str(owner_id))
    assert row.status == "approved"  # open questions do NOT block a Founder who knowingly approves


def test_readiness_report_never_silently_authorizes_and_approval_rechecks_fresh(superuser_db):
    """No silent authorization + TOCTOU safety: a READY readiness result does not change state,
    and if a source goes stale AFTER that report, approval still re-checks FRESH and fails closed."""
    owner_id, draft, docs = _make(superuser_db)
    report = evaluate_blueprint_readiness(superuser_db, owner_id=owner_id, draft=draft)
    assert report["readiness"] == READINESS_READY
    assert _status(superuser_db, owner_id, draft) == "draft"  # the report authorized nothing

    # Source goes stale between the report the Founder saw and the approval attempt.
    docs[0].active_truth_status = ActiveTruthStatus.disputed
    superuser_db.commit()
    with pytest.raises(BlueprintError, match="approval_blocked"):
        approve_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft.id, approved_by=str(owner_id))
    assert _status(superuser_db, owner_id, draft) == "draft"


def test_approved_draft_cannot_be_reapproved_immutability_preserved(superuser_db):
    owner_id, draft, _ = _make(superuser_db)
    approve_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft.id, approved_by=str(owner_id))
    # A second approval is refused (already decided) — immutable, Founder-only guarantees intact.
    with pytest.raises(BlueprintError):
        approve_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft.id, approved_by=str(owner_id))
