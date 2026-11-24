"""Phase 3: CROSS-SESSION COGNITIVE CONTINUITY — the 10-step scenario on real
PostgreSQL through production call paths, spanning SESSION RESTARTS (fresh DB
connections = the restart boundary for state), owner isolation, provenance, and
concurrent updates.

Scenario (the mission's exact 10 steps, in order):
  1.  receives project information      — documents ingested for a project
  2.  forms a provisional conclusion    — claims land with confidence + metadata
  3.  records supporting evidence       — claim evidence metadata stamped + provenance chain
  4.  receives contradictory information— a second source's claim declared contradicts
  5.  re-evaluates the conclusion       — the verdict MOVES (epistemic shift recorded)
  6.  preserves the original reasoning  — prior decision rows + audit trail untouched
  7.  records the correction            — correction_candidate signal with provenance + audit
  8.  recalls the current conclusion in a new session — FRESH connection reads current verdict + rationale
  9.  explains why it changed           — the shift provenance (prior -> current, both ids) answers "why"
  10. avoids reviving superseded claims — a superseded decision stays superseded; the
      contradicted claim never regains verified status without new evidence

Plus: owner isolation (another owner sees none of it), authorization (founder-only
mutations), provenance (claim->chunk->source->stamp intact after restarts), and
concurrent updates (two writers racing on decision supersession stay consistent).

NO second memory system: everything rides life_problem_decisions, knowledge_claims,
claim_relationships, claim_evidence_metadata, candidate signals, and audit_log.
"""

import threading
import uuid

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
from app.problem_learning.service import active_decision, create_problem, record_decision
from app.rag.claim_graph import trace_evidence_chain
from app.rag.claims import EXTRACTION_VERSION
from app.rag.evidence_metadata import record_claim_evidence_metadata
from app.request_context import current_user_id as current_user_id_var
from tests.backend.test_claim_graph_integration import FOUNDER_PASSWORD

from app.config import get_settings as _gs
DIM = _gs().embedding_dim
FOUNDER_EMAIL = "founder@lifeos.local"


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def get_dim():
    return DIM


def _bow(text: str) -> list[float]:
    v = [0.0] * DIM
    for w in (text or "").lower().split():
        v[hash(w) % DIM] += 1.0
    import math

    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


@pytest.fixture
def env(client, db_session):
    # THE founder account: the mutation endpoints are restricted to the founder ACCOUNT
    # (not merely founder-role) — the scenario must exercise the real production paths.
    from app.models.user import User

    user = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    csrf = res.json()["csrf_token"]
    _set_rls_user(db_session, user.id)
    return {"client": client, "db": db_session, "owner": user, "csrf": csrf}


def _claim_for(env, title, text, *, topic=None, meta=None, confidence=ClaimConfidence.likely, owner_id=None):
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
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=source.id, chunk_id=chunk.id, claim_text=text,
        confidence=confidence, claim_type=ClaimType.historical, status=ClaimStatus.active,
        extraction_version=EXTRACTION_VERSION,
    )
    db.add(claim)
    db.commit()
    if topic or meta:
        record_claim_evidence_metadata(db, owner_id, claim.id, {"research_topic": topic, **(meta or {})}, declared_via="r11_continuity")
        db.commit()
    return source, claim


