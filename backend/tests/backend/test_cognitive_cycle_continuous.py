"""M6 tests (Round 9): CONTINUOUS VERIFICATION — the full cognitive cycle 1-10 on real
PostgreSQL/Redis, extended past Round 8's acceptance with the two missing pieces:

  8. REMEMBER important findings: a conclusion that actually changed is recorded as a
     CANDIDATE learning signal (epistemic_correction_candidate) with provenance — SIGNAL
     != TRUTH WRITER, idempotent on the edge id, persistent across a restart, and
     listable/dismissable by the founder through the existing signal contract.
  6. PERFORMANCE measured, not assumed: the NEW read paths added this round (graph
     context, lesson feedback, landscape) run in BOUNDED statement counts at realistic
     corpus sizes — a regression in query cost is a test failure, not a discovery later.

Steps 1-5 and 7 are exercised by the R8 acceptance suite (test_cognitive_acceptance_e2e)
and the graph-prompt suite; here they are chained into ONE continuous run and extended
with steps 8-10's new assertions, so the whole cycle provably closes on itself.
"""

import asyncio
import time
import uuid

import pytest
from sqlalchemy import event, text as sa_text

from app.config import get_settings
from app.db import SessionLocal
from app.founder_memory_signals.service import list_candidate_signals
from app.mainai_execution import planner
from app.mainai_execution.lessons import record_lesson
from app.models.claim_relationship import ClaimRelationship, ClaimRelationshipType
from app.models.document import Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.knowledge_claim import ClaimConfidence, ClaimStatus, ClaimType, KnowledgeClaim
from app.models.mainai_execution import EngineeringLessonSeverity
from app.rag.claims import EXTRACTION_VERSION
from app.rag.evidence_metadata import record_claim_evidence_metadata, summarize_topic_landscape
from app.rag.graph_instructions import format_graph_context_instructions
from app.mainai_execution.lesson_selection import format_lesson_feedback_instructions
from app.request_context import current_user_id as current_user_id_var

DIM = get_settings().embedding_dim


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _source_with_claim(db, owner_id, *, title, text, confidence=ClaimConfidence.likely):
    _set_rls_user(db, owner_id)
    source = Document(title=title, source=DocumentSource.upload, uploaded_by=owner_id)
    db.add(source)
    db.commit()
    chunk = DocumentChunk(document_id=source.id, owner_id=owner_id, chunk_index=0, text=text, embedding=[0.1] * DIM)
    db.add(chunk)
    db.commit()
    claim = KnowledgeClaim(
        owner_id=owner_id, source_id=source.id, chunk_id=chunk.id, claim_text=text,
        confidence=confidence, claim_type=ClaimType.historical, status=ClaimStatus.active,
        extraction_version=EXTRACTION_VERSION,
    )
    db.add(claim)
    db.commit()
    return source, claim


# --- steps 1-7 + 8: receive -> extract -> provenance -> quality -> contradictions ->
#     relationships -> calibrated conclusion -> REMEMBER the correction -------------------------


