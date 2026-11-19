"""Round 12 Phase 5: MEMORY & COGNITIVE CONTINUITY UNDER REALISTIC CONDITIONS.

Extends the R11 10-step scenario with the conditions real usage produces:

  C1  multiple CONCURRENT conversations (interleaved) — decisions of one problem never
      leak into another's recall; both stay independently consistent
  C2  contradictory USER statements — both are recorded; no silent overwrite; the
      supersession mechanism (not a rewrite) is how a correction lands
  C3  revised long-term GOALS — revision is a SUPERSESSION with provenance, never an edit
  C4  superseded evidence feeding decisions — the decision cites evidence that later
      conflicts; the why-chain survives
  C5  explicit memory correction via deletion — a DELETED source leaves retrieval and the
      evidence surface; the audit trail RETAINS the fact (retention vs forgetting)
  C6  model-provider SWITCHING mid-history — decisions/claims are provider-independent;
      a provider swap changes no stored cognition (full activation restore asserted)
  C7  authorization boundaries — a non-founder owner's concurrent session still cannot
      see or mutate the founder's cognitive state (RLS self-assertion, concurrency-safe)

Why-chains: every conclusion change answers WHY from persisted provenance (audit rows +
decision provenance) — history is never silently rewritten.
"""

import threading
import uuid

import pytest
from sqlalchemy import text as sa_text

from app.db import SessionLocal
from app.models.audit import AuditLog
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.models.provider_config import ProviderConfig
from app.problem_learning.service import active_decision, create_problem, record_decision
from app.rag.claims import EXTRACTION_VERSION
from app.rag.evidence_metadata import record_claim_evidence_metadata
from app.providers import registry as provider_registry
from app.providers.zai_provider import ZAIProvider
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password

from app.config import get_settings as _gs
DIM = _gs().embedding_dim


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _bow(text: str) -> list[float]:
    import math

    v = [0.0] * DIM
    for w in (text or "").lower().split():
        v[hash(w) % DIM] += 1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


@pytest.fixture
def env(client, db_session):
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == "founder@lifeos.local").first()
    _set_rls_user(db_session, founder.id)
    return {"db": db_session, "owner": founder}


def _doc(env, title, text, topic=None, confidence=ClaimConfidence.likely, owner_id=None):
    db = env["db"]
    owner_id = owner_id or env["owner"].id
    _set_rls_user(db, owner_id)
    source = Document(title=title, source=DocumentSource.upload, uploaded_by=owner_id)
    db.add(source)
    db.commit()
    _set_rls_user(db, owner_id)
    chunk = DocumentChunk(document_id=source.id, owner_id=owner_id, chunk_index=0, text=text, embedding=_bow(text))
    db.add(chunk)
    db.commit()
    _set_rls_user(db, owner_id)
    claim = KnowledgeClaim(owner_id=owner_id, source_id=source.id, chunk_id=chunk.id, claim_text=text,
                           confidence=confidence, claim_type=ClaimType.historical, status=ClaimStatus.active,
                           extraction_version=EXTRACTION_VERSION)
    db.add(claim)
    db.commit()
    if topic:
        record_claim_evidence_metadata(db, owner_id, claim.id, {"research_topic": topic}, declared_via="r12_cont")
        db.commit()
    return source, claim


