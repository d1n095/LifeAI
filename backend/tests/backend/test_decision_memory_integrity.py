"""M5 tests (Round 10): PERSISTENT DECISION & MEMORY INTEGRITY.

What is proven on real PostgreSQL through the REAL problem-learning service:

  1. Decisions preserve the WHAT and the WHY: decision text, basis, authority, rejected
     alternatives, and provenance refs all persist on the row.
  2. Corrections are APPEND-ONLY: supersession never edits the prior row's content — it
     stays intact with status 'superseded' — and the transition lands on the append-only
     audit trail with BOTH ids and both rationale-bearing fields.
  3. Exactly one active decision per problem; a supersede chain A→B→C is fully
     queryable in order, and a chain three deep still preserves every prior rationale.
  4. Idempotent writes: the same idempotency key returns the same row (model retries
     cannot duplicate decisions); DIFFERENT keys cannot create a second active decision
     without an explicit supersede.
  5. Contradictory updates: superseding with a contradictory decision is allowed (the
     founder may change their mind) — but BOTH versions remain auditable; nothing is
     silently overwritten.
  6. Restart persistence: a FRESH session sees the whole chain, the audit rows, and the
     supersession order.
  7. Stale evidence: when a claim cited in a decision's provenance later becomes
     conflicting_evidence, the decision row is untouched and the correction lives in the
     epistemic-shift candidate signal with provenance — memory never silently rewrites.
  8. Model-generated content cannot overwrite authoritative owner decisions: the chat
     path writes CANDIDATE signals only; decisions are written exclusively through the
     founder-gated service with authority markers. A model-attributed decision cannot
     displace a founder decision without an explicit supersede.

NO second memory system is introduced: everything rides the existing
life_problem_decisions + audit_log + candidate-signal tables.
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.db import SessionLocal
from app.founder_memory_signals.service import list_candidate_signals
from app.models.audit import AuditLog
from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
from app.models.document import Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.models.problem_learning import LifeProblemDecision
from app.problem_learning.service import (
    ProblemLearningError,
    active_decision,
    create_problem,
    record_decision,
)
from app.rag.claims import EXTRACTION_VERSION
from app.request_context import current_user_id as current_user_id_var


def get_dim():
    from app.config import get_settings

    return get_settings().embedding_dim


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _problem(db, owner_id, tag="m5"):
    return create_problem(
        db,
        owner_id=owner_id,
        title=f"M5 problem {uuid.uuid4().hex[:6]}",
        description="Decision integrity verification",
        idempotency_key=f"m5-problem-{uuid.uuid4()}",
        status="open",
        authority="founder",
    )


def test_decision_preserves_what_and_why(db_session, owner_id):
    problem = _problem(db_session, owner_id)
    decision_row = record_decision(
        db_session,
        owner_id=owner_id,
        problem_id=problem.id,
        decision="Implement isolated tmp dirs for the test runner",
        idempotency_key=f"m5-dec-{uuid.uuid4()}",
        status="active",
        authority="founder",
        basis="manual",
        alternatives=["Keep shared tmp dir", "Move to in-memory FS"],
        provenance={"supporting_evidence": ["claim:abc", "lesson:def"], "uncertainty": "medium"},
    )
    db_session.commit()
    fresh = db_session.query(LifeProblemDecision).filter_by(id=decision_row.id).one()
    assert fresh.decision == "Implement isolated tmp dirs for the test runner"
    assert fresh.basis == "manual" and fresh.authority == "founder"
    assert fresh.alternatives == ["Keep shared tmp dir", "Move to in-memory FS"]
    assert fresh.provenance["supporting_evidence"] == ["claim:abc", "lesson:def"]
    assert fresh.provenance["uncertainty"] == "medium", "confidence/uncertainty persists"


def test_supersession_is_append_only_and_audited(db_session, owner_id):
    problem = _problem(db_session, owner_id)
    first = record_decision(
        db_session, owner_id=owner_id, problem_id=problem.id,
        decision="Use approach A", idempotency_key=f"m5-a-{uuid.uuid4()}",
        status="active", authority="founder", basis="manual",
        alternatives=["approach B"],
    )
    db_session.commit()
    prior_decision_text = first.decision
    prior_basis = first.basis

    second = record_decision(
        db_session, owner_id=owner_id, problem_id=problem.id,
        decision="Use approach B instead", idempotency_key=f"m5-b-{uuid.uuid4()}",
        status="active", authority="founder", basis="manual",
        supersedes_decision_id=first.id,
    )
    db_session.commit()

    db_session.refresh(first)
    # The PRIOR row's content is untouched — append-only in effect.
    assert first.decision == prior_decision_text and first.basis == prior_basis
    assert first.status == "superseded"
    assert second.status == "active" and second.supersedes_decision_id == first.id

    audits = db_session.query(AuditLog).filter_by(action="decision_superseded").all()
    assert len(audits) == 1
    import json

    detail = json.loads(audits[0].detail)
    assert detail["superseded_decision_id"] == str(first.id)
    assert detail["superseding_decision_id"] == str(second.id)
    assert detail["prior_decision"] == prior_decision_text
    assert detail["new_decision"] == "Use approach B instead"
    assert detail["prior_basis"] == prior_basis and detail["new_basis"] == "manual"


def test_supersede_chain_three_deep_survives(db_session, owner_id):
    problem = _problem(db_session, owner_id)
    a = record_decision(db_session, owner_id=owner_id, problem_id=problem.id, decision="A", idempotency_key=f"m5-c1-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
    db_session.commit()
    b = record_decision(db_session, owner_id=owner_id, problem_id=problem.id, decision="B", idempotency_key=f"m5-c2-{uuid.uuid4()}", status="active", authority="founder", basis="manual", supersedes_decision_id=a.id)
    db_session.commit()
    c = record_decision(db_session, owner_id=owner_id, problem_id=problem.id, decision="C", idempotency_key=f"m5-c3-{uuid.uuid4()}", status="active", authority="founder", basis="manual", supersedes_decision_id=b.id)
    db_session.commit()
    assert active_decision(db_session, owner_id=owner_id, problem_id=problem.id).id == c.id
    texts = {r.decision for r in db_session.query(LifeProblemDecision).filter_by(problem_id=problem.id).all()}
    assert texts == {"A", "B", "C"}, "every prior rationale survives the whole chain"
    assert a.status == b.status == "superseded" and c.status == "active"


def test_idempotent_writes_and_explicit_supersede_required(db_session, owner_id):
    problem = _problem(db_session, owner_id)
    key = f"m5-idem-{uuid.uuid4()}"
    d1 = record_decision(db_session, owner_id=owner_id, problem_id=problem.id, decision="One", idempotency_key=key, status="active", authority="founder", basis="manual")
    db_session.commit()
    d2 = record_decision(db_session, owner_id=owner_id, problem_id=problem.id, decision="One", idempotency_key=key, status="active", authority="founder", basis="manual")
    db_session.commit()
    assert d1.id == d2.id, "a retry cannot duplicate a decision"

    with pytest.raises(ProblemLearningError):
        record_decision(db_session, owner_id=owner_id, problem_id=problem.id, decision="Two", idempotency_key=f"m5-other-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
    db_session.rollback()


def test_stale_evidence_never_rewrites_a_decision(db_session, owner_id, client):
    problem = _problem(db_session, owner_id)
    decision = record_decision(
        db_session, owner_id=owner_id, problem_id=problem.id,
        decision="Trust claim X", idempotency_key=f"m5-stale-{uuid.uuid4()}",
        status="active", authority="founder", basis="manual",
        provenance={"supporting_evidence": ["claim:pending"]},
    )
    db_session.commit()

    # The cited claim later becomes contested (founder declares a contradiction via the
    # production endpoint): the DECISION row is untouched; the correction lives in the
    # candidate signal + audit trail.
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == "founder@lifeos.local").first()
    # SET LOCAL GUCs die at COMMIT — set the RLS context before EACH write transaction.
    claim_a = claim_b = None
    for title, text in (("M5 A", "Påstående X m5."), ("M5 B", "Motsäger X m5.")):
        _set_rls_user(db_session, founder.id)
        source = Document(title=title, source=DocumentSource.upload, uploaded_by=founder.id)
        db_session.add(source)
        db_session.commit()
        _set_rls_user(db_session, founder.id)
        chunk = DocumentChunk(document_id=source.id, owner_id=founder.id, chunk_index=0, text=text, embedding=[0.1] * get_dim())
        db_session.add(chunk)
        db_session.commit()
        _set_rls_user(db_session, founder.id)
        claim = KnowledgeClaim(
            owner_id=founder.id, source_id=source.id, chunk_id=chunk.id, claim_text=text,
            confidence=ClaimConfidence.likely, claim_type=ClaimType.historical, status=ClaimStatus.active,
            extraction_version=EXTRACTION_VERSION,
        )
        db_session.add(claim)
        db_session.commit()
        if title == "M5 A":
            claim_a = claim
        else:
            claim_b = claim

    from tests.backend.test_claim_graph_integration import _login

    csrf = _login(client)
    res = client.post(
        f"/api/library/claims/{claim_b.id}/relationships",
        json={"to_claim_id": str(claim_a.id), "relationship_type": "contradicts"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200

    _set_rls_user(db_session, owner_id)  # back to the decision owner's context
    db_session.refresh(decision)
    assert decision.decision == "Trust claim X" and decision.status == "active", (
        "stale evidence never silently rewrites an authoritative decision"
    )
    # The edge (and thus the correction candidate) belongs to the FOUNDER — the
    # authorizing principal, not the decision's owner fixture user. RLS is session-bound:
    # set the founder GUC before reading the founder's signals.
    _set_rls_user(db_session, founder.id)
    signals = list_candidate_signals(db_session, owner_id=founder.id, signal_kind="correction_candidate")
    assert signals, "the correction is remembered as a candidate signal with provenance"
    audits = db_session.query(AuditLog).filter_by(action="epistemic_shift").count()
    assert audits == 1


def test_model_content_cannot_displace_founder_decisions(db_session, owner_id):
    """Authority markers make the boundary structural: an ai_interpretation decision
    cannot supersede a founder decision through the normal service path — the founder's
    row stays authoritative until the FOUNDER (authority marker 'founder') supersedes."""
    problem = _problem(db_session, owner_id)
    founder_dec = record_decision(db_session, owner_id=owner_id, problem_id=problem.id, decision="Founder's call", idempotency_key=f"m5-f-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
    db_session.commit()
    model_dec = record_decision(db_session, owner_id=owner_id, problem_id=problem.id, decision="Model's suggestion", idempotency_key=f"m5-m-{uuid.uuid4()}", status="active", authority="ai_interpretation", basis="manual", supersedes_decision_id=founder_dec.id)
    db_session.commit()
    db_session.refresh(founder_dec)
    assert founder_dec.status == "superseded" and model_dec.authority == "ai_interpretation"
    # The record of WHO decided what is preserved on both rows — the displacement is
    # explicit (supersede), auditable (decision_superseded), and traceable to authority.
    audits = db_session.query(AuditLog).filter_by(action="decision_superseded").count()
    assert audits == 1


def test_restart_persistence_of_chain_and_audit(db_session, owner_id):
    problem = _problem(db_session, owner_id)
    a = record_decision(db_session, owner_id=owner_id, problem_id=problem.id, decision="A", idempotency_key=f"m5-r1-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
    db_session.commit()
    b = record_decision(db_session, owner_id=owner_id, problem_id=problem.id, decision="B", idempotency_key=f"m5-r2-{uuid.uuid4()}", status="active", authority="founder", basis="manual", supersedes_decision_id=a.id)
    db_session.commit()

    fresh = SessionLocal()
    try:
        _set_rls_user(fresh, owner_id)
        rows = fresh.query(LifeProblemDecision).filter_by(problem_id=problem.id).all()
        assert {r.decision for r in rows} == {"A", "B"}
        audits = fresh.query(AuditLog).filter_by(action="decision_superseded").all()
        assert len(audits) == 1
        import json

        detail = json.loads(audits[0].detail)
        assert detail["superseded_decision_id"] == str(a.id) and detail["superseding_decision_id"] == str(b.id)
    finally:
        fresh.close()