def test_ten_step_cross_session_continuity(env):
    db, owner_id, client, csrf = env["db"], env["owner"].id, env["client"], env["csrf"]
    TOPIC = "cont_balder"

    # 1. project information arrives
    _set_rls_user(db, owner_id)
    problem = create_problem(
        db, owner_id=owner_id, title="Projekt Balder sömnspårning", description="Balder: kaffepåverkan på sömn.",
        idempotency_key=f"cont-p-{uuid.uuid4()}", status="open", authority="founder",
    )
    src_rct, claim_rct = _claim_for(
        env, "Balder RCT", "Balder-studien: kaffe försämrar sömnkvaliteten.", topic=TOPIC,
        meta={"study_design": "randomized_trial", "sample_size": 350, "study_setting": "Sverige",
              "competing_explanations": ["controlled"], "causal_claim": True, "causal_claim_supported_by_design": True},
        confidence=ClaimConfidence.certain,
    )
    # 2. provisional conclusion: the founder records a DECISION on the current evidence
    _set_rls_user(db, owner_id)
    decision_a = record_decision(
        db, owner_id=owner_id, problem_id=problem.id,
        decision="Balder: kaffe försämrar sömn enligt RCT — utgå från försämring.",
        idempotency_key=f"cont-d1-{uuid.uuid4()}", status="active", authority="founder", basis="manual",
        provenance={"supporting_evidence": [f"claim:{claim_rct.id}"], "uncertainty": "low"},
    )
    db.commit()

    # 3. supporting evidence recorded (provenance chain verified)
    chain_a = trace_evidence_chain(db, owner_id, claim_rct.id)
    assert chain_a.chunk_grounded and chain_a.verdict.status.value == "verified_fact"
    from app.rag.evidence_metadata import quality_metadata_for_claims

    assert claim_rct.id in quality_metadata_for_claims(db, owner_id, [claim_rct.id])

    # 4. contradictory information arrives (production endpoint, declared symmetric edge)
    _src2, claim_obs = _claim_for(
        env, "Balder kohort", "Balder kohortstudie: kaffe utan tydlig sömnpåverkan.", topic=TOPIC,
        meta={"study_design": "prospective_cohort", "sample_size": 1100, "study_setting": "Japan",
              "competing_explanations": ["controlled"]},
    )
    res = client.post(
        f"/api/library/claims/{claim_obs.id}/relationships",
        json={"to_claim_id": str(claim_rct.id), "relationship_type": "contradicts"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200
    shifts = res.json()["epistemic_shifts"]
    assert any(sh["changed"] for sh in shifts)

    # 5. the conclusion is RE-EVALUATED: verdict moved + the founder supersedes the decision
    chain_b = trace_evidence_chain(db, owner_id, claim_rct.id)
    assert chain_b.verdict.status.value == "conflicting_evidence", "5: re-evaluation happened"
    _set_rls_user(db, owner_id)
    decision_b = record_decision(
        db, owner_id=owner_id, problem_id=problem.id,
        decision="Balder REVIDERAD: bevisläget är motsagt — kaffeeffekten är oklar, fortsätt mäta.",
        idempotency_key=f"cont-d2-{uuid.uuid4()}", status="active", authority="founder", basis="manual",
        provenance={"supporting_evidence": [f"claim:{claim_obs.id}", f"claim:{claim_rct.id}"], "uncertainty": "high"},
        supersedes_decision_id=decision_a.id,
    )
    db.commit()

    # 6. the ORIGINAL reasoning survives untouched (row content + audit)
    db.refresh(decision_a)
    assert decision_a.status == "superseded"
    assert decision_a.decision.startswith("Balder: kaffe försämrar sömn enligt RCT"), "6: original reasoning preserved verbatim"
    audits = db.query(AuditLog).filter_by(action="decision_superseded").all()
    assert len(audits) == 1

    # 7. the correction is recorded (candidate signal with provenance)
    signals = list_candidate_signals(db, owner_id=owner_id, signal_kind="correction_candidate")
    assert signals, "7: correction recorded"
    assert any(sh["prior"] == "verified_fact" and sh["current"] == "conflicting_evidence" for sh in signals[-1].provenance["shifts"])

    # --- SESSION RESTART: everything below reads through FRESH connections -------------
    fresh = SessionLocal()
    try:
        _set_rls_user(fresh, owner_id)

        # 8. the CURRENT conclusion is recalled in the new session
        current = active_decision(fresh, owner_id=owner_id, problem_id=problem.id)
        assert current.id == decision_b.id and "REVIDERAD" in current.decision, "8: current conclusion recalled"

        # 9. WHY it changed is answerable from persisted provenance
        import json

        detail = json.loads(audits[0].detail)
        assert detail["prior_decision"].startswith("Balder: kaffe försämrar") and "REVIDERAD" in detail["new_decision"]
        assert detail["superseded_decision_id"] == str(decision_a.id) and detail["superseding_decision_id"] == str(decision_b.id)
        fresh_chain = trace_evidence_chain(fresh, owner_id, claim_rct.id)
        assert fresh_chain.verdict.status.value == "conflicting_evidence" and fresh_chain.contradicting_edges, (
            "9: the contradiction (the WHY) is visible in the new session"
        )

        # 10. the superseded conclusion is NOT revived: the decision stays superseded and the
        #     claim never regains verified status without NEW evidence.
        rows = fresh.query(LifeProblemDecision).filter_by(problem_id=problem.id).all()
        assert [r.status for r in rows].count("active") == 1
        assert rows[0].id != decision_a.id or decision_a.id == decision_b.id  # sanity
        revived = trace_evidence_chain(fresh, owner_id, claim_rct.id)
        assert revived.verdict.status.value != "verified_fact", "10: no revival without new evidence"

        # the contradicted claim CAN regain status only through the declared mechanism
        # (remove the contradiction edge) — asserting the edge still blocks it:
        edges = fresh.query(ClaimRelationship).filter_by(owner_id=owner_id).all()
        assert any(e.relationship_type == ClaimRelationshipType.contradicts for e in edges)
    finally:
        fresh.close()

    # Owner isolation across the restart: another owner's fresh session sees NOTHING.
    from app.models.user import User, UserRole
    from app.security import hash_password

    _set_rls_user(db, owner_id)
    other = User(email=f"cont-other-{uuid.uuid4().hex[:8]}@lifeos.local", password_hash=hash_password("OtherContPass123!"), role=UserRole.founder, email_verified=True)
    db.add(other)
    db.commit()
    fresh2 = SessionLocal()
    try:
        _set_rls_user(fresh2, other.id)
        # The service RAISES for another owner's problem — the problem itself is
        # invisible (RLS), which is the isolation guarantee asserting itself.
        from app.problem_learning.service import ProblemLearningError

        with pytest.raises(ProblemLearningError):
            active_decision(fresh2, owner_id=other.id, problem_id=problem.id)
        assert fresh2.query(LifeProblemDecision).filter_by(problem_id=problem.id).count() == 0, "isolation: no rows visible"
        with pytest.raises(LookupError):
            trace_evidence_chain(fresh2, other.id, claim_rct.id)  # the claim itself is invisible
    finally:
        fresh2.close()


def test_concurrent_decision_updates_stay_consistent(env):
    """Two writers racing to supersede the SAME active decision: exactly one wins, the
    loser fails loudly (never silently), and the chain stays consistent."""
    db, owner_id = env["db"], env["owner"].id
    _set_rls_user(db, owner_id)
    problem = create_problem(
        db, owner_id=owner_id, title="Concurrent Balder", description="Racing supersessions.",
        idempotency_key=f"conc-p-{uuid.uuid4()}", status="open", authority="founder",
    )
    _set_rls_user(db, owner_id)
    base = record_decision(db, owner_id=owner_id, problem_id=problem.id, decision="Bas", idempotency_key=f"conc-0-{uuid.uuid4()}", status="active", authority="founder", basis="manual")
    db.commit()

    results: list = []

    def writer(tag: str):
        s = SessionLocal()
        try:
            _set_rls_user(s, owner_id)
            row = record_decision(
                s, owner_id=owner_id, problem_id=problem.id,
                decision=f"Uppdatering {tag}", idempotency_key=f"conc-{tag}-{uuid.uuid4()}",
                status="active", authority="founder", basis="manual", supersedes_decision_id=base.id,
            )
            s.commit()
            results.append(("ok", tag, row.id))
        except Exception as exc:  # noqa: BLE001 — the loser MUST fail loudly
            s.rollback()
            results.append(("error", tag, str(exc)))
        finally:
            s.close()

    t1 = threading.Thread(target=writer, args=("A",))
    t2 = threading.Thread(target=writer, args=("B",))
    t1.start(); t2.start(); t1.join(); t2.join()

    statuses = [r[0] for r in results]
    assert statuses.count("ok") == 1 and statuses.count("error") == 1, f"exactly one winner: {results}"
    fresh = SessionLocal()
    try:
        _set_rls_user(fresh, owner_id)
        current = active_decision(fresh, owner_id=owner_id, problem_id=problem.id)
        assert current.decision.startswith("Uppdatering"), "the winner is the stored current decision"
        rows = fresh.query(LifeProblemDecision).filter_by(problem_id=problem.id).all()
        # ATOMICITY is the real assertion: the loser rolled back completely — no debris
        # row, no half-state. The chain is exactly base(superseded) + winner(active).
        assert len(rows) == 2 and [r.status for r in rows].count("active") == 1, "chain consistent after the race"
        by_status = {r.status: r for r in rows}
        assert "superseded" in by_status and "active" in by_status
    finally:
        fresh.close()