def test_full_cognitive_cycle_with_memory_signal(db_session, client, make_verified_user):
    from app.models.user import User
    from app.models.audit import AuditLog

    founder = db_session.query(User).filter(User.email == "founder@lifeos.local").first()
    founder_id = founder.id
    csrf = _login_from(client)

    # 1. RECEIVE information.
    src_a, claim_a = _source_with_claim(db_session, founder_id, title="Studie A", text="Substans X höjer markören Y.")
    src_b, claim_b = _source_with_claim(db_session, founder_id, title="Studie B", text="Substans X sänker markören Y.")

    # 2-3. EXTRACT/classify + preserve provenance: claims are grounded to chunks; the
    # founder stamps study metadata (provenance-aware, closed vocabulary).
    record_claim_evidence_metadata(
        db_session, founder_id, claim_a.id,
        {"research_topic": "x_markör", "study_design": "randomized_trial", "sample_size": 400,
         "conflicts_of_interest": "industry-funded", "causal_claim": True,
         "causal_claim_supported_by_design": True, "competing_explanations": ["controlled"]},
        declared_via="m6_cycle",
    )
    db_session.commit()

    # 4. EVALUATE evidence quality: the funding conflict floors at low, travels as caveat.
    from app.rag.evidence_metadata import quality_metadata_for_claims

    stamped = quality_metadata_for_claims(db_session, founder_id, [claim_a.id])
    assert stamped[claim_a.id].quality_grade == "low"
    assert any("does NOT by itself invalidate" in c for c in stamped[claim_a.id].quality_caveats)

    # 5. IDENTIFY contradictions (founder declares) + 6. EXPLORE relationships: the
    # contradiction is a claim-graph edge AND propagates to the topic landscape.
    res = client.post(
        f"/api/library/claims/{claim_b.id}/relationships",
        json={"to_claim_id": str(claim_a.id), "relationship_type": "contradicts"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200
    shifts = res.json()["epistemic_shifts"]
    assert any(sh["changed"] for sh in shifts)

    landscape = summarize_topic_landscape(db_session, founder_id, "x_markör")
    assert landscape.base.study_count == 1 and landscape.disagreements == [], (
        "the conflicting claim carries no topic stamp yet — the landscape counts only what is declared"
    )

    # 7. CALIBRATED conclusion: the chain trace says conflicting_evidence with reasons and
    #    the landscape line presents the declared conflict with both sides.
    chain = client.get(f"/api/library/claims/{claim_a.id}/evidence-chain").json()
    assert chain["epistemic_status"] == "conflicting_evidence" and chain["verdict_reasons"]

    # 8. REMEMBER the finding: the conclusion change is a CANDIDATE memory signal.
    signals = list_candidate_signals(db_session, owner_id=founder_id, signal_kind="correction_candidate")
    assert len(signals) == 1, "exactly one candidate for one conclusion change"
    assert signals[0].provenance["shifts"][0]["prior"] == "verified_fact"
    assert signals[0].provenance["shifts"][0]["current"] == "conflicting_evidence"

    # 9. CORRECT when evidence changes: new information flips the verdict again — and the
    #    audit trail shows BOTH movements.
    src_c, claim_c = _source_with_claim(db_session, founder_id, title="Replikering 2026", text="X påverkar inte markören Y.")
    res = client.post(
        f"/api/library/claims/{claim_c.id}/relationships",
        json={"to_claim_id": str(claim_a.id), "relationship_type": "contradicts"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200
    audits = db_session.query(AuditLog).filter(AuditLog.action == "epistemic_shift").all()
    assert len(audits) == 2, "both corrections are on the audit trail"
    signals2 = list_candidate_signals(db_session, owner_id=founder_id, signal_kind="correction_candidate")
    assert len(signals2) == 2, "each conclusion change is remembered separately"

    # 10. IMPROVE subsequent planning using measured outcomes: a lesson with a track
    #     record ranks FIRST and is applied to a real plan; the decision row exists.
    lesson = record_lesson(
        db_session, problem="Cyclic suite flakiness", root_cause="shared temp dir",
        affected_component=f"m6_component_{uuid.uuid4().hex[:6]}", severity=EngineeringLessonSeverity.medium,
        evidence="m6 cycle", fix="isolated tmp", general_rule="isolate tmp dirs",
        applies_to=["run_tests"], source_type="m6_tests", source_ref=f"m6-{uuid.uuid4()}",
        created_by="test", first_seen_at=__import__("datetime").datetime.utcnow(),
        regression_test="tests/backend/test_mainai_execution_planner.py::test_get_goal_raises_for_unknown_id",
    )
    db_session.commit()
    goal = planner.create_goal(db_session, owner_id=founder_id, title=f"M6 mål {uuid.uuid4().hex[:6]}", original_instruction="Kör.", created_by="test")
    plan = planner.create_plan(
        db_session, goal=goal, rationale="m6 step 10",
        tasks=[planner.PlannedTaskSpec(description="Kör", task_type="run_tests")],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(planner.MainAITask).filter(planner.MainAITask.plan_id == plan.id).one()
    assert lesson.regression_test in {s.get("target") for s in task.verification_plan if s.get("kind") == "targeted_tests"}
    from app.models.lesson_plan_decisions import EngineeringLessonPlanDecision

    assert db_session.query(EngineeringLessonPlanDecision).filter_by(task_id=task.id, lesson_id=lesson.id).count() == 1

    # Persistence + restart behavior: a FRESH connection sees the signals, the shifts,
    # and the decision rows — the cognitive cycle's outputs are durable.
    fresh = SessionLocal()
    try:
        _set_rls_user(fresh, founder_id)
        assert len(list_candidate_signals(fresh, owner_id=founder_id, signal_kind="correction_candidate")) == 2
        assert fresh.query(EngineeringLessonPlanDecision).filter_by(lesson_id=lesson.id).count() == 1
    finally:
        fresh.close()


def _login_from(client) -> str:
    res = client.post("/api/auth/login", json={"email": "founder@lifeos.local", "password": "TestFounderPassword123!"})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def test_memory_signal_is_dismissable_via_the_existing_contract(db_session, client):
    """The remembered finding is a CANDIDATE — the founder's explicit judgment (dismiss)
    works through the existing signal contract; the system never auto-promoted it. Self-
    seeded: every test starts from a truncated database."""
    from app.models.user import User
    from app.founder_memory_signals.service import dismiss_candidate_signal, list_candidate_signals

    founder = db_session.query(User).filter(User.email == "founder@lifeos.local").first()
    _sa, claim_a = _source_with_claim(db_session, founder.id, title="A", text="Påstående A.")
    _sb, claim_b = _source_with_claim(db_session, founder.id, title="B", text="Påstående B.")
    csrf = _login_from(client)
    res = client.post(
        f"/api/library/claims/{claim_b.id}/relationships",
        json={"to_claim_id": str(claim_a.id), "relationship_type": "contradicts"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 200

    signals = list_candidate_signals(db_session, owner_id=founder.id, signal_kind="correction_candidate")
    assert signals, "the correction candidate exists after the declared shift"
    dismiss_candidate_signal(db_session, owner_id=founder.id, signal_id=signals[0].id, reason="duplicate of an existing note")
    db_session.commit()
    # The default listing includes resolved rows (list semantics, not a bug) — the founder's
    # judgment is recorded as the signal's STATUS, and the unreviewed view excludes it.
    remaining = list_candidate_signals(db_session, owner_id=founder.id, signal_kind="correction_candidate")
    dismissed_row = next(s for s in remaining if s.id == signals[0].id)
    assert dismissed_row.status == "dismissed"
    unreviewed = list_candidate_signals(db_session, owner_id=founder.id, signal_kind="correction_candidate", status="unreviewed")
    assert all(s.id != signals[0].id for s in unreviewed)


# --- performance measurement: bounded query cost on the NEW read paths ---------------------------


def _counted(engine, fn, *args, **kwargs) -> tuple[object, int]:
    statements: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        result = fn(*args, **kwargs)
    finally:
        try:
            event.remove(engine, "before_cursor_execute", _count)
        except Exception:
            pass
    return result, len(statements)


def test_read_path_query_costs_are_bounded(db_session, client):
    """The prompt-layer read paths added in R9 must scale with RETRIEVED SOURCES, not with
    corpus size. Bounds measured on a 12-claim, 3-source corpus: adding corpus must not
    add statements (the topic scan, overlap scan and landscape are each a bounded query)."""
    from app.models.user import User

    founder = db_session.query(User).filter(User.email == "founder@lifeos.local").first()
    founder_id = founder.id
    sources = []
    for i in range(3):
        src, claim = _source_with_claim(db_session, founder_id, title=f"Perf källa {i}", text=f"Perf påstående {i} om kaffe och puls.")
        record_claim_evidence_metadata(
            db_session, founder_id, claim.id, {"research_topic": "kaffe_puls", "study_design": "randomized_trial", "sample_size": 100 + i, "competing_explanations": ["controlled"]},
            declared_via="perf_test",
        )
        sources.append(src.id)
    # Extra corpus noise: 9 more stamped claims OUTSIDE the retrieved set.
    for i in range(9):
        _s, c = _source_with_claim(db_session, founder_id, title=f"Brus {i}", text=f"Brus påstående {i}.")
        record_claim_evidence_metadata(db_session, founder_id, c.id, {"dose": f"{i} mg"}, declared_via="perf_noise")
    db_session.commit()

    engine = db_session.get_bind()

    _line, graph_stmts = _counted(
        engine, format_graph_context_instructions, db_session, founder_id, sources, "Vad gäller för kaffe och puls?"
    )
    # R10 M6 added the scientific composition + investigation policy line to this block;
    # its rows/landscape are SHARED with the landscape (the 12-statement regression the
    # original bound caught was the duplicate fetch — fixed by passing the fetched rows).
    # 9 statements = consequence walk + overlap scan + topic lookup + landscape(3, shared
    # with science) — still every-bounded, still scaling with sources not corpus.
    assert graph_stmts <= 9, (
        f"graph context used {graph_stmts} statements — the consequence walk, overlap scan "
        "and topic lookup must each stay a bounded query"
    )

    _fb, feedback_stmts = _counted(engine, format_lesson_feedback_instructions, db_session, owner_id=founder_id)
    assert feedback_stmts <= 4, f"lesson feedback used {feedback_stmts} statements"

    landscape, landscape_stmts = _counted(
        engine, summarize_topic_landscape, db_session, founder_id, "kaffe_puls"
    )
    assert landscape_stmts <= 4, f"topic landscape used {landscape_stmts} statements"
    assert landscape.base.study_count == 3, "the topic scope still selects exactly its own studies"

    # Wall-clock sanity at this corpus size (generous bound — a CI box, not a perf rig):
    # detects ORDER-OF-MAGNITUDE regressions, not microseconds.
    start = time.perf_counter()
    format_graph_context_instructions(db_session, founder_id, sources, "Vad gäller för kaffe och puls?")
    elapsed = time.perf_counter() - start
    assert elapsed < 2.0, f"graph context took {elapsed:.2f}s at 12 claims — pathological regression"