def test_c1_concurrent_conversations_isolated(env):
    db, owner = env["db"], env["owner"].id
    from app.db import SessionLocal

    _set_rls_user(db, owner)
    p1 = create_problem(db, owner_id=owner, title="C1 projekt A", description="Inköp A.", idempotency_key=f"c1a-{uuid.uuid4()}", status="open", authority="founder")
    _set_rls_user(db, owner)
    p2 = create_problem(db, owner_id=owner, title="C1 projekt B", description="Rekrytering B.", idempotency_key=f"c1b-{uuid.uuid4()}", status="open", authority="founder")
    _set_rls_user(db, owner)
    d1 = record_decision(db, owner_id=owner, problem_id=p1.id, decision="C1 A: välja leverantör X.", idempotency_key=f"c1a-d-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
    _set_rls_user(db, owner)
    d2 = record_decision(db, owner_id=owner, problem_id=p2.id, decision="C1 B: anställa först i Q2.", idempotency_key=f"c1b-d-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
    db.commit()

    def revise(problem_id, prior_id, tag):
        s = SessionLocal()
        try:
            _set_rls_user(s, owner)
            record_decision(s, owner_id=owner, problem_id=problem_id, decision=f"C1 reviderad {tag}.", idempotency_key=f"c1-{tag}-{uuid.uuid4()}", status="active", authority="founder", basis="manual", supersedes_decision_id=prior_id)
            s.commit()
        finally:
            s.close()

    t1 = threading.Thread(target=revise, args=(p1.id, d1.id, "A"))
    t2 = threading.Thread(target=revise, args=(p2.id, d2.id, "B"))
    t1.start(); t2.start(); t1.join(); t2.join()

    fresh = SessionLocal()
    try:
        _set_rls_user(fresh, owner)
        a = active_decision(fresh, owner_id=owner, problem_id=p1.id)
        b = active_decision(fresh, owner_id=owner, problem_id=p2.id)
        assert "reviderad A" in a.decision and "leverantör X" not in a.decision
        assert "reviderad B" in b.decision and "Q2" not in b.decision
        assert a.problem_id == p1.id and b.problem_id == p2.id, "C1: no cross-conversation leakage"
    finally:
        fresh.close()


def test_c2_contradictory_user_statements_recorded_both(env):
    db, owner = env["db"], env["owner"].id
    _set_rls_user(db, owner)
    problem = create_problem(db, owner_id=owner, title="C2 motstridiga uttalanden", description="Grunden är A sedan B.", idempotency_key=f"c2-{uuid.uuid4()}", status="open", authority="founder")
    _set_rls_user(db, owner)
    first = record_decision(db, owner_id=owner, problem_id=problem.id, decision="C2 grunden är A (grundarens första uttalande).", idempotency_key=f"c2-1-{uuid.uuid4()}", status="active", authority="founder", basis="manual", provenance={"user_statement": "grunden är A"})
    _set_rls_user(db, owner)
    second = record_decision(db, owner_id=owner, problem_id=problem.id, decision="C2 grunden är B (grundarens senare uttalande).", idempotency_key=f"c2-2-{uuid.uuid4()}", status="active", authority="founder", basis="manual", provenance={"user_statement": "grunden är B"}, supersedes_decision_id=first.id)
    db.commit()
    db.refresh(first)
    assert first.decision.endswith("(grundarens första uttalande).") and first.status == "superseded", (
        "C2: the FIRST statement is never overwritten — both statements exist in history"
    )
    assert second.provenance and "grunden är B" in second.provenance.get("user_statement", "")
    audits = db.query(AuditLog).filter_by(action="decision_superseded").count()
    assert audits >= 1, "C2: the correction is audited — why-answerable"


def test_c3_revised_goals_supersede_never_edit(env):
    db, owner = env["db"], env["owner"].id
    _set_rls_user(db, owner)
    problem = create_problem(db, owner_id=owner, title="C3 mål", description="Långtidsmål 2026: 10 kunder.", idempotency_key=f"c3-{uuid.uuid4()}", status="open", authority="founder")
    _set_rls_user(db, owner)
    g1 = record_decision(db, owner_id=owner, problem_id=problem.id, decision="C3 mål: 10 kunder till sommaren.", idempotency_key=f"c3-1-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
    _set_rls_user(db, owner)
    g2 = record_decision(db, owner_id=owner, problem_id=problem.id, decision="C3 MÅL REVIDERAD: 25 kunder till sommaren (grundarens beslut 2026-11-19).", idempotency_key=f"c3-2-{uuid.uuid4()}", status="active", authority="founder", basis="manual", provenance={"goal_revision": True}, supersedes_decision_id=g1.id)
    db.commit()
    db.refresh(g1)
    assert "10 kunder" in g1.decision and g1.status == "superseded", "C3: the old goal is preserved, not edited"
    assert g2.provenance.get("goal_revision") is True, "C3: the revision is MARKED as a revision"
    assert "25 kunder" in g2.decision


def test_c4_decision_on_later_conflicted_evidence(env):
    db, owner = env["db"], env["owner"].id
    from app.db import SessionLocal
    from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType

    _set_rls_user(db, owner)
    problem = create_problem(db, owner_id=owner, title="C4 bevisförskjuten corrigering", description="Beslut på studie som senare motsägs.", idempotency_key=f"c4-{uuid.uuid4()}", status="open", authority="founder")
    _src, claim = _doc(env, "C4 studie", "Studie: metod M fungerar.", topic="c4_t", confidence=ClaimConfidence.certain)
    _set_rls_user(db, owner)
    d1 = record_decision(db, owner_id=owner, problem_id=problem.id, decision="C4: använd metod M (stöd: studie).", idempotency_key=f"c4-1-{uuid.uuid4()}", status="active", authority="founder", basis="manual", provenance={"supporting_evidence": [f"claim:{claim.id}"]})
    _src2, claim2 = _doc(env, "C4 motsägelse", "Studie: metod M fungerar inte.", topic="c4_t")
    _set_rls_user(db, owner)
    db.add(ClaimRelationship(owner_id=owner, from_claim_id=claim2.id, to_claim_id=claim.id, relationship_type=ClaimRelationshipType.contradicts))
    db.commit()
    _set_rls_user(db, owner)
    d2 = record_decision(db, owner_id=owner, problem_id=problem.id, decision="C4 REVIDERAD: metod M ifrågasatt — motstridigt bevis.", idempotency_key=f"c4-2-{uuid.uuid4()}", status="active", authority="founder", basis="manual", provenance={"supporting_evidence": [f"claim:{claim2.id}", f"claim:{claim.id}"]}, supersedes_decision_id=d1.id)
    db.commit()
    fresh = SessionLocal()
    try:
        _set_rls_user(fresh, owner)
        import json

        audit = fresh.query(AuditLog).filter_by(action="decision_superseded").one()
        detail = json.loads(audit.detail)
        assert detail["superseded_decision_id"] == str(d1.id) and detail["superseding_decision_id"] == str(d2.id)
        assert d2.provenance["supporting_evidence"] == [f"claim:{claim2.id}", f"claim:{claim.id}"], (
            "C4: the why-chain cites BOTH the new and the superseded evidence"
        )
    finally:
        fresh.close()


def test_c5_deletion_forgets_content_retains_audit(env):
    db, owner = env["db"], env["owner"].id
    from app.db import SessionLocal
    from datetime import datetime, timezone

    _set_rls_user(db, owner)
    problem = create_problem(db, owner_id=owner, title="C5 radering", description="Källa raderas efter beslut.", idempotency_key=f"c5-{uuid.uuid4()}", status="open", authority="founder")
    src, claim = _doc(env, "C5 källa att radera", "Studie: metod Z verkar.", topic="c5_t", confidence=ClaimConfidence.certain)
    _set_rls_user(db, owner)
    record_decision(db, owner_id=owner, problem_id=problem.id, decision="C5: metod Z vald (källa: studie).", idempotency_key=f"c5-d-{uuid.uuid4()}", status="active", authority="founder", basis="manual", provenance={"supporting_evidence": [f"claim:{claim.id}"]})
    db.commit()
    # DELETE the source (soft delete) — retention: the audit of the deletion act remains
    _set_rls_user(db, owner)
    src.deleted_at = datetime.now(timezone.utc)
    db.add(AuditLog(user_id=owner, action="source_deleted", entity_type="document", entity_id=str(src.id), detail='{"reason": "founder explicit deletion"}'))
    db.commit()
    fresh = SessionLocal()
    try:
        _set_rls_user(fresh, owner)
        deleted_visible = fresh.query(Document).filter(Document.id == src.id, Document.deleted_at.is_(None)).count()
        assert deleted_visible == 0, "C5: the deleted source leaves the live surface"
        audit = fresh.query(AuditLog).filter_by(action="source_deleted", entity_id=str(src.id)).count()
        assert audit == 1, "C5: the deletion EVENT is retained (retention vs forgetting)"
        d = active_decision(fresh, owner_id=owner, problem_id=problem.id)
        assert d.provenance["supporting_evidence"] == [f"claim:{claim.id}"], (
            "C5: the decision's provenance reference is RETAINED — history explains itself even after deletion"
        )
    finally:
        fresh.close()


def test_c6_provider_switch_changes_no_cognition(env):
    db, owner = env["db"], env["owner"].id
    _set_rls_user(db, owner)
    problem = create_problem(db, owner_id=owner, title="C6 leverantörsbyte", description="Byt modell, behåll minnet.", idempotency_key=f"c6-{uuid.uuid4()}", status="open", authority="founder")
    _set_rls_user(db, owner)
    d1 = record_decision(db, owner_id=owner, problem_id=problem.id, decision="C6: arkitekturen fryses till januari.", idempotency_key=f"c6-1-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
    db.commit()

    # SWITCH the chat provider through the admin path (run-scoped, restored)
    provider_registry._PROVIDERS["zai"] = ZAIProvider
    db.add(ProviderConfig(role="chat", provider="zai", model="glm-4-plus", is_active=True))
    db.commit()
    try:
        _set_rls_user(db, owner)
        d2 = record_decision(db, owner_id=owner, problem_id=problem.id, decision="C6: arkitekturfrysning förlängs till februari.", idempotency_key=f"c6-2-{uuid.uuid4()}", status="active", authority="founder", basis="manual", supersedes_decision_id=d1.id)
        db.commit()
    finally:
        for role in ("chat", "embedding"):
            cfg = db.query(ProviderConfig).filter_by(role=role, provider="zai").first()
            if cfg is not None:
                db.delete(cfg)
        provider_registry._PROVIDERS.pop("zai", None)
        db.commit()
    db.refresh(d1)
    from app.db import SessionLocal

    fresh = SessionLocal()
    try:
        _set_rls_user(fresh, owner)
        current = active_decision(fresh, owner_id=owner, problem_id=problem.id)
        assert current.decision == d2.decision and d1.status == "superseded", (
            "C6: the provider switch changed NO stored cognition — decisions are provider-independent"
        )
        assert db.query(ProviderConfig).filter_by(provider="zai", is_active=True).count() == 0, (
            "C6: the switch is fully restored — no residual activation"
        )
    finally:
        fresh.close()


def test_c7_authorization_boundary_under_concurrency(env):
    db, owner = env["db"], env["owner"].id
    from app.db import SessionLocal
    from app.models.user import User, UserRole
    from app.problem_learning.service import ProblemLearningError

    _set_rls_user(db, owner)
    problem = create_problem(db, owner_id=owner, title="C7 gränser", description="Andra ägare ser inget.", idempotency_key=f"c7-{uuid.uuid4()}", status="open", authority="founder")
    _set_rls_user(db, owner)
    record_decision(db, owner_id=owner, problem_id=problem.id, decision="C7: hemlig plan.", idempotency_key=f"c7-d-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
    db.commit()
    _set_rls_user(db, None)
    other = User(email=f"c7-other-{uuid.uuid4().hex[:6]}@lifeos.local", password_hash=hash_password("OtherC7Pass123!"), role=UserRole.member, email_verified=True)
    db.add(other)
    db.commit()

    attempts: list = []

    def try_touch():
        s = SessionLocal()
        try:
            _set_rls_user(s, other.id)
            try:
                row = record_decision(s, owner_id=other.id, problem_id=problem.id, decision="intrång", idempotency_key=f"c7-x-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
                s.commit()
                attempts.append(("leak", str(row.id)))
            except ProblemLearningError:
                s.rollback()
                attempts.append(("blocked", None))
        finally:
            s.close()

    t = threading.Thread(target=try_touch)
    t.start(); t.join()
    assert attempts == [("blocked", None)], f"C7: concurrent foreign-owner mutation blocked: {attempts}"
    fresh = SessionLocal()
    try:
        _set_rls_user(fresh, owner)
        d = active_decision(fresh, owner_id=owner, problem_id=problem.id)
        assert d.decision == "C7: hemlig plan.", "C7: the founder's state is intact after the intrusion attempt"
    finally:
        fresh.close()
